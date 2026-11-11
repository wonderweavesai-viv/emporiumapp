"""Printing a wall as vertical panels: the arithmetic, and the two PDFs (printer instructions, installer card).

Everything is worked out on the print area (wall + allowance). Panels run from top to bottom, side by side, left to
right as seen when facing the wall. Every panel carries `overlap` cm of extra image on each joining side, so two
neighbours share 2 x overlap cm of identical artwork."""
import io

from PIL import Image
from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A4, A5, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import core

MAX_PANELS = 40
OVERLAP_RANGE = (1.0, 10.0)
LABEL_STRIP_CM = 2.5          # optional white strip with the panel number, added below the image (to be trimmed off)


def plan(width, material, margin=10.0, overlap=2.0, count=None):
    """Work out the panels for a print area `width` cm wide.

    material = width of the roll, margin = unprinted width across the roll (total), overlap = extra image on
    each joining side. Returns a dict; raises ValueError with a message fit for a salesperson."""
    try:
        width, material, margin, overlap = float(width), float(material), float(margin), float(overlap)
    except (TypeError, ValueError):
        raise ValueError("Material width, margin and overlap must be numbers.")
    lo, hi = OVERLAP_RANGE
    if not (lo <= overlap <= hi):
        raise ValueError(f"Overlap should be between {core.fmt(lo)} and {core.fmt(hi)} cm on each side.")
    if material < 20 or material > 500:
        raise ValueError("Material width should be between 20 and 500 cm.")
    if margin < 0 or margin >= material:
        raise ValueError("The unprinted margin has to be smaller than the material width.")
    printable = material - margin
    if printable <= 2 * overlap + 5:
        raise ValueError(f"The printable width ({core.fmt(printable)} cm) is too small for an overlap of {core.fmt(overlap)} cm on each side.")

    def widest(n):
        return width / 2 + overlap if n == 2 else width / n + 2 * overlap

    if width <= printable:
        n_min = 1
    else:
        n_min = next((n for n in range(2, MAX_PANELS + 1) if widest(n) <= printable + 1e-9), None)
        if n_min is None:
            raise ValueError(f"This wall would need more than {MAX_PANELS} panels. Use wider material or a smaller overlap.")
    n = n_min
    if count not in (None, "", 0):
        try:
            n = int(count)
        except (TypeError, ValueError):
            raise ValueError("The number of panels must be a whole number.")
        if n < n_min:
            raise ValueError(f"This wall needs at least {n_min} panels with this material and overlap.")
        if n > MAX_PANELS:
            raise ValueError(f"Up to {MAX_PANELS} panels are possible.")
    panels = []
    for i in range(n):
        s0, s1 = width * i / n, width * (i + 1) / n
        left, right = (overlap if i > 0 else 0.0), (overlap if i < n - 1 else 0.0)
        panels.append({"n": i + 1, "x0": s0 - left, "x1": s1 + right, "core0": s0, "core1": s1,
                       "w": s1 - s0 + left + right, "left_ov": left, "right_ov": right})
    return {"count": n, "min_count": n_min, "needed": n > 1, "width": width, "material": material, "margin": margin,
            "printable": printable, "overlap": overlap, "panels": panels,
            "spare": [round(material - p["w"], 2) for p in panels]}


def panel_pixels(total_px, n, overlap_px):
    """Pixel columns (x0, x1) of each panel in the full-width picture. All cut from one pixel grid, so neighbours
    hold identical pixels where they overlap and nothing drifts."""
    out = []
    for i in range(n):
        s0, s1 = round(total_px * i / n), round(total_px * (i + 1) / n)
        out.append((max(0, s0 - overlap_px) if i > 0 else 0, min(total_px, s1 + overlap_px) if i < n - 1 else total_px))
    return out


def panel_filename(stem, i, n, w_cm, h_cm, dpi):
    return f"{stem}_P{i}of{n}_{core.fmt(round(w_cm, 1))}x{core.fmt(round(h_cm, 1))}cm_{dpi:g}dpi_print.tiff"


# ------------------------------------------------------------------ drawing
BAND_A = Color(0.15, 0.45, 0.45, alpha=0.16)
BAND_B = Color(0.9, 0.55, 0.1, alpha=0.16)
OVERLAP = Color(0.85, 0.15, 0.15, alpha=0.38)
INK = Color(0.1, 0.1, 0.1)
MUTED = Color(0.38, 0.38, 0.38)
ACCENT = Color(0.06, 0.37, 0.37)


