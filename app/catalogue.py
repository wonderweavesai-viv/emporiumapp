"""Curated design catalogue: a multi-page A4 PDF of chosen designs for sending to a customer.

Page 1 is a cover with your brand logo. Then one design per page (or four per page).
Every design can carry your logo (default, bottom right) or a watermark, at 10-100% strength.
Pictures are made at 96 dpi or less, JPEG quality 90, so the file stays small enough to email."""
import io
import math
from datetime import date
from pathlib import Path

from PIL import Image, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

Image.MAX_IMAGE_PIXELS = None

PAGE_W, PAGE_H = 595.28, 841.89          # A4 in points
MARGIN = 36
MAX_DPI = 96
MAX_SIDE = 2000                          # longest side of any picture, in pixels
JPEG_Q = 90
MARK_MODES = ("none", "logo", "watermark")
POSITIONS = ("br", "center", "tile")
MAX_DESIGNS = 300
INK = (0.12, 0.13, 0.14)
MUTED = (0.42, 0.44, 0.46)

DEFAULTS = {"company": "Emporium", "cover": True, "mark": "logo", "position": "br", "opacity": 100, "size": 18}


def clean_settings(d):
    """Make any stored or posted catalogue settings safe, with defaults filled in."""
    d = d or {}
    out = dict(DEFAULTS)
    if str(d.get("company", "")).strip():
        out["company"] = " ".join(str(d["company"]).split())[:60]
    if "cover" in d:
        out["cover"] = bool(d["cover"])
    if d.get("mark") in MARK_MODES:
        out["mark"] = d["mark"]
    if d.get("position") in POSITIONS:
        out["position"] = d["position"]
    for k, lo, hi in (("opacity", 10, 100), ("size", 5, 60)):
        try:
            out[k] = int(min(hi, max(lo, round(float(d[k])))))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def load_brand_image(path, max_side=1600):
    """A logo or watermark as RGBA, reduced if it is huge."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
    if max(im.size) > max_side:
        k = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    return im


def _with_opacity(mark, opacity):
    mark = mark.copy()
    a = mark.getchannel("A").point(lambda v: int(v * opacity / 100))
    mark.putalpha(a)
    return mark


def apply_mark(img, mark, position, opacity, size):
    """Put the logo/watermark on a picture. `size` is the mark's width as a percent of the picture's width."""
    if mark is None:
        return img
    base = img.convert("RGBA")
    w, h = base.size
    mw = max(8, round(w * size / 100))
    mh = max(8, round(mark.height * mw / mark.width))
    if mh > h * 0.9:                                  # very tall mark: never bigger than the picture
        mh = round(h * 0.9); mw = max(8, round(mark.width * mh / mark.height))
    m = _with_opacity(mark.resize((mw, mh), Image.LANCZOS), opacity)
    if position == "br":
        pad = max(6, round(min(w, h) * 0.03))
        base.alpha_composite(m, (max(0, w - mw - pad), max(0, h - mh - pad)))
    elif position == "center":
        base.alpha_composite(m, ((w - mw) // 2, (h - mh) // 2))
    else:                                              # tiled on a slant, so it cannot be cropped away
        t = m.rotate(30, expand=True, resample=Image.BICUBIC)
        sx, sy = round(t.width * 1.5), round(t.height * 1.5)
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        row = 0
        for y in range(-t.height, h + t.height, sy):
            for x in range(-t.width + (sx // 2 if row % 2 else 0), w + t.width, sx):
                _paste_clipped(layer, t, x, y)
            row += 1
        base = Image.alpha_composite(base, layer)
    return base.convert("RGB")


def _paste_clipped(layer, tile, x, y):
    """alpha_composite that tolerates tiles hanging over the edge."""
    w, h = layer.size
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + tile.width), min(h, y + tile.height)
    if x1 <= x0 or y1 <= y0:
        return
    layer.alpha_composite(tile.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))


def load_design(path, box_pt):
    """The design as RGB at no more than 96 dpi for a box of (w, h) points, never enlarged, never over 2000 px."""
    with Image.open(path) as im:
        im.draft("RGB", (MAX_SIDE * 2, MAX_SIDE * 2))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba).convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")
        else:
            im = im.copy()
    bw, bh = box_pt
    k = min(bw / im.width, bh / im.height)            # points per pixel to fit the box
    shown = (im.width * k, im.height * k)             # size on the page, in points
    want = (max(1, round(shown[0] / 72 * MAX_DPI)), max(1, round(shown[1] / 72 * MAX_DPI)))
    scale = min(1.0, want[0] / im.width, want[1] / im.height, MAX_SIDE / max(im.size))
    if scale < 1:
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    return im, shown


def _jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_Q, dpi=(MAX_DPI, MAX_DPI), optimize=True)
    buf.seek(0)
    return buf


def _fit_text(c, text, font, size, width):
    while len(text) > 3 and c.stringWidth(text, font, size) > width:
        text = text[:-2].rstrip() + "…"
    return text


def _cover(c, st, title, customer, logo, when):
    c.setFillColorRGB(*INK)
    y = PAGE_H * 0.58
    if logo is not None:
        bw, bh = PAGE_W * 0.55, PAGE_H * 0.22
        k = min(bw / logo.width, bh / logo.height)
        lw, lh = logo.width * k, logo.height * k
        buf = io.BytesIO(); logo.save(buf, "PNG"); buf.seek(0)
        c.drawImage(ImageReader(buf), (PAGE_W - lw) / 2, y, lw, lh, mask="auto")
    else:
        c.setFont("Helvetica-Bold", 34)
        c.drawCentredString(PAGE_W / 2, y + 40, _fit_text(c, st["company"], "Helvetica-Bold", 34, PAGE_W - 120))
    c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setLineWidth(0.8)
    c.line(PAGE_W / 2 - 40, y - 28, PAGE_W / 2 + 40, y - 28)
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(PAGE_W / 2, y - 64, _fit_text(c, title, "Helvetica-Bold", 22, PAGE_W - 120))
    ly = y - 92
    c.setFillColorRGB(*MUTED)
    if customer:
        c.setFont("Helvetica", 13)
        c.drawCentredString(PAGE_W / 2, ly, _fit_text(c, "Prepared for " + customer, "Helvetica", 13, PAGE_W - 120)); ly -= 20
    c.setFont("Helvetica", 11)
    c.drawCentredString(PAGE_W / 2, ly, when)
    c.setFont("Helvetica", 9)
    c.drawCentredString(PAGE_W / 2, 48, st["company"])


def _footer(c, st, n):
    c.setFillColorRGB(*MUTED); c.setFont("Helvetica", 8)
    c.drawString(MARGIN, 22, st["company"])
    c.drawRightString(PAGE_W - MARGIN, 22, str(n))


def _caption(c, x, y, w, item, big):
    c.setFillColorRGB(*INK)
    fs = 14 if big else 9.5
    c.setFont("Helvetica-Bold", fs)
    c.drawString(x, y, _fit_text(c, item["name"], "Helvetica-Bold", fs, w))
    sub = "  ·  ".join(t for t in (item.get("material"), item.get("size")) if t)
    if sub:
        c.setFillColorRGB(*MUTED); c.setFont("Helvetica", 10.5 if big else 8)
        c.drawString(x, y - (17 if big else 11.5), _fit_text(c, sub, "Helvetica", 10.5 if big else 8, w))


def build(items, settings, title="Design selection", customer="", per_page=1, logo=None, watermark=None, today=None):
    """items: [{name, material, path, size}] in the order wanted. Returns the PDF bytes.
    `logo` and `watermark` are RGBA images or None."""
    st = clean_settings(settings)
    if not items:
        raise ValueError("Choose at least one design.")
    if len(items) > MAX_DESIGNS:
        raise ValueError(f"A catalogue can hold up to {MAX_DESIGNS} designs.")
    per_page = 4 if int(per_page or 1) == 4 else 1
    mark = {"logo": logo, "watermark": watermark}.get(st["mark"])
    when = (today or date.today()).strftime("%d %B %Y").lstrip("0")
    title = " ".join(str(title or "").split())[:80] or "Design selection"
    customer = " ".join(str(customer or "").split())[:80]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(title); c.setAuthor(st["company"])
    page = 0
    if st["cover"]:
        _cover(c, st, title, customer, logo, when)
        c.showPage()
    cells = [(MARGIN, MARGIN + 70, PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN - 70 - 20)] if per_page == 1 else None
    if per_page == 4:
        gx, gy = 18, 14
        cw = (PAGE_W - 2 * MARGIN - gx) / 2; ch = (PAGE_H - 2 * MARGIN - 20 - gy) / 2
        top = PAGE_H - MARGIN - 8
        cells = [(MARGIN + (i % 2) * (cw + gx), top - (i // 2 + 1) * ch - (i // 2) * gy, cw, ch) for i in range(4)]
    for start in range(0, len(items), per_page):
        page += 1
        chunk = items[start:start + per_page]
        for item, (x, y, w, h) in zip(chunk, cells):
            cap_h = 34 if per_page == 1 else 30
            box = (w, h - (0 if per_page == 1 else cap_h))
            img, shown = load_design(item["path"], box)
            img = apply_mark(img, mark, st["position"], st["opacity"], st["size"]) if mark is not None else img
            iw, ih = shown
            if per_page == 1:
                ix, iy = x + (w - iw) / 2, y + (h - ih) / 2 + 12
                c.drawImage(ImageReader(_jpeg(img)), ix, iy, iw, ih)
                c.setStrokeColorRGB(0.85, 0.85, 0.85); c.setLineWidth(0.5); c.rect(ix, iy, iw, ih, fill=0)
                _caption(c, x, 58, w, item, True)
            else:
                ix, iy = x + (w - iw) / 2, y + cap_h + (h - cap_h - ih) / 2
                c.drawImage(ImageReader(_jpeg(img)), ix, iy, iw, ih)
                c.setStrokeColorRGB(0.85, 0.85, 0.85); c.setLineWidth(0.5); c.rect(ix, iy, iw, ih, fill=0)
                _caption(c, x, y + cap_h - 12, w, item, False)
        _footer(c, st, page + (1 if st["cover"] else 0))
        c.showPage()
    c.save()
    return buf.getvalue()
