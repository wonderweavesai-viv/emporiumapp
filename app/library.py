#!/usr/bin/env python3
"""Artwork library: scans your design folders, makes thumbnails and previews, tags colours and orientation,
and keeps everything (including the tags you edit by hand) in a small SQLite database.

Your artwork folders are only ever READ. Nothing is written into them.
Everything this module creates lives in the app's own data folder:
    catalog.db        one row per artwork (what the app knows about it, plus your edits)
    cache/thumbs/     small images for browsing
    cache/previews/   medium images used for the live preview and the proof
Safe to delete the cache: it is rebuilt on the next scan. Deleting catalog.db loses your hand-made tags.
"""
import json
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
THUMB_W = 520
PREVIEW_LONG = 1800
UNCATEGORISED = "Uncategorised"

COLOR_SWATCH = {
    "White & cream": "#f3efe6",
    "Beige & sand": "#d9c7a8",
    "Grey": "#9aa0a3",
    "Charcoal & black": "#2b2d2f",
    "Brown": "#7a5a40",
    "Gold & yellow": "#d4a72c",
    "Orange & terracotta": "#c4623a",
    "Red & pink": "#cf5c6e",
    "Purple": "#7a5fa0",
    "Blue": "#3d6fa8",
    "Green & teal": "#3d8a7a",
}
NEUTRAL = {"White & cream", "Beige & sand", "Grey", "Charcoal & black"}
FAMILIES = list(COLOR_SWATCH)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def classify(h, s, v):
    """h in degrees, s and v in 0..1 -> colour family name."""
    if v < 0.22:
        return "Charcoal & black"
    if s < 0.10:
        return "White & cream" if v > 0.86 else "Grey"
    if 15 <= h < 65 and s < 0.18 and v > 0.86:
        return "White & cream"
    if 8 <= h < 65 and v < 0.55:
        return "Brown"
    if 15 <= h < 65 and s < 0.42:
        return "Beige & sand"
    if 40 <= h < 68 and s >= 0.42:
        return "Gold & yellow"
    if 8 <= h < 40 and s >= 0.42:
        return "Orange & terracotta"
    if h >= 330 or h < 8:
        return "Red & pink"
    if 68 <= h < 195:
        return "Green & teal"
    if 195 <= h < 265:
        return "Blue"
    if 265 <= h < 330:
        return "Purple"
    return "Beige & sand"


def auto_colors(img: Image.Image):
    small = img.convert("RGB")
    small.thumbnail((120, 120))
    hsv = np.asarray(small.convert("HSV"), dtype=np.float32).reshape(-1, 3)
    h = hsv[:, 0] * 360.0 / 255.0
    s = hsv[:, 1] / 255.0
    v = hsv[:, 2] / 255.0
    counts = {}
    for hh, ss, vv in zip(h, s, v):
        fam = classify(hh, ss, vv)
        counts[fam] = counts.get(fam, 0) + 1
    total = sum(counts.values())
    tags = []
    for fam, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        share = c / total
        if share >= 0.15 or (fam not in NEUTRAL and share >= 0.04):
            tags.append(fam)
    return tags[:4]


def orientation(w, h):
    if max(w, h) and abs(w - h) / max(w, h) < 0.03:
        return "square"
    return "portrait" if h > w else "landscape"


def clean_list(v, limit=40, lower=False):
    """Tags from the editor: a list or a comma separated string -> tidy list without duplicates."""
    if isinstance(v, str):
        v = v.replace("|", ",").split(",")
    out = []
    for x in v or []:
        x = " ".join(str(x).split())[:60]
        if lower:
            x = x.lower()
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:limit]


SCHEMA = """
CREATE TABLE IF NOT EXISTS artworks (
    id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    root TEXT NOT NULL,
    rel TEXT NOT NULL,
    category TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    w INTEGER NOT NULL, h INTEGER NOT NULL,
    orient TEXT NOT NULL,
    auto_colors TEXT NOT NULL DEFAULT '[]',
    thumb_w INTEGER, thumb_h INTEGER, preview_w INTEGER, preview_h INTEGER,
    name TEXT NOT NULL,
    material TEXT NOT NULL DEFAULT '',
    styles TEXT NOT NULL DEFAULT '[]',
    colors TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    added INTEGER NOT NULL,
    missing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_art_sig ON artworks (size, w, h);
"""


