#!/usr/bin/env python3
"""Render engine: one "recipe" describes every edit, and this module turns it into pixels.

The same recipe drives the phone preview (JavaScript, which mirrors the colour maths below), the client proof
(rendered here from the preview image) and the print file (rendered here from the full-size master, in strips
so that a 200-megapixel wall does not need several copies of itself in memory).

Recipe fields
    flip_h, flip_v   flip the finished wall picture
    tile             "off", "repeat" or "mirror"
    art_w            width of the whole artwork on the wall, in cm (the physical "zoom")
    off_x, off_y     where the wall's top-left corner sits on the artwork plane, in cm
    colour           brightness, contrast, saturation, vibrance, hue, warmth, bw, sepia, fade
    hsl              detailed colour-range editor: mode (hsl or hsv), a master shift for all colours, and six
                     colour ranges, each defined by four hue-wheel nodes and a hue, saturation and luminosity shift
"""
import numpy as np
from PIL import Image, ImageOps

COLOUR_DEFAULT = dict(brightness=0, contrast=0, saturation=0, vibrance=0, hue=0, warmth=0, bw=0, sepia=0, fade=0)
COLOUR_RANGE = dict(brightness=(-100, 100), contrast=(-100, 100), saturation=(-100, 100), vibrance=(-100, 100),
                    hue=(-180, 180), warmth=(-100, 100), bw=(0, 100), sepia=(0, 100), fade=(0, 100))
LUM = np.array([0.213, 0.715, 0.072])
SEPIA = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]])


def colour_is_neutral(c):
    return all(float(c.get(k, 0)) == 0 for k in COLOUR_DEFAULT)


def hue_matrix(deg):
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([
        [0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928],
        [0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283],
        [0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072]])