def draw_layout(c, x, y, box_w, box_h, region, pl, height_cm, unit="cm", num_size=None, brackets=True):
    """Draws the wall picture with the panels on it inside the box (x, y = bottom-left). Returns the size used."""
    width = pl["width"]
    k = min(box_w / width, (box_h - (46 if brackets else 6)) / height_cm)
    iw, ih = width * k, height_cm * k
    ox = x + (box_w - iw) / 2
    oy = y + box_h - ih
    c.saveState()
    c.setFillColor(Color(0.93, 0.93, 0.93))
    c.rect(ox, oy, iw, ih, stroke=0, fill=1)
    if region is not None:
        c.drawImage(ImageReader(region), ox, oy, iw, ih)
    for p in pl["panels"]:
        c.setFillColor(BAND_A if p["n"] % 2 else BAND_B)
        c.rect(ox + p["x0"] * k, oy, (p["x1"] - p["x0"]) * k, ih, stroke=0, fill=1)
    ov = pl["overlap"]
    for p in pl["panels"][:-1]:       # the shared zone at each seam
        s = p["core1"]
        c.setFillColor(OVERLAP)
        c.rect(ox + (s - ov) * k, oy, 2 * ov * k, ih, stroke=0, fill=1)
        c.setStrokeColor(Color(0.7, 0.05, 0.05))
        c.setDash(3, 2)
        c.setLineWidth(0.7)
        c.line(ox + s * k, oy, ox + s * k, oy + ih)
        c.setDash()
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.rect(ox, oy, iw, ih, stroke=1, fill=0)
    # panel numbers
    size = num_size or max(9, min(34, (iw / pl["count"]) * 0.42))
    for p in pl["panels"]:
        cx = ox + (p["core0"] + p["core1"]) / 2 * k
        cy = oy + ih - size * 1.05
        c.setFillColor(white)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.2)
        c.circle(cx, cy, size * 0.62, stroke=1, fill=1)
        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Bold", size)
        c.drawCentredString(cx, cy - size * 0.34, str(p["n"]))
    # top marker
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", max(6.5, min(9, iw / 40)))
    c.drawString(ox + 3, oy + ih + 3, "TOP")
    if brackets:      # width of each panel, in two staggered rows so neighbours never collide
        fs = max(6, min(8.5, iw / pl["count"] / 9))
        for p in pl["panels"]:
            row = p["n"] % 2
            by = oy - 8 - row * 17
            x0, x1 = ox + p["x0"] * k, ox + p["x1"] * k
            c.setStrokeColor(ACCENT if p["n"] % 2 else Color(0.8, 0.45, 0.05))
            c.setLineWidth(0.9)
            c.line(x0, by, x1, by)
            c.line(x0, by - 3, x0, by + 3)
            c.line(x1, by - 3, x1, by + 3)
            c.setFillColor(INK)
            c.setFont("Helvetica", fs)
            c.drawCentredString((x0 + x1) / 2, by - fs - 1.5, f"{core.dim(p['w'], unit)}")
    c.restoreState()
    return ox, oy, iw, ih, k


