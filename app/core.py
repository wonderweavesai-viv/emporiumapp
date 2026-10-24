#!/usr/bin/env python3
"""Print engine: crop maths, resolution check, client proof PDF and print TIFF."""
import io
import math
import re
from pathlib import Path

from PIL import Image, ImageOps

from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import render
import textures as textures_mod

Image.MAX_IMAGE_PIXELS = None
A4_W, A4_H = 595.44, 841.92  # points


def fmt(n: float) -> str:
    return f"{n:g}"


def dim(cm: float, unit: str = "cm") -> str:
    """A length for people to read, in the unit they work in: '180 cm' or '70.87 in'."""
    return f"{fmt(round(cm / 2.54, 2))} in" if unit == "in" else f"{fmt(round(cm, 2))} cm"


def safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")


def job_size(wall_w, wall_h, allowance):
    return wall_w + allowance, wall_h + allowance


def target_px(jw, jh, dpi):
    return round(jw / 2.54 * dpi), round(jh / 2.54 * dpi)


def check_params(p: dict):
    """Validate and normalise request parameters. Raises ValueError with a message fit to show to a salesperson."""
    try:
        out = {
            "wall_w": float(p["wall_w"]),
            "wall_h": float(p["wall_h"]),
            "allowance": float(p.get("allowance", 10)),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("Wall width and height are required numbers.")
    if not (20 <= out["wall_w"] <= 1500 and 20 <= out["wall_h"] <= 1500):
        raise ValueError("Wall width and height should be between 20 and 1500 cm.")
    if not (0 <= out["allowance"] <= 100):
        raise ValueError("Allowance should be between 0 and 100 cm.")
    out["unit"] = "in" if p.get("unit") == "in" else "cm"
    if p.get("dpi") == "native":        # "Image default": the artwork's own resolution at this size
        out["dpi_native"] = True
    elif "dpi" in p and p["dpi"] not in (None, ""):
        try:
            out["dpi"] = int(p["dpi"])
        except (TypeError, ValueError):
            raise ValueError("dpi must be a whole number.")
        if not (36 <= out["dpi"] <= 600):
            raise ValueError("dpi should be between 36 and 600.")
    return out


DROP_MODES = render.DROP_MODES
CUSTOM_MODES = ("custom_v", "custom_h")      # drop by any amount, set in "drop" (percent of a repeat)
TILE_MODES = ("off", "repeat", "mirror") + tuple(DROP_MODES) + CUSTOM_MODES
TILE_LABEL = {"repeat": "Repeat", "mirror": "Mirror", "brick": "Brick", "half_v": "Half drop (vertical)",
              "half_h": "Half drop (horizontal)", "quarter_v": "Quarter drop (vertical)", "quarter_h": "Quarter drop (horizontal)"}
MIN_REPEAT_CM = 5.0


def cover_w(mw, mh, jw, jh):
    """Smallest artwork width (cm) that still covers the whole wall."""
    return max(jw, jh * mw / mh)


def eff_dpi(mw, art_w):
    """Pixels per inch the master gives at this artwork width. Same in both directions (uniform scale)."""
    return mw / art_w * 2.54


def max_repeat_w(mw, mh, jw, jh, den=1):
    """Largest repeat width allowed in tile mode: no bigger than covering the wall, and with a unit that stays
    within 3x the wall area so rendering memory stays bounded."""
    return min(cover_w(mw, mh, jw, jh), (3 * jw * jh * mw / mh / den) ** 0.5)


TEXTURE_MODES = ("repeat", "mirror")
TEXTURE_MIN_CM = 2.0


def check_texture(t, lookup, jw, jh):
    """Validate the texture layer. `lookup(id)` gives (path, name, w, h) or None. Returns None when there is no texture."""
    if not t or not t.get("id"):
        return None
    found = lookup(str(t["id"])) if lookup else None
    if not found:
        raise ValueError("That texture is not in the texture folder any more.")
    _, name, tw, th = found
    try:
        mode = t.get("mode", "repeat")
        scale = float(t.get("scale") or 50)
        opacity = float(t.get("opacity", 100))
    except (TypeError, ValueError):
        raise ValueError("The texture settings are not valid.")
    if mode not in TEXTURE_MODES:
        raise ValueError("Unknown texture tiling mode.")
    if scale != scale or opacity != opacity:
        raise ValueError("The texture settings are not valid.")
    # one copy of the texture stays within 4x the wall area, so memory stays bounded
    top = (4 * jw * jh * tw / th) ** 0.5
    scale = min(max(scale, TEXTURE_MIN_CM), max(TEXTURE_MIN_CM, top))
    return {"id": str(t["id"]), "name": name, "scale": scale, "mode": mode, "opacity": min(100.0, max(0.0, opacity))}


def check_recipe(r, mw, mh, jw, jh, textures=None):
    """Validate and normalise an edit recipe. Raises ValueError with a message fit for a salesperson.
    `textures` is a lookup function for the texture library (see check_texture)."""
    r = r or {}
    tile = r.get("tile", "off")
    if tile not in TILE_MODES:
        raise ValueError("Unknown tile mode.")
    try:
        cover = cover_w(mw, mh, jw, jh)
        art_w = float(r.get("art_w") or cover)
        ah_ratio = mh / mw
        if tile == "off":
            art_w = max(art_w, cover)
        else:
            top = max_repeat_w(mw, mh, jw, jh, DROP_MODES.get(tile, ("", 1))[1])
            if art_w < MIN_REPEAT_CM:
                raise ValueError(f"The repeat should be at least {fmt(MIN_REPEAT_CM)} cm wide.")
            if art_w > top * 1.001:
                raise ValueError("This repeat is too large to tile on this wall. Make it smaller or turn tiling off.")
        ah = art_w * ah_ratio
        drop = 0.0
        if tile in CUSTOM_MODES:
            drop = min(100.0, max(0.0, float(r.get("drop", 50))))
            if drop != drop:
                raise ValueError("The drop amount is not valid.")
        off_x = float(r.get("off_x", (art_w - jw) / 2 if tile == "off" else 0))
        off_y = float(r.get("off_y", (ah - jh) / 2 if tile == "off" else 0))
        colour = {}
        src = r.get("colour") or {}
        for k, (lo, hi) in render.COLOUR_RANGE.items():
            v = float(src.get(k, 0) or 0)
            if v != v:
                raise ValueError("Colour settings are not valid.")
            colour[k] = min(hi, max(lo, v))
        hsl = render.check_hsl(r.get("hsl"))
        texture = check_texture(r.get("texture"), textures, jw, jh)
    except (TypeError, ValueError) as e:
        if isinstance(e, ValueError) and str(e) and not str(e).startswith("could not convert"):
            raise
        raise ValueError("The edit settings are not valid.")
    if tile == "off":
        off_x = min(max(off_x, 0.0), max(0.0, art_w - jw))
        off_y = min(max(off_y, 0.0), max(0.0, ah - jh))
    else:
        ax, den = DROP_MODES.get(tile, ("", 1))
        if tile in CUSTOM_MODES:     # the pattern comes round again after this many columns (or rows)
            dn = int(round(drop * 100))
            den, ax = 10000 // math.gcd(dn, 10000) if dn else 1, tile[-1]
        period_x = art_w * (2 if tile == "mirror" else den if ax == "v" else 1)
        period_y = ah * (2 if tile == "mirror" else den if ax == "h" else 1)
        off_x, off_y = off_x % period_x, off_y % period_y
    return {"flip_h": bool(r.get("flip_h")), "flip_v": bool(r.get("flip_v")), "tile": tile, "drop": drop,
            "art_w": art_w, "off_x": off_x, "off_y": off_y, "colour": colour, "hsl": hsl, "texture": texture}


COLOUR_LABEL = dict(brightness="Brightness", contrast="Contrast", saturation="Saturation", vibrance="Vibrance",
                    hue="Hue shift", warmth="Warmth", bw="Black & white", sepia="Sepia", fade="Fade")


def describe_hsl(h):
    """Plain-language lines for the HSL colour editor, e.g. 'HSL Blues: hue +30, saturation -20'."""
    tag = "HSV" if h["mode"] == "hsv" else "HSL"
    lum = "value" if h["mode"] == "hsv" else "luminosity"

    def parts(d):
        p = []
        if d["hue"]:
            p.append(f"hue {d['hue']:+g}")
        if d["sat"]:
            p.append(f"saturation {d['sat']:+g}")
        if d["lum"]:
            p.append(f"{lum} {d['lum']:+g}")
        return ", ".join(p)

    out = []
    if parts(h["master"]):
        out.append(f"{tag} all colours: {parts(h['master'])}")
    default = render.hsl_default()["ranges"]
    for i, rg in enumerate(h["ranges"]):
        p = parts(rg)
        if not p:
            continue
        same = all(abs(x - y) < 0.5 for x, y in zip(rg["a"], default[i]["a"]))
        centre = (rg["a"][1] + rg["a"][2]) / 2 % 360
        name = render.HSL_NAMES[i] if same else f"colour range around hue {round(centre)} deg"
        out.append(f"{tag} {name}: {p}")
    return out


def describe_edits(r, unit="cm"):
    """Plain-language list of the edits in a recipe (empty list when the artwork is untouched)."""
    out = []
    if r["flip_h"]:
        out.append("Flipped horizontally")
    if r["flip_v"]:
        out.append("Flipped vertically")
    if r["tile"] != "off":
        label = (f"Custom drop {r['drop']:g}% ({'vertical' if r['tile'] == 'custom_v' else 'horizontal'})"
                 if r["tile"] in CUSTOM_MODES else TILE_LABEL[r["tile"]])
        out.append(f"{label} tile, {dim(r['art_w'], unit)} wide")
    for k, label in COLOUR_LABEL.items():
        v = r["colour"].get(k, 0)
        if v:
            out.append(f"{label} {v:+g}" if k not in ("bw", "sepia", "fade") else f"{label} {v:g}%")
    out.extend(describe_hsl(r["hsl"]))
    t = r.get("texture")
    if t and t["opacity"] > 0:
        out.append(f"Texture \"{t['name']}\" multiplied under the artwork, {dim(t['scale'], unit)} wide, "
                   f"{'mirrored' if t['mode'] == 'mirror' else 'repeated'}, opacity {t['opacity']:g}%")
    return out


def is_edited(r):
    return bool(describe_edits(r))


def make_proof_pdf(region, design, material, jw, jh, logo_path=None, edits=None, unit="cm") -> bytes:
    """One A4 page: the wall picture (a PIL image already rendered from the recipe) to scale, dimension lines,
    caption, a line listing any edits, and an optional logo."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(A4_W, A4_H))
    c.setTitle(f"{design} - Proof")

    max_w, max_h = 400.0, 610.0
    scale = min(max_w / jw, max_h / jh)
    w_pt, h_pt = jw * scale, jh * scale
    x0 = (A4_W - w_pt) / 2 + 8
    y0 = A4_H - 50 - h_pt
    c.drawImage(ImageReader(region), x0, y0, w_pt, h_pt)

    c.setStrokeColorRGB(0, 0, 0)
    c.setFillColorRGB(0, 0, 0)
    c.setLineWidth(0.6)
    xl = x0 - 8
    c.line(xl, y0, xl, y0 + h_pt)
    yb = y0 - 9
    c.line(x0, yb, x0 + w_pt, yb)
    c.setFont("Helvetica", 9)
    c.saveState()
    c.translate(xl - 8, y0 + h_pt / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, dim(jh, unit))
    c.restoreState()
    c.drawCentredString(x0 + w_pt / 2, yb - 16, dim(jw, unit))

    c.setFont("Helvetica", 11)
    c.drawString(24, 70, f"{design} - Material {material}" if material else design)
    if unit == "in":
        c.drawString(24, 42, f"Dimensions - {fmt(round(jw / 2.54, 2))} in x {fmt(round(jh / 2.54, 2))} in  ({fmt(jw)} cm x {fmt(jh)} cm)")
    else:
        c.drawString(24, 42, f"Dimensions - {fmt(jw)} cm x {fmt(jh)} cm")
    if edits:
        c.setFont("Helvetica", 8)
        text = "Edited version: " + ", ".join(edits)
        while len(text) > 3 and c.stringWidth(text, "Helvetica", 8) > A4_W - 48 - 80:
            text = text[:-2]
        c.drawString(24, 24, text if text == "Edited version: " + ", ".join(edits) else text.rstrip(", ") + "...")

    if logo_path:
        with Image.open(logo_path) as logo:
            lw = 62.0
            lh = lw * logo.height / logo.width
            c.drawImage(ImageReader(logo.convert("RGBA")), A4_W - 24 - lw, 14, lw, lh, mask="auto")
    c.showPage()
    c.save()
    return buf.getvalue()


def proof_region(preview_path: Path, recipe, jw, jh, texture_path=None):
    """The wall picture for the proof, made from the preview image with the same engine as the print file."""
    long_side = 1600
    k = long_side / max(jw, jh)
    with Image.open(preview_path) as pv:
        pv = pv.convert("RGB")
        tex = textures_mod.load(texture_path, 1600) if texture_path and recipe.get("texture") else None
        return render.render(pv, recipe, jw, jh, max(1, round(jw * k)), max(1, round(jh * k)), texture=tex)


def make_print_tiff(master_path: Path, recipe, jw, jh, dpi, out_path: Path, progress=None, texture_path=None):
    """Render the master through the recipe at the exact print size and write an LZW TIFF."""
    tw, th = target_px(jw, jh, dpi)
    with Image.open(master_path) as im:
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba).convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")
            icc = None  # profile no longer matches after conversion
        tex = textures_mod.load(texture_path) if texture_path and recipe.get("texture") else None
        out = render.render(im, recipe, jw, jh, tw, th, progress=progress, texture=tex)
    del im
    kwargs = {"format": "TIFF", "compression": "tiff_lzw", "dpi": (dpi, dpi)}
    if icc:
        kwargs["icc_profile"] = icc
    out.save(out_path, **kwargs)
    return tw, th
