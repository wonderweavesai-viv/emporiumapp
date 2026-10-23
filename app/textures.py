"""The texture library: images in a folder you choose (sub-folders included) that can be placed under an artwork.

Nothing here changes your files. Small previews are kept in the app's own data folder."""
import hashlib
import threading
from pathlib import Path

from PIL import Image, ImageOps

EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
THUMB = 240
PREVIEW = 1024


def _id(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


class Textures:
    def __init__(self, data_dir):
        self.cache = Path(data_dir) / "cache" / "textures"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.root = None
        self.items = {}          # id -> dict
        self.lock = threading.RLock()
        self._dims = {}          # (path, size, mtime) -> (w, h)

    def set_folder(self, folder):
        self.root = Path(folder).expanduser() if folder else None
        self.refresh()

    def refresh(self):
        items = {}
        if self.root and self.root.is_dir():
            for p in sorted(self.root.rglob("*"), key=lambda q: str(q).lower()):
                if p.suffix.lower() not in EXTS or p.name.startswith(".") or not p.is_file():
                    continue
                rel = str(p.relative_to(self.root))
                try:
                    st = p.stat()
                    key = (str(p), st.st_size, st.st_mtime_ns)
                    if key not in self._dims:
                        with Image.open(p) as im:
                            self._dims[key] = im.size
                    w, h = self._dims[key]
                except Exception:
                    continue          # unreadable file: skip it
                tid = _id(rel)
                folder = str(Path(rel).parent) if Path(rel).parent != Path(".") else ""
                items[tid] = {"id": tid, "name": p.stem.replace("_", " ").replace("-", " ").strip().title() or p.stem,
                              "folder": folder, "w": w, "h": h, "path": str(p), "mtime": st.st_mtime_ns}
        with self.lock:
            self.items = items

    def listing(self):
        """What the browser gets: no file paths."""
        with self.lock:
            return [{"id": t["id"], "name": t["name"], "folder": t["folder"], "w": t["w"], "h": t["h"],
                     "thumb": f"media/textures/{t['id']}_t.jpg", "preview": f"media/textures/{t['id']}_p.jpg"}
                    for t in self.items.values()]

    def get(self, tid):
        with self.lock:
            return self.items.get(str(tid))

    def lookup(self, tid):
        """(path, name, w, h) or None. Used when a recipe names a texture."""
        t = self.get(tid)
        return (Path(t["path"]), t["name"], t["w"], t["h"]) if t else None

    def media(self, fname):
        """Bytes of a thumbnail ('<id>_t.jpg') or preview ('<id>_p.jpg'), made on first use."""
        try:
            tid, kind = fname[:-4].rsplit("_", 1)
        except ValueError:
            return None
        t = self.get(tid)
        if not t or kind not in ("t", "p"):
            return None
        out = self.cache / f"{tid}_{t['mtime']}_{kind}.jpg"
        if not out.exists():
            try:
                im = load(Path(t["path"]), PREVIEW if kind == "p" else THUMB)
                im.save(out, "JPEG", quality=88 if kind == "p" else 80)
            except Exception:
                return None
        return out.read_bytes()


def load(path, max_side=None):
    """Open a texture as plain RGB (transparent parts become white), optionally reduced to `max_side` pixels."""
    with Image.open(path) as im:
        if max_side:
            im.draft("RGB", (max_side * 2, max_side * 2))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba).convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")
        else:
            im = im.copy()
    if max_side and max(im.size) > max_side:
        k = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    return im