def sat_matrix(s):
    return np.array([
        [0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
        [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
        [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s]])


HSL_NAMES = ["Reds", "Yellows", "Greens", "Cyans", "Blues", "Magentas"]
HSL_GATE = (0.03, 0.12)  # colour ranges ignore near-greys: no effect below the first value, full effect above the second


def hsl_default():
    return {"mode": "hsl", "master": {"hue": 0.0, "sat": 0.0, "lum": 0.0},
            "ranges": [{"a": [c - 45.0, c - 15.0, c + 15.0, c + 45.0], "hue": 0.0, "sat": 0.0, "lum": 0.0}
                       for c in (0.0, 60.0, 120.0, 180.0, 240.0, 300.0)]}


def hsl_active(h):
    """The master and the ranges that actually change something."""
    out = []
    m = h["master"]
    if m["hue"] or m["sat"] or m["lum"]:
        out.append((None, m))
    for r in h["ranges"]:
        if r["hue"] or r["sat"] or r["lum"]:
            out.append((r["a"], r))
    return out


def hsl_is_neutral(h):
    return not h or not hsl_active(h)


def check_hsl(src):
    """Validate and normalise the HSL block of a recipe. Raises ValueError with a message fit for a salesperson."""
    base = hsl_default()
    if not src:
        return base
    bad = ValueError("The colour range settings are not valid.")
    if not isinstance(src, dict):
        raise bad
    mode = src.get("mode", "hsl")
    if mode not in ("hsl", "hsv"):
        raise bad

    def num(v, lo, hi):
        v = float(v or 0)
        if v != v or v in (float("inf"), float("-inf")):
            raise bad
        return min(hi, max(lo, v))

    try:
        m = src.get("master") or {}
        if not isinstance(m, dict):
            raise bad
        master = {"hue": num(m.get("hue"), -180, 180), "sat": num(m.get("sat"), -100, 100), "lum": num(m.get("lum"), -100, 100)}
        given = src.get("ranges") if isinstance(src.get("ranges"), list) else []
        if len(given) > 6:
            raise bad
        ranges = []
        for i in range(6):
            d = given[i] if i < len(given) and isinstance(given[i], dict) else {}
            a = d.get("a") or base["ranges"][i]["a"]
            if len(a) != 4:
                raise bad
            a = sorted(num(x, -1e5, 1e5) for x in a)
            a[3] = min(a[3], a[0] + 360.0)  # a range can cover at most the whole wheel
            a[1], a[2] = min(a[1], a[3]), min(a[2], a[3])
            ranges.append({"a": a, "hue": num(d.get("hue"), -180, 180), "sat": num(d.get("sat"), -100, 100),
                           "lum": num(d.get("lum"), -100, 100)})
    except (TypeError, ValueError, AttributeError):
        raise bad
    return {"mode": mode, "master": master, "ranges": ranges}


def _smooth(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def range_weight(hue, a):
    """How strongly a colour with this hue (degrees) belongs to a range with nodes a0 <= a1 <= a2 <= a3.
    Full effect between a1 and a2, fading smoothly to nothing at a0 and a3. Mirrored exactly in the JavaScript preview."""
    a0, a1, a2, a3 = a
    x = np.mod(hue - a0, 360.0) + a0
    up = _smooth((x - a0) / max(a1 - a0, 1e-6))
    down = _smooth((a3 - x) / max(a3 - a2, 1e-6))
    w = np.where(x < a1, up, np.where(x <= a2, 1.0, down))
    return np.where(x >= a3, 0.0, w)


def apply_hsl(a, h):
    """a: float32 array (..., 3), 0..1. Shifts hue, saturation and luminosity of all colours (master) and of colours
    inside each range. In "hsl" mode luminosity mixes towards white or black; in "hsv" mode it scales brightness.
    The maths is mirrored exactly in the JavaScript preview. Change both or neither."""
    active = hsl_active(h)
    if not active:
        return a
    hsv = h["mode"] == "hsv"
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    c = mx - mn
    sc = np.where(c > 0, c, 1.0)
    hue = np.where(mx == r, np.mod((g - b) / sc, 6.0), np.where(mx == g, (b - r) / sc + 2.0, (r - g) / sc + 4.0)) * 60.0
    hue = np.where(c > 0, hue, 0.0)
    light = (mx + mn) * 0.5
    den = 1.0 - np.abs(2.0 * light - 1.0)
    s_hsl = np.where(den > 1e-6, c / np.where(den > 1e-6, den, 1.0), 0.0)
    gate = _smooth((s_hsl - HSL_GATE[0]) / (HSL_GATE[1] - HSL_GATE[0]))

    dh = np.zeros(mx.shape, np.float32)
    ds = np.zeros(mx.shape, np.float32)
    dl = np.zeros(mx.shape, np.float32)
    for nodes, d in active:
        w = 1.0 if nodes is None else range_weight(hue, nodes) * gate
        dh += w * d["hue"]
        ds += w * d["sat"]
        dl += w * d["lum"]
    ds = np.clip(ds, -100.0, 100.0) / 100.0
    dl = np.clip(dl, -100.0, 100.0) / 100.0

    if hsv:
        s0 = np.where(mx > 0, c / np.where(mx > 0, mx, 1.0), 0.0)
        x0 = mx
    else:
        s0 = np.clip(s_hsl, 0.0, 1.0)
        x0 = light
    s1 = np.where(ds > 0, 1.0 - np.power(np.maximum(1.0 - s0, 0.0), np.maximum(1.0 + 3.0 * ds, 1.0)), s0 * (1.0 + ds))
    x1 = np.where(dl > 0, x0 + (1.0 - x0) * dl, x0 * (1.0 + dl))
    h1 = np.mod(hue + dh, 360.0)

    if hsv:
        def f(n):
            k = np.mod(n + h1 / 60.0, 6.0)
            return x1 - x1 * s1 * np.maximum(0.0, np.minimum(np.minimum(k, 4.0 - k), 1.0))
        out = np.stack([f(5.0), f(3.0), f(1.0)], axis=-1)
    else:
        amp = s1 * np.minimum(x1, 1.0 - x1)

        def f(n):
            k = np.mod(n + h1 / 30.0, 12.0)
            return x1 - amp * np.maximum(-1.0, np.minimum(np.minimum(k - 3.0, 9.0 - k), 1.0))
        out = np.stack([f(0.0), f(8.0), f(4.0)], axis=-1)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def apply_colour(a, c, hsl=None):
    """a: float32 array (..., 3) with values 0..1. Returns a new float32 array. The order of the steps and the
    places where values are clamped are mirrored exactly in the JavaScript preview. Change both or neither.
    The HSL colour ranges run first, so a colour picked from the artwork is matched against the original colours."""
    g = lambda k: float(c.get(k, 0))
    a = a.astype(np.float32, copy=True)
    if hsl and not hsl_is_neutral(hsl):
        a = apply_hsl(a, hsl)
    if g("brightness"):
        a += g("brightness") / 250.0
    if g("contrast"):
        a = (a - 0.5) * (1.0 + g("contrast") / 100.0) + 0.5
    if g("brightness") or g("contrast"):
        np.clip(a, 0, 1, out=a)
    if g("warmth"):
        w = g("warmth") / 400.0
        a[..., 0] *= 1.0 + w
        a[..., 2] *= 1.0 - w
        np.clip(a, 0, 1, out=a)
    if g("hue") or g("saturation"):
        m = (sat_matrix(1.0 + g("saturation") / 100.0) @ hue_matrix(g("hue"))).astype(np.float32)
        a = a @ m.T
        np.clip(a, 0, 1, out=a)
    if g("vibrance"):
        gray = (a @ LUM.astype(np.float32))[..., None]
        sat = (a.max(-1) - a.min(-1))[..., None]
        f = 1.0 + g("vibrance") / 100.0 * (1.0 - sat)
        a = gray + (a - gray) * f
        np.clip(a, 0, 1, out=a)
    if g("bw"):
        gray = (a @ LUM.astype(np.float32))[..., None]
        a = a + (gray - a) * (g("bw") / 100.0)
    if g("sepia"):
        s = a @ SEPIA.T.astype(np.float32)
        a = a + (s - a) * (g("sepia") / 100.0)
        np.clip(a, 0, 1, out=a)
    if g("fade"):
        t = g("fade") / 100.0 * 0.8
        a = a * (1.0 - t) + t
    np.clip(a, 0, 1, out=a)
    return a


def colour_image(img, c, hsl=None):
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    out = apply_colour(arr, c, hsl)
    return Image.fromarray(np.rint(out * 255.0).astype(np.uint8), "RGB")


def _clamp(v, lo, hi):
    return min(hi, max(lo, v))


def _tile_index(start, n, size, mirror):
    i = start + np.arange(n)
    if mirror:
        m = i % (2 * size)
        return np.where(m < size, m, 2 * size - 1 - m)
    return i % size


# brick / drops: every next row (brick, horizontal drops) shifts sideways, or every next column (vertical drops)
# shifts down, by a half or a quarter of the repeat, so the pattern only repeats after 2 or 4 rows / columns
DROP_MODES = {"brick": ("h", 2), "half_v": ("v", 2), "half_h": ("h", 2), "quarter_v": ("v", 4), "quarter_h": ("h", 4)}


def drop_unit(unit, ax, den):
    """Brick / drop layouts: a larger repeating block made of `den` copies of the unit, each next copy moved by 1/den.
    ax 'v': neighbouring columns drop down; ax 'h': neighbouring rows shift sideways."""
    uh, uw = unit.shape[:2]
    if ax == "v":
        out = np.empty((uh, uw * den, 3), np.uint8)
        for k in range(den):
            out[:, k * uw:(k + 1) * uw] = np.roll(unit, (k * uh) // den, axis=0)
    else:
        out = np.empty((uh * den, uw, 3), np.uint8)
        for k in range(den):
            out[k * uh:(k + 1) * uh] = np.roll(unit, (k * uw) // den, axis=1)
    return out


def texture_unit(tex_img, spec, pxx, pxy):
    """One copy of the texture at the size it has on the wall, as a numpy array."""
    uw = max(1, round(spec["scale"] * pxx))
    uh = max(1, round(spec["scale"] * tex_img.height / tex_img.width * pxy))
    return np.asarray(tex_img.resize((uw, uh), Image.LANCZOS))


def multiply_texture(strip, unit, cols, spec, y0, y1):
    """Multiply the texture over a strip of the wall. Opacity 0 = no change, 100 = full multiply."""
    uh = unit.shape[0]
    rows = _tile_index(y0, y1 - y0, uh, spec["mode"] == "mirror")
    t = unit[rows[:, None], cols[None, :]].astype(np.float32) / 255.0
    k = spec["opacity"] / 100.0
    a = np.asarray(strip, dtype=np.float32)
    a *= 1.0 - k * (1.0 - t)
    return Image.fromarray(np.rint(a).clip(0, 255).astype(np.uint8), "RGB")


def render(src, recipe, jw, jh, out_w, out_h, strip_px=4_000_000, progress=None, colour=True, texture=None):
    """Render the wall (jw x jh cm) from the image `src` into an out_w x out_h RGB picture.

    The picture is built in horizontal strips. Each strip is resampled straight from the source with Pillow's
    `box` argument, which lets the filter see the neighbouring rows, so there are no seams between strips."""
    sw, sh = src.size
    aw = recipe["art_w"]
    ah = aw * sh / sw
    pxx, pxy = out_w / jw, out_h / jh
    tile = recipe.get("tile", "off")
    flip_h, flip_v = bool(recipe.get("flip_h")), bool(recipe.get("flip_v"))
    col = recipe.get("colour") or {}
    hsl = recipe.get("hsl")
    do_colour = colour and (not colour_is_neutral(col) or not hsl_is_neutral(hsl))

    if tile == "off":
        bl = recipe["off_x"] / aw * sw
        bt = recipe["off_y"] / ah * sh
        bw = jw / aw * sw
        bh = jh / ah * sh

        def get(y0, y1):
            t0, t1 = bt + bh * y0 / out_h, bt + bh * y1 / out_h
            box = (_clamp(bl, 0, sw), _clamp(t0, 0, sh), _clamp(bl + bw, 0, sw), _clamp(t1, 0, sh))
            return src.resize((out_w, y1 - y0), Image.LANCZOS, box=box)
    else:
        uw, uh = max(1, round(aw * pxx)), max(1, round(ah * pxy))
        unit = np.asarray(src.resize((uw, uh), Image.LANCZOS))
        if tile in DROP_MODES:
            unit = drop_unit(unit, *DROP_MODES[tile])
            uh, uw = unit.shape[:2]
        ox, oy = round(recipe["off_x"] * pxx), round(recipe["off_y"] * pxy)

        cols = _tile_index(ox, out_w, uw, tile == "mirror")
        dnum = int(round(float(recipe.get("drop") or 0) * 100))     # custom drop, in 1/10000 of a repeat

        def get(y0, y1):
            if tile in ("custom_v", "custom_h"):
                ys = oy + y0 + np.arange(y1 - y0, dtype=np.int64)
                xs = ox + np.arange(out_w, dtype=np.int64)
                if tile == "custom_v":       # each next column drops a little more
                    shift = (xs // uw) * dnum * uh // 10000 % uh
                    return Image.fromarray(unit[(ys[:, None] - shift[None, :]) % uh, (xs % uw)[None, :]], "RGB")
                shift = (ys // uh) * dnum * uw // 10000 % uw     # each next row moves a little more
                return Image.fromarray(unit[(ys % uh)[:, None], (xs[None, :] - shift[:, None]) % uw], "RGB")
            rows = _tile_index(oy + y0, y1 - y0, uh, tile == "mirror")
            return Image.fromarray(unit[rows[:, None], cols[None, :]], "RGB")

    tspec = recipe.get("texture")
    tunit = tcols = None
    if texture is not None and tspec and tspec.get("opacity", 0) > 0:
        tunit = texture_unit(texture, tspec, pxx, pxy)
        tcols = _tile_index(0, out_w, tunit.shape[1], tspec["mode"] == "mirror")

    final = Image.new("RGB", (out_w, out_h))
    step = max(16, strip_px // max(1, out_w))
    for y0 in range(0, out_h, step):
        y1 = min(out_h, y0 + step)
        strip = get(out_h - y1, out_h - y0) if flip_v else get(y0, y1)
        if flip_v:
            strip = ImageOps.flip(strip)
        if flip_h:
            strip = ImageOps.mirror(strip)
        if do_colour:
            strip = colour_image(strip, col, hsl)
        if tunit is not None:      # the texture lies on the wall; the artwork is multiplied over it
            strip = multiply_texture(strip, tunit, tcols, tspec, y0, y1)
        final.paste(strip, (0, y0))
        if progress:
            progress(y1 / out_h)
    return final
