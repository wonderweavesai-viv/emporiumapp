#!/usr/bin/env python3
"""End-to-end check of the engine: makes sample designs, scans them, edits tags, makes a proof and a print file,
and checks the access rules. Run by the build on every release, and handy on your own computer:

    python3 selftest.py            (or:  "Emporium Print Studio.app/Contents/MacOS/Emporium-Print-Studio" --selftest)

Uses a temporary data folder, so it never touches your real settings or designs.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image


def make_designs(root: Path):
    rng = np.random.default_rng(3)
    specs = [("Florals/Chinoiserie", "egret-garden.png", (1800, 1200), (200, 90, 60)),
             ("Florals", "rose.jpg", (1200, 1800), (200, 60, 90)),
             ("Geometric", "squares.png", (1500, 1500), (40, 90, 200)),
             ("", "loose-file.jpg", (1600, 900), (60, 160, 90))]
    for folder, name, (w, h), col in specs:
        d = root / folder
        d.mkdir(parents=True, exist_ok=True)
        base = np.zeros((h, w, 3), np.uint8)
        base[:] = col
        noise = rng.integers(0, 60, (h // 20 + 1, w // 20 + 1, 1), dtype=np.uint8)
        base = np.clip(base.astype(int) + np.kron(noise, np.ones((20, 20, 1), np.uint8))[:h, :w], 0, 255).astype(np.uint8)
        Image.fromarray(base).save(d / name)


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, ""

    def call(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if "json" in (r.headers.get("Content-Type") or "") else raw), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), e.headers
            except ValueError:
                return e.code, raw, e.headers


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        raise SystemExit(f"Self-test failed: {msg}")


def run():
    home = Path(tempfile.mkdtemp(prefix="emporium-selftest-"))
    os.environ["EMPORIUM_HOME"] = str(home / "data")
    os.environ["EMPORIUM_QUIET"] = "1"
    designs = home / "designs"
    out = home / "prints"
    make_designs(designs)
    import server
    server.init(home / "data")
    server.CFG["port"] = 18080
    port = server.start_background(scan=False)
    base = f"http://127.0.0.1:{port}"
    c = Client(base)
    try:
        print("Access rules")
        st, j, _ = c.call("GET", "/api/config")
        check(st == 200 and not j["authed"] and not j["pin_required"], "nobody is signed in without the owner link")
        check(c.call("GET", "/api/catalog")[0] == 401, "the catalogue is closed without the owner link")
        check(c.call("POST", "/api/print", {})[0] == 401, "printing is closed without the owner link")
        check(c.call("GET", "/api/config", headers={"Host": "evil.example.com"})[0] == 403, "foreign Host header is refused")
        st, _, h = c.call("GET", "/?owner=" + server.OWNER_TOKEN)
        check(st == 200, "owner link accepted")
        c.cookie = "emp_owner=" + server.OWNER_TOKEN
        check(c.call("GET", "/api/config")[1]["owner"] is True, "the app window is the owner")
        check(c.call("POST", "/api/settings", {}, headers={"Origin": "http://evil.example.com"})[0] == 403, "cross-site post is refused")

        print("Settings and scan")
        st, j, _ = c.call("POST", "/api/settings", {"folders": [str(designs), str(home / "nope")]})
        check(st == 400 and "can't find" in j["error"], "a missing folder is refused")
        st, j, _ = c.call("POST", "/api/settings", {"folders": [str(designs)], "output_dir": str(out)})
        check(st == 200 and j["folders"][0]["exists"], "folder and output saved")
        for _ in range(120):
            s = c.call("GET", "/api/scan")[1]
            if not s["running"] and s["finished"]:
                break
            time.sleep(0.5)
        check(not s["running"] and not s["errors"], f"scan finished without errors ({s['new']} new)")
        cat = c.call("GET", "/api/catalog")[1]
        check(len(cat) == 4, "all four designs found, including those in sub-folders")
        by = {a["name"]: a for a in cat}
        check(by["Rose"]["orientation"] == "portrait" and by["Egret Garden"]["orientation"] == "landscape"
              and by["Squares"]["orientation"] == "square", "orientation worked out")
        check(by["Rose"]["category"] == "Florals" and by["Squares"]["category"] == "Geometric"
              and by["Loose File"]["category"] == "Uncategorised", "categories come from the top-level folder")
        check(by["Egret Garden"]["styles"] == ["chinoiserie"], "deeper folder becomes a style tag")
        check("path" not in cat[0] and str(designs) not in json.dumps(cat), "file paths are not sent to the browser")
        check(c.call("GET", "/" + by["Rose"]["thumb"])[0] == 200, "thumbnail served")

        print("Editing tags")
        rose = by["Rose"]["id"]
        st, _, _ = c.call("POST", "/api/artwork", {"id": rose, "name": "Rose Garden", "material": "D-200",
                                                   "styles": "floral, romantic", "colors": ["Red & pink"], "notes": "hero"})
        check(st == 200, "details saved")
        c.call("POST", "/api/artworks/bulk", {"ids": [rose, by["Squares"]["id"]], "add_styles": ["bestseller"]})
        cat = {a["id"]: a for a in c.call("GET", "/api/catalog")[1]}
        check(cat[rose]["name"] == "Rose Garden" and cat[rose]["styles"] == ["floral", "romantic", "bestseller"]
              and cat[rose]["colors"] == ["Red & pink"], "edits and bulk tag stored")
        # rescan must keep hand-made tags, and a renamed file keeps them too
        os.rename(designs / "Florals" / "rose.jpg", designs / "Geometric" / "rose-renamed.jpg")
        c.call("POST", "/api/scan", {})
        time.sleep(0.5)
        for _ in range(120):
            s = c.call("GET", "/api/scan")[1]
            if not s["running"]:
                break
            time.sleep(0.5)
        cat = {a["id"]: a for a in c.call("GET", "/api/catalog")[1]}
        check(rose in cat and cat[rose]["name"] == "Rose Garden" and cat[rose]["category"] == "Geometric",
              "a renamed or moved file keeps its tags")
        check(len(cat) == 4, "no duplicates after the move")

        print("Proof and print file")
        art = cat[rose]
        payload = {"artwork_id": rose, "wall_w": 20, "wall_h": 30, "allowance": 10, "dpi": 96, "unit": "in",
                   "recipe": {"colour": {"saturation": 10}, "hsl": {"mode": "hsl", "master": {"hue": 20}, "ranges": []}},
                   "approved": True, "client_ref": "TEST", "created_by": "selftest"}
        st, pdf, _ = c.call("POST", "/api/proof", payload)
        check(st == 200 and pdf[:4] == b"%PDF", "proof PDF made")
        check(any((out / "Proofs").glob("*.pdf")), "proof saved in the print folder")
        st, job, _ = c.call("POST", "/api/print", payload)
        check(st == 202, "print job accepted")
        for _ in range(240):
            job = c.call("GET", "/api/jobs/" + job["id"])[1]
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        check(job["status"] == "done", "print job finished")
        tiff = next(out.rglob("*_print.tiff"))
        with Image.open(tiff) as im:
            check(im.size == (round(30 / 2.54 * 96), round(40 / 2.54 * 96)), f"print file has the exact pixel size {im.size}")
            check(im.info.get("dpi", (0, 0))[0] in (96, 95.9999, 96.0), "print file carries 96 dpi")
        check((tiff.parent / "job.txt").read_text().count("HSL all colours") == 1, "job sheet lists the colour edit")
        c.call("POST", "/api/open", {"kind": "job", "folder": job["result"]["folder"]})

        print("Image default resolution")
        native = dict(payload, dpi="native", recipe={})
        st, job, _ = c.call("POST", "/api/print", native)
        check(st == 202 and abs(job["dpi"] - 101.6) < 0.05, f"image default works out the artwork's own dpi ({job.get('dpi')})")
        for _ in range(240):
            job = c.call("GET", "/api/jobs/" + job["id"])[1]
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        check(job["status"] == "done" and job["result"]["px"] == [1200, 1600], f"image default file has no resampling {job['result']['px']}")
        check("own resolution" in (out / job["result"]["folder"] / "job.txt").read_text(), "job sheet says image default")

        print("Texture layer")
        tdir = home / "textures"
        (tdir / "Paper").mkdir(parents=True)
        Image.fromarray(np.full((300, 400, 3), 128, np.uint8)).save(tdir / "Paper" / "grey-paper.png")
        Image.fromarray(np.full((200, 200, 3), 230, np.uint8)).save(tdir / "linen_weave.jpg")
        (tdir / "notes.txt").write_text("not an image")
        check(c.call("GET", "/api/textures")[1] == [], "no textures before a folder is chosen")
        check(c.call("POST", "/api/settings", {"texture_dir": str(home / "nope")})[0] == 400, "a missing texture folder is refused")
        st, j, _ = c.call("POST", "/api/settings", {"texture_dir": str(tdir)})
        check(st == 200 and j["texture_count"] == 2, "texture folder saved, 2 textures found (text file ignored)")
        texs = {t["name"]: t for t in c.call("GET", "/api/textures")[1]}
        check(set(texs) == {"Grey Paper", "Linen Weave"} and texs["Grey Paper"]["folder"] == "Paper", "textures listed with names and folders")
        check(str(tdir) not in json.dumps(texs), "texture file paths are not sent to the browser")
        check(c.call("GET", "/" + texs["Grey Paper"]["thumb"])[0] == 200 and c.call("GET", "/" + texs["Grey Paper"]["preview"])[0] == 200,
              "texture thumbnail and preview served")
        # the maths, on a tiny wall where every pixel is known (texture: 4 px white then 4 px grey)
        import core, render
        tex = np.full((4, 8, 3), 255, np.uint8)
        tex[:, 4:] = 128
        art = Image.new("RGB", (100, 100), (200, 100, 50))
        info = lambda tid: (tdir / "x.png", "Test", 8, 4)
        base = {"art_w": 10, "off_x": 0, "off_y": 0}
        def row(texture):
            r = core.check_recipe(dict(base, texture=texture), 100, 100, 10, 10, info)
            return np.asarray(render.render(art, r, 10, 10, 20, 20, texture=Image.fromarray(tex)))[5]
        rep = row({"id": "a", "scale": 4, "mode": "repeat", "opacity": 100})
        check(tuple(rep[1]) == (200, 100, 50) and tuple(rep[6]) == (100, 50, 25) and tuple(rep[9]) == (200, 100, 50) and tuple(rep[14]) == (100, 50, 25),
              "multiply, repeat: white leaves the artwork alone, grey darkens it, and the tile repeats")
        mir = row({"id": "a", "scale": 4, "mode": "mirror", "opacity": 100})
        check(tuple(mir[1]) == (200, 100, 50) and tuple(mir[6]) == (100, 50, 25) and tuple(mir[9]) == (100, 50, 25) and tuple(mir[14]) == (200, 100, 50),
              "multiply, mirror: every second tile is flipped")
        half = row({"id": "a", "scale": 4, "mode": "repeat", "opacity": 50})
        check(tuple(half[6]) == (150, 75, 37) or tuple(half[6]) == (150, 75, 38), f"opacity 50 is halfway {tuple(half[6])}")
        check(tuple(row({"id": "a", "scale": 4, "mode": "repeat", "opacity": 0})[6]) == (200, 100, 50), "opacity 0 changes nothing")
        # the whole flow
        tp = dict(payload, recipe={"texture": {"id": texs["Grey Paper"]["id"], "scale": 30, "mode": "mirror", "opacity": 60}})
        st, pdf, _ = c.call("POST", "/api/proof", tp)
        check(st == 200 and pdf[:4] == b"%PDF", "proof with a texture")
        bad = dict(payload, recipe={"texture": {"id": "doesnotexist", "scale": 30}})
        check(c.call("POST", "/api/proof", bad)[0] == 400, "an unknown texture is refused")
        st, job, _ = c.call("POST", "/api/print", tp)
        check(st == 202, "print job with a texture accepted")
        for _ in range(240):
            job = c.call("GET", "/api/jobs/" + job["id"])[1]
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        check(job["status"] == "done", "print job with a texture finished")
        sheet = (out / job["result"]["folder"] / "job.txt").read_text()
        check("EDITED VERSION" in sheet and 'Texture "Grey Paper"' in sheet and "opacity 60%" in sheet, "job sheet lists the texture and marks it EDITED")
        flat = next((out / job["result"]["folder"]).glob("*_print.tiff"))
        with Image.open(flat) as im, Image.open(tiff) as ref:
            check(np.asarray(im).mean() < np.asarray(ref).mean() - 10, "the printed file really is darker with the texture multiplied in")

        print("Brick and drop tiling")
        cell = np.arange(64, dtype=np.uint8).reshape(8, 8)
        unit_img = Image.fromarray(np.dstack([cell * 3] * 3))      # 64 different values, so every move can be checked
        yy, xx = np.mgrid[0:32, 0:32]
        def tiled(mode, off=(0, 0)):
            r = core.check_recipe({"tile": mode, "art_w": 5, "off_x": off[0], "off_y": off[1]}, 8, 8, 20, 20)
            return np.asarray(render.render(unit_img, r, 20, 20, 32, 32))[:, :, 0] // 3, r
        for mode, ax, den in [("half_v", "v", 2), ("quarter_v", "v", 4), ("half_h", "h", 2), ("quarter_h", "h", 4), ("brick", "h", 2)]:
            got, r = tiled(mode)
            if ax == "v":
                col = xx // 8
                want = ((yy - (col % den) * 8 // den) % 8) * 8 + xx % 8
            else:
                rw = yy // 8
                want = (yy % 8) * 8 + (xx - (rw % den) * 8 // den) % 8
            check((got == want).all(), f"{mode}: every pixel is where a {'column drop' if ax == 'v' else 'row shift'} of 1/{den} puts it")
        def custom(mode, pct):
            r = core.check_recipe({"tile": mode, "drop": pct, "art_w": 5, "off_x": 0, "off_y": 0}, 8, 8, 20, 20)
            return np.asarray(render.render(unit_img, r, 20, 20, 32, 32))[:, :, 0] // 3, r
        for mode, pct in [("custom_v", 37), ("custom_h", 62.5), ("custom_v", 100), ("custom_h", 0)]:
            got, r = custom(mode, pct)
            dn = int(round(pct * 100))
            if mode == "custom_v":
                want = ((yy - (xx // 8) * dn * 8 // 10000) % 8) * 8 + xx % 8
            else:
                want = (yy % 8) * 8 + (xx - (yy // 8) * dn * 8 // 10000) % 8
            check((got == want).all(), f"custom drop {pct}% {mode[-1]}: every pixel is where it should be")
        check((custom("custom_v", 50)[0] == tiled("half_v")[0]).all(), "custom drop 50% gives exactly the half drop")
        check((custom("custom_h", 25)[0] == tiled("quarter_h")[0]).all(), "custom drop 25% gives exactly the quarter drop")
        check((custom("custom_v", 100)[0] == tiled("repeat")[0]).all(), "custom drop 100% is a plain repeat")
        check(core.describe_edits(custom("custom_v", 37)[1])[0].startswith("Custom drop 37% (vertical) tile"), "edits list gives the custom amount")
        check(custom("custom_v", 250)[1]["drop"] == 100, "drop amount is kept between 0 and 100")
        plain, _ = tiled("repeat")
        check((plain == ((yy % 8) * 8 + xx % 8)).all(), "plain repeat still lines up")
        check(core.describe_edits(tiled("quarter_h")[1])[0].startswith("Quarter drop (horizontal) tile"), "edits list names the layout")
        st, pdf, _ = c.call("POST", "/api/proof", dict(payload, recipe={"tile": "half_v", "art_w": 15}))
        check(st == 200 and pdf[:4] == b"%PDF", "proof with a half drop")
        check(c.call("POST", "/api/proof", dict(payload, recipe={"tile": "wobbly"}))[0] == 400, "an unknown tiling is refused")

        print("Office network")
        st, j, _ = c.call("POST", "/api/settings", {"share_network": True})
        check(st == 400, "sharing is refused until a PIN is set")
        st, j, _ = c.call("POST", "/api/settings", {"access_pin": "12"})
        check(st == 400, "a short PIN is refused")
        st, j, _ = c.call("POST", "/api/settings", {"access_pin": "4821", "share_network": True})
        check(st == 200 and j["sharing_active"], "sharing switched on with a PIN")
        time.sleep(1.5)
        team = Client(f"http://127.0.0.1:{server.RUNTIME['port']}")
        check(team.call("GET", "/api/catalog")[0] == 401, "a team member needs the PIN")
        check(team.call("POST", "/api/login", {"pin": "0000"})[0] == 403, "wrong PIN refused")
        st, _, h = team.call("POST", "/api/login", {"pin": "4821"})
        check(st == 200, "right PIN accepted")
        team.cookie = h["Set-Cookie"].split(";")[0]
        check(team.call("GET", "/api/catalog")[0] == 200, "team member can browse")
        check(team.call("GET", "/api/textures")[0] == 200, "team members can use textures")
        check(team.call("GET", "/api/settings")[0] == 403 and team.call("POST", "/api/artwork", {"id": rose, "name": "x"})[0] == 403,
              "team member cannot change settings or tags")
        check(server.RUNTIME["host"] == "0.0.0.0", "server listens on the office network")
        c2 = Client(f"http://127.0.0.1:{server.RUNTIME['port']}")
        c2.cookie = "emp_owner=" + server.OWNER_TOKEN
        c2.call("POST", "/api/settings", {"share_network": False})
        time.sleep(1.5)
        check(server.RUNTIME["host"] == "127.0.0.1", "sharing switched off again")
        print("\nAll checks passed.")
    finally:
        server.LISTENER and server.LISTENER.stop()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    run()
