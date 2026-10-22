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