class Library:
    def __init__(self, data_dir: Path):
        self.data = Path(data_dir)
        self.cache = self.data / "cache"
        (self.cache / "thumbs").mkdir(parents=True, exist_ok=True)
        (self.cache / "previews").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.data / "catalog.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.scan_state = {"running": False, "done": 0, "total": 0, "current": "", "errors": [], "finished": None,
                           "notes": [], "new": 0, "changed": 0, "missing": 0}
        self._cancel = False
        self._claimed = set()

    # ------------------------------------------------------------------ reading
    @staticmethod
    def _row(r):
        styles = json.loads(r["styles"])
        manual = json.loads(r["colors"])
        return {
            "id": r["id"], "name": r["name"], "material": r["material"], "category": r["category"],
            "styles": styles, "colors": manual or json.loads(r["auto_colors"]), "manual_colors": bool(manual),
            "orientation": r["orient"], "notes": r["notes"], "folder": r["rel"],
            "w": r["w"], "h": r["h"], "added": r["added"],
            "thumb": f"media/thumbs/{r['id']}.jpg", "thumb_w": r["thumb_w"], "thumb_h": r["thumb_h"],
            "preview": f"media/previews/{r['id']}.jpg", "preview_w": r["preview_w"], "preview_h": r["preview_h"],
        }

    def catalog(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM artworks WHERE missing=0 ORDER BY category COLLATE NOCASE, name COLLATE NOCASE").fetchall()
        return [self._row(r) for r in rows]

    def get(self, art_id):
        """The catalogue entry plus the absolute path of the master. For the server's own use, never sent to browsers."""
        with self.lock:
            r = self.db.execute("SELECT * FROM artworks WHERE id=? AND missing=0", (art_id,)).fetchone()
        if not r:
            return None
        d = self._row(r)
        d["path"] = r["path"]
        return d

    def counts(self):
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM artworks WHERE missing=0").fetchone()[0]
            m = self.db.execute("SELECT COUNT(*) FROM artworks WHERE missing=1").fetchone()[0]
        return {"artworks": n, "missing": m}

    # ------------------------------------------------------------------ editing (your tags are never overwritten by a scan)
    def update(self, art_id, fields):
        sets, vals = [], []
        if "name" in fields:
            name = " ".join(str(fields["name"]).split())[:120]
            if not name:
                raise ValueError("The name cannot be empty.")
            sets.append("name=?"); vals.append(name)
        if "material" in fields:
            sets.append("material=?"); vals.append(" ".join(str(fields["material"]).split())[:60])
        if "styles" in fields:
            sets.append("styles=?"); vals.append(json.dumps(clean_list(fields["styles"], lower=True)))
        if "colors" in fields:
            cols = [c for c in clean_list(fields["colors"]) if c in COLOR_SWATCH]
            sets.append("colors=?"); vals.append(json.dumps(cols))
        if "notes" in fields:
            sets.append("notes=?"); vals.append(str(fields["notes"])[:500])
        if not sets:
            return
        with self.lock:
            cur = self.db.execute(f"UPDATE artworks SET {', '.join(sets)} WHERE id=?", vals + [art_id])
            self.db.commit()
        if cur.rowcount == 0:
            raise ValueError("That design is not in the library.")

    def bulk_styles(self, ids, add=(), remove=()):
        add, remove = clean_list(add, lower=True), set(clean_list(remove, lower=True))
        with self.lock:
            for art_id in ids:
                r = self.db.execute("SELECT styles FROM artworks WHERE id=?", (art_id,)).fetchone()
                if not r:
                    continue
                cur = [s for s in json.loads(r["styles"]) if s not in remove]
                for a in add:
                    if a not in cur:
                        cur.append(a)
                self.db.execute("UPDATE artworks SET styles=? WHERE id=?", (json.dumps(cur), art_id))
            self.db.commit()

    # ------------------------------------------------------------------ scanning
    def cancel(self):
        self._cancel = True

    def start_scan(self, folders, background=True):
        if self.scan_state["running"]:
            return False
        self.scan_state.update(running=True, done=0, total=0, current="Looking for designs", errors=[], notes=[],
                               finished=None, new=0, changed=0, missing=0)
        self._cancel = False
        if background:
            threading.Thread(target=self._scan_safe, args=(list(folders),), daemon=True).start()
        else:
            self._scan_safe(list(folders))
        return True

    def _scan_safe(self, folders):
        try:
            self._scan(folders)
        except Exception as e:  # never leave the screen stuck on "scanning"
            self.scan_state["errors"].append(f"The scan stopped: {type(e).__name__}: {e}")
        finally:
            import time
            self.scan_state.update(running=False, current="", finished=time.time())

    @staticmethod
    def _files(root: Path):
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() in EXTS and p.is_file():
                rel = p.relative_to(root)
                if not any(part.startswith(".") or part.startswith("~") for part in rel.parts):
                    yield p, rel

    def _scan(self, folders):
        st = self.scan_state
        found = []   # (path, root, rel, size, mtime)
        live_roots = []
        for f in folders:
            root = Path(f).expanduser()
            if not root.is_dir():
                st["notes"].append(f"Folder not found, so its designs are hidden for now: {root}")
                continue
            live_roots.append(str(root))
            st["current"] = f"Looking in {root.name}"
            for p, rel in self._files(root):
                try:
                    s = p.stat()
                except OSError:
                    continue
                found.append((str(p), str(root), rel, s.st_size, s.st_mtime))
        st["total"] = len(found)
        with self.lock:
            rows = {r["path"]: r for r in self.db.execute("SELECT * FROM artworks").fetchall()}
        seen = {f[0] for f in found}
        todo = []
        for path, root, rel, size, mtime in found:
            r = rows.get(path)
            thumb_ok = r and (self.cache / "thumbs" / f"{r['id']}.jpg").exists() and (self.cache / "previews" / f"{r['id']}.jpg").exists()
            if r is None or not thumb_ok or r["size"] != size or abs(r["mtime"] - mtime) > 1:
                todo.append((path, root, rel, size, mtime, r))
            else:
                self._touch(r, root, rel)
                st["done"] += 1
        # a file that was renamed or moved keeps its tags: match new files to vanished ones by size and pixels
        gone = [r for p, r in rows.items() if p not in seen and str(r["root"]) in live_roots]
        gone_by_sig = {}
        for r in gone:
            gone_by_sig.setdefault((r["size"], r["w"], r["h"]), []).append(r)

        def work(item):
            if self._cancel:
                return None
            path, root, rel, size, mtime, r = item
            try:
                return self._process(path, root, rel, size, mtime, r, gone_by_sig)
            except Exception as e:
                st["errors"].append(f"Skipped {rel.as_posix()}: {type(e).__name__}: {e}")
                return None
            finally:
                st["done"] += 1
                st["current"] = rel.name

        with ThreadPoolExecutor(max_workers=3) as ex:
            for _ in ex.map(work, todo):
                pass
        # designs in folders that exist but are no longer there are hidden (tags kept in case they come back)
        with self.lock:
            for r in gone:
                if r["id"] not in self._claimed:
                    self.db.execute("UPDATE artworks SET missing=1 WHERE id=?", (r["id"],))
                    st["missing"] += 1
            for p, r in rows.items():
                if p in seen and r["missing"]:
                    self.db.execute("UPDATE artworks SET missing=0 WHERE id=?", (r["id"],))
            self.db.commit()
        self._claimed.clear()

    def _touch(self, r, root, rel):
        cat = rel.parts[0] if len(rel.parts) > 1 else UNCATEGORISED
        if r["missing"] or r["category"] != cat or r["rel"] != rel.as_posix() or r["root"] != root:
            with self.lock:
                self.db.execute("UPDATE artworks SET missing=0, category=?, rel=?, root=? WHERE id=?",
                                (cat, rel.as_posix(), root, r["id"]))
                self.db.commit()

    def _process(self, path, root, rel, size, mtime, r, gone_by_sig):
        with Image.open(path) as im:
            w, h = im.size
            try:  # sizes are stored as the picture is meant to be seen, so account for rotated camera files
                if im.getexif().get(0x0112) in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:
                pass
            old = r
            if old is None:  # renamed or moved?
                cands = gone_by_sig.get((size, w, h)) or []
                with self.lock:
                    cands = [c for c in cands if c["id"] not in self._claimed]
                    if len(cands) == 1:
                        old = cands[0]
                        self._claimed.add(old["id"])
            art_id = old["id"] if old is not None else f"{slug(Path(path).stem)[:30] or 'design'}-{uuid.uuid4().hex[:6]}"
            im.draft("RGB", (PREVIEW_LONG * 2, PREVIEW_LONG * 2))
            base = ImageOps.exif_transpose(im)
            if base.mode in ("RGBA", "LA", "P"):
                rgba = base.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                base = Image.alpha_composite(bg, rgba).convert("RGB")
            else:
                base = base.convert("RGB")
            pv = base.copy()
            pv.thumbnail((PREVIEW_LONG, PREVIEW_LONG), Image.LANCZOS)
            pv.save(self.cache / "previews" / f"{art_id}.jpg", quality=86)
            th = base.copy()
            th.thumbnail((THUMB_W, THUMB_W * 3), Image.LANCZOS)
            th.save(self.cache / "thumbs" / f"{art_id}.jpg", quality=82)
            colors = json.dumps(auto_colors(th))
            tw, thh = th.size
            pw, ph = pv.size
        cat = rel.parts[0] if len(rel.parts) > 1 else UNCATEGORISED
        with self.lock:
            if old is None:
                styles = [p.replace("-", " ").replace("_", " ").lower() for p in rel.parts[1:-1]]
                name = Path(path).stem.replace("-", " ").replace("_", " ").title()
                import time
                self.db.execute(
                    "INSERT INTO artworks (id,path,root,rel,category,size,mtime,w,h,orient,auto_colors,thumb_w,thumb_h,preview_w,preview_h,name,styles,added)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (art_id, path, root, rel.as_posix(), cat, size, mtime, w, h, orientation(w, h), colors, tw, thh, pw, ph,
                     name, json.dumps(styles), int(time.time())))
                self.scan_state["new"] += 1
            else:
                self.db.execute(
                    "UPDATE artworks SET path=?,root=?,rel=?,category=?,size=?,mtime=?,w=?,h=?,orient=?,auto_colors=?,thumb_w=?,thumb_h=?,"
                    "preview_w=?,preview_h=?,missing=0 WHERE id=?",
                    (path, root, rel.as_posix(), cat, size, mtime, w, h, orientation(w, h), colors, tw, thh, pw, ph, art_id))
                self.scan_state["changed"] += 1
            self.db.commit()


def logos(logo_dir: Path):
    out = []
    d = Path(logo_dir)
    if d.exists():
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                out.append({"id": slug(p.stem), "name": p.stem.replace("_", " ").replace("-", " ").title(),
                            "file": p.name, "url": f"media/logos/{p.name}"})
    return out