def _wrap(c, text, font, size, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if c.stringWidth(t, font, size) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _text(c, x, y, text, font="Helvetica", size=9, width=None, lead=None, color=INK):
    c.setFillColor(color)
    c.setFont(font, size)
    lead = lead or size * 1.35
    for line in (_wrap(c, text, font, size, width) if width else [text]):
        c.drawString(x, y, line)
        y -= lead
    return y


def printer_pdf(info, region, pl) -> bytes:
    """Page for the printer: what to print, on which material, and which file is which panel."""
    unit = info.get("unit", "cm")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    m = 36
    c.setTitle(f"Print instructions {info['job_id']}")
    y = H - m
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(m, y - 12, "Print instructions: panels")
    c.setFont("Helvetica", 9.5)
    c.setFillColor(MUTED)
    c.drawRightString(W - m, y - 10, f"Job {info['job_id']}  ·  {info['created'][:10]}")
    y -= 34
    rows = [("Design", info["design"] + (f" ({info['material_code']})" if info.get("material_code") else "")),
            ("Client / reference", info.get("client_ref") or "-"),
            ("Wall", f"{core.dim(info['wall_w'], unit)} x {core.dim(info['wall_h'], unit)}  +  {core.dim(info['allowance'], unit)} allowance"),
            ("Print area", f"{core.dim(pl['width'], unit)} wide x {core.dim(info['height'], unit)} long"),
            ("Material", f"{core.dim(pl['material'], unit)} wide roll, {core.dim(pl['margin'], unit)} left unprinted across the width "
                         f"(printable {core.dim(pl['printable'], unit)})"),
            ("Resolution", f"{info['dpi']:g} dpi"),
            ("Panels", f"{pl['count']} panels, {core.dim(pl['overlap'], unit)} of extra image on each joining side "
                       f"({core.dim(2 * pl['overlap'], unit)} shared between neighbours)")]
    for k_, v in rows:
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(INK)
        c.drawString(m, y, k_)
        y = _text(c, m + 105, y, v, "Helvetica", 9, W - 2 * m - 105)
        y -= 2.5
    y -= 8
    # layout
    lay_h = 205
    draw_layout(c, m, y - lay_h, W - 2 * m, lay_h, region, pl, info["height"], unit)
    y -= lay_h + 14
    # files table
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(INK)
    c.drawString(m, y, "Files")
    y -= 14
    cols = [m, m + 24, m + 262, m + 340, m + 420]
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(MUTED)
    for x_, t in zip(cols, ["#", "File", "Pixels", "Print size", "Position (from left)"]):
        c.drawString(x_, y, t)
    y -= 4
    c.setStrokeColor(MUTED)
    c.setLineWidth(0.4)
    c.line(m, y, W - m, y)
    y -= 11
    for p, f in zip(pl["panels"], info["files"]):
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(cols[0], y, str(p["n"]))
        c.setFont("Helvetica", 7.2)
        c.drawString(cols[1], y, f["name"][-52:] if len(f["name"]) > 52 else f["name"])
        c.setFont("Helvetica", 8.2)
        c.drawString(cols[2], y, f"{f['px'][0]:,} x {f['px'][1]:,}")
        c.drawString(cols[3], y, f"{core.dim(p['w'], unit)} x {core.dim(info['height'], unit)}")
        c.drawString(cols[4], y, f"{core.dim(p['x0'], unit)} to {core.dim(p['x1'], unit)}")
        y -= 12
    y -= 8
    notes = [
        "Print every file at 100% (no scaling or fit-to-page). Each file is already the exact print size.",
        "Panels run top to bottom. The top of the artwork is the top of the file. Panel 1 is the left-most panel when facing the wall, "
        "then 2, 3 ... left to right.",
        f"Neighbouring panels carry {core.dim(pl['overlap'], unit)} of extra image on each joining side (red zones above). "
        "The same artwork appears on both panels there. Please do not trim it.",
        "Please print all panels from the same roll or batch, so the colour matches across the joins.",
        "There are no marks or text inside the image areas. Panel numbers are in the file names.",
    ]
    if info.get("label_strip"):
        notes[4] = (f"Each file has a white strip of {core.dim(LABEL_STRIP_CM, unit)} at the bottom with the panel number. "
                    "It is outside the artwork and is to be trimmed off.")
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(INK)
    c.drawString(m, y, "Notes for the printer")
    y -= 14
    for t in notes:
        c.setFillColor(ACCENT)
        c.circle(m + 3, y + 3, 1.6, stroke=0, fill=1)
        y = _text(c, m + 12, y, t, "Helvetica", 8.6, W - 2 * m - 12) - 3
    c.showPage()
    c.save()
    return buf.getvalue()


def installer_card(info, region, pl, logo_path=None) -> bytes:
    """Card for the packaging: shows the installer how the panels go on the wall."""
    unit = info.get("unit", "cm")
    land = pl["width"] >= info["height"]
    size = landscape(A5) if land else A5
    W, H = size
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    c.setTitle(f"Installer card {info['job_id']}")
    m = 22
    c.setFillColor(ACCENT)
    c.rect(0, H - 34, W, 34, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(m, H - 23, "Panel layout for installation")
    c.setFont("Helvetica", 9)
    c.drawRightString(W - m, H - 22, f"{info['design']}  ·  Job {info['job_id']}")
    foot = 74
    top = H - 34 - 12
    ox, oy, iw, ih, k = draw_layout(c, m, foot, W - 2 * m, top - foot, region, pl, info["height"], unit, brackets=True)
    y = foot - 20
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.6)
    c.drawString(m, y + 14, f"{pl['count']} panels  ·  numbered 1 to {pl['count']} from LEFT to RIGHT, facing the wall  ·  top of the panel is the top of the wall")
    c.setFont("Helvetica", 8.2)
    msg = (f"Wall {core.dim(info['wall_w'], unit)} x {core.dim(info['wall_h'], unit)}; printed {core.dim(pl['width'], unit)} x "
           f"{core.dim(info['height'], unit)} including {core.dim(info['allowance'], unit)} extra around the wall. "
           f"Each joining side carries {core.dim(pl['overlap'], unit)} of extra image: the same artwork appears on both panels in the red zones, "
           "so you can match the artwork on the wall and join the panels your preferred way.")
    _text(c, m, y + 2, msg, "Helvetica", 8.2, W - 2 * m - (70 if logo_path else 0), 10.4, MUTED)
    if logo_path:
        try:
            with Image.open(logo_path) as lg:
                lw = 56.0
                lh = min(lw * lg.height / lg.width, 40)
                c.drawImage(ImageReader(lg.convert("RGBA")), W - m - lw, 18, lw, lh, mask="auto")
        except OSError:
            pass
    c.showPage()
    c.save()
    return buf.getvalue()
