#!/usr/bin/env python3
"""Emporium Print Studio: the engine behind the app window (and, if you switch it on, the office network).

    python3 server.py            run on its own for development (prints the owner link)

The Mac app (desktop.py) starts this in the background and shows it in its own window.
Needs: pillow, reportlab, numpy. No other packages.
"""
import io
import json
import os
import platform
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageDraw, ImageFont

import core
import library
import share

VERSION = "2.0.0"
APP = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))   # where the bundled web/ folder lives


# ---------------------------------------------------------------- settings
def data_dir() -> Path:
    """Where the app keeps its own files (catalogue, thumbnails, settings, logos). Your artworks are never touched."""
    env = os.environ.get("EMPORIUM_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Emporium"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Emporium"
    return Path.home() / ".emporium"


DEFAULTS = {
    "folders": [],                  # the folders with your high-res designs (sub-folders are read too)
    "output_dir": "",               # where proofs and print files go (empty = Documents/Emporium Print Files)
    "share_network": False,         # let other computers on your office network open the app
    "access_pin": "",               # required from other computers when sharing is on
    "port": 8080,
    "allowance_cm": 10,             # added to wall width and height
    "dpi_options": [144, 96],
    "min_dpi": 72,
    "public_url": "",               # only for emailing the printer a download link (see README)
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_user": "", "smtp_password": "",
              "from": "", "to": [], "cc": [], "link_days": 14, "greeting": "Hello,", "signature": "Emporium"},
}

DATA = data_dir()
CFG = {}
LIB = None
OUT = None
LOGO_DIR = None
OWNER_TOKEN = secrets.token_hex(16)   # proves a request comes from the app window itself
TOKENS = set()                        # PIN sessions of people on the office network
JOBQ = queue.Queue()
LOCK = threading.Lock()
PICKER = None                         # set by the app window: a function that asks for a folder
RUNTIME = {"port": 8080, "host": "127.0.0.1"}
LISTENER = None


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    p = DATA / "config.json"
    if p.exists():
        try:
            user = json.loads(p.read_text())
        except ValueError:
            user = {}
        cfg["email"].update(user.pop("email", {}) or {})
        if user.get("masters_dir") and not user.get("folders"):   # older versions
            user["folders"] = [user["masters_dir"]]
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return cfg


def save_config():
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / "config.json.tmp"
    tmp.write_text(json.dumps(CFG, indent=2))
    tmp.replace(DATA / "config.json")


def default_output():
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "Emporium Print Files"


def apply_config():
    global OUT
    OUT = Path(CFG["output_dir"]).expanduser() if CFG.get("output_dir") else default_output()
    OUT.mkdir(parents=True, exist_ok=True)


def init(data=None):
    """Load settings and open the catalogue. Call once before starting the server."""
    global DATA, CFG, LIB, LOGO_DIR
    if data:
        DATA = Path(data)
    DATA.mkdir(parents=True, exist_ok=True)
    CFG = load_config()
    LOGO_DIR = DATA / "logos"
    LOGO_DIR.mkdir(exist_ok=True)
    LIB = library.Library(DATA)
    apply_config()


def jobs_dir():
    return OUT / "jobs"


# ---------------------------------------------------------------- jobs
def save_job(job):
    jobs_dir().mkdir(parents=True, exist_ok=True)
    tmp = jobs_dir() / f"{job['id']}.json.tmp"
    tmp.write_text(json.dumps(job, indent=1))
    tmp.replace(jobs_dir() / f"{job['id']}.json")


def load_job(job_id):
    p = jobs_dir() / f"{Path(job_id).name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def create_job(data):
    with LOCK:
        jobs_dir().mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        n = len(list(jobs_dir().glob(f"EMP-{day}-*.json"))) + 1
        job = dict(data, id=f"EMP-{day}-{n:03d}", status="queued", progress="Waiting in the queue",
                   created=datetime.now().isoformat(timespec="seconds"))
        save_job(job)
    return job


def recent_jobs(n=30):
    files = sorted(jobs_dir().glob("EMP-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    return [json.loads(p.read_text()) for p in files]


def logo_path(logo_id):
    for lg in library.logos(LOGO_DIR):
        if lg["id"] == logo_id:
            return LOGO_DIR / lg["file"]
    return None


def preview_path(art_id):
    return LIB.cache / "previews" / f"{art_id}.jpg"


def process_job(job):
    art = LIB.get(job["artwork_id"])
    if not art:
        raise ValueError("The design is no longer in the library.")
    jw, jh = core.job_size(job["wall_w"], job["wall_h"], job["allowance"])
    recipe = core.check_recipe(job["recipe"], art["w"], art["h"], jw, jh)
    unit = job.get("unit", "cm")
    edits = core.describe_edits(recipe, unit)
    dpi = job["dpi"]
    day = job["created"][:10]
    stem = core.safe(art["name"]) + (f"_{core.safe(art['material'])}" if art["material"] else "") + ("_edited" if edits else "")
    size_tag = f"{core.fmt(jw)}x{core.fmt(jh)}cm"
    folder = OUT / day / f"{job['id']}_{stem}_{size_tag}_{dpi}dpi"
    folder.mkdir(parents=True, exist_ok=True)

    job.update(status="running", progress="Preparing the proof")
    save_job(job)
    region = core.proof_region(preview_path(art["id"]), recipe, jw, jh)
    proof = core.make_proof_pdf(region, art["name"], art["material"], jw, jh, logo_path(job.get("logo")), edits, unit)
    proof_path = folder / f"{stem}_{size_tag}_proof.pdf"
    proof_path.write_bytes(proof)

    job.update(progress="Making the print file. This takes a minute or two for large walls.")
    save_job(job)
    last = [0.0]

    def progress(f):
        if f - last[0] >= 0.1:
            last[0] = f
            job["progress"] = f"Making the print file ({int(f * 100)}%)"
            save_job(job)

    tiff_path = folder / f"{stem}_{size_tag}_{dpi}dpi_print.tiff"
    px_w, px_h = core.make_print_tiff(Path(art["path"]), recipe, jw, jh, dpi, tiff_path, progress)

    sheet = [
        f"Job {job['id']}",
        f"Created: {job['created'].replace('T', ' ')} by {job.get('created_by') or 'unknown'}",
        f"Client / reference: {job.get('client_ref') or '-'}",
        f"Design: {art['name']}" + (f" (material {art['material']})" if art["material"] else ""),
        f"Source file: {art['folder']}",
        f"Wall: {core.fmt(job['wall_w'])} x {core.fmt(job['wall_h'])} cm + {core.fmt(job['allowance'])} cm allowance"
        f" = {core.fmt(jw)} x {core.fmt(jh)} cm"
        + (f" ({core.dim(jw, 'in')} x {core.dim(jh, 'in')})" if unit == "in" else ""),
        f"Print file: {px_w} x {px_h} px at {dpi} dpi",
        f"Artwork width on the wall: {core.fmt(round(recipe['art_w'], 1))} cm"
        + (f" ({core.dim(recipe['art_w'], 'in')})" if unit == "in" else "") + f", position {core.fmt(round(recipe['off_x'], 1))} / "
        f"{core.fmt(round(recipe['off_y'], 1))} cm from the artwork's top-left (master {art['w']} x {art['h']} px)",
        "EDITED VERSION: " + "; ".join(edits) if edits else "Edits: none (original artwork)",
        f"Logo on proof: {job.get('logo') or 'none'}",
        "Client approved the proof: yes",
        "",
        f"Files: {tiff_path.name}, {proof_path.name}",
    ]
    (folder / "job.txt").write_text("\n".join(sheet) + "\n")
    size_mb = round(tiff_path.stat().st_size / 2 ** 20, 1)
    job.update(status="done", progress="Ready", finished=datetime.now().isoformat(timespec="seconds"),
               edits=edits, result={"folder": str(folder.relative_to(OUT)), "tiff": tiff_path.name,
                                    "proof": proof_path.name, "px": [px_w, px_h], "size_mb": size_mb})
    save_job(job)
    (folder / "job.json").write_text(json.dumps(job, indent=1))
    email_job(job, "\n".join(sheet))


def email_job(job, sheet_text=None):
    """Emails the download links to the printer. A failure here never fails the print job."""
    ok, why = share.email_ready(CFG)
    if not ok:
        job["email"] = {"status": "off", "note": why}
    else:
        try:
            if sheet_text is None:
                sheet_text = (OUT / job["result"]["folder"] / "job.txt").read_text()
            rcpts = share.send_printer_email(CFG, OUT, job, sheet_text)
            job["email"] = {"status": "sent", "to": rcpts, "at": datetime.now().isoformat(timespec="seconds")}
        except Exception as e:
            traceback.print_exc()
            job["email"] = {"status": "failed", "note": f"The email could not be sent ({type(e).__name__}). "
                                                       "Check the email settings, then press Resend."}
    save_job(job)
    folder = OUT / job["result"]["folder"]
    (folder / "job.json").write_text(json.dumps(job, indent=1))


def worker():
    while True:
        job_id = JOBQ.get()
        job = load_job(job_id)
        if not job:
            continue
        try:
            process_job(job)
        except Exception:  # keep the queue alive whatever happens to one job
            traceback.print_exc()
            job.update(status="error", progress="Something went wrong while making the file.",
                       error="The print file could not be made. Please note the job number.")
            save_job(job)


def requeue_unfinished():
    for p in jobs_dir().glob("EMP-*.json") if jobs_dir().exists() else []:
        job = json.loads(p.read_text())
        if job.get("status") in ("queued", "running"):
            job.update(status="queued", progress="Waiting in the queue")
            save_job(job)
            JOBQ.put(job["id"])


# ---------------------------------------------------------------- helpers
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def sharing():
    return bool(CFG.get("share_network")) and len(str(CFG.get("access_pin", ""))) >= 4


def open_path(path: Path, reveal=False):
    """Open a file or folder with the Mac's own tools (Preview, Finder). Does nothing where that is not available."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # noqa
        elif platform.system() == "Linux" and os.environ.get("DISPLAY"):
            subprocess.Popen(["xdg-open", str(path.parent if reveal else path)])
        else:
            return False
        return True
    except OSError:
        return False


def pick_folder():
    if PICKER:
        return PICKER()
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a folder with your designs")'],
                               capture_output=True, text=True, timeout=300)
            return r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def settings_view():
    folders = []
    for f in CFG["folders"]:
        p = Path(f).expanduser()
        folders.append({"path": str(p), "exists": p.is_dir()})
    ip = lan_ip()
    return {
        "folders": folders, "output_dir": str(OUT), "share_network": bool(CFG["share_network"]),
        "sharing_active": sharing(), "pin_set": bool(CFG["access_pin"]), "port": RUNTIME["port"],
        "share_url": f"http://{ip}:{RUNTIME['port']}", "logo_dir": str(LOGO_DIR), "data_dir": str(DATA),
        "version": VERSION, "counts": LIB.counts(), "scan": dict(LIB.scan_state),
        "email_on": bool(CFG["email"].get("enabled")),
    }


def icon_png(size):
    img = Image.new("RGB", (size, size), "#0d5b63")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=int(size * 0.55))
    except TypeError:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2), "E", fill="white", font=font, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


SKELETON_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    '<meta name="theme-color" content="#f4f4f2"><link rel="icon" href="/icon-192.png"><link rel="apple-touch-icon" href="/icon-192.png">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
    "html,body{height:100%}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}"
    "</style></head><body>"
)
MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


# ---------------------------------------------------------------- web
class Handler(BaseHTTPRequestHandler):
    server_version = f"Emporium/{VERSION}"

    def log_message(self, fmt, *args):
        if os.environ.get("EMPORIUM_QUIET"):
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # helpers
    def send_bytes(self, data, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200, extra=None):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status, extra)

    def role(self):
        """'owner' = the app window itself, 'team' = someone on the office network who entered the PIN, else None."""
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "emp_owner" in jar and secrets.compare_digest(jar["emp_owner"].value, OWNER_TOKEN):
            return "owner"
        if sharing() and "emp" in jar and jar["emp"].value in TOKENS:
            return "team"
        return None

    def host_ok(self):
        """Blocks other web sites from talking to the app through the browser (DNS rebinding and cross-site posts)."""
        host = (self.headers.get("Host") or "").lower()
        if not sharing():
            if host.rsplit(":", 1)[0] not in ("127.0.0.1", "localhost", "[::1]"):
                return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != host:
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 262144:
            raise ValueError("Request too large.")
        return json.loads(self.rfile.read(n) or b"{}")

    # routes
    def do_GET(self):
        if not self.host_ok():
            return self.send_json({"error": "Not allowed"}, 403)
        u = urlparse(self.path)
        path = unquote(u.path)
        if path in ("/", "/index.html"):
            q = parse_qs(u.query)
            tok = (q.get("owner") or [""])[0]
            if tok and secrets.compare_digest(tok, OWNER_TOKEN):
                self.send_response(302)
                self.send_header("Set-Cookie", f"emp_owner={OWNER_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            html = (APP / "web" / "index.html").read_text(encoding="utf-8")
            return self.send_bytes((SKELETON_HEAD + html + "</body></html>").encode(), "text/html; charset=utf-8",
                                   extra={"Cache-Control": "no-cache"})
        if path in ("/icon-192.png", "/icon-512.png"):
            return self.send_bytes(icon_png(192 if "192" in path else 512), "image/png",
                                   extra={"Cache-Control": "max-age=86400"})
        role = self.role()
        if path == "/api/config":
            cfg = {"role": role, "authed": bool(role), "pin_required": sharing(), "version": VERSION}
            if role:
                cfg.update(allowance=CFG["allowance_cm"], dpi_options=CFG["dpi_options"], min_dpi=CFG["min_dpi"],
                           logos=library.logos(LOGO_DIR), owner=role == "owner", artworks=LIB.counts()["artworks"],
                           scanning=LIB.scan_state["running"])
            return self.send_json(cfg)
        if path.startswith("/dl/"):
            return self.download(path)
        if not role:
            return self.send_json({"error": "Not signed in"}, 401)
        if path == "/api/catalog":
            return self.send_json(LIB.catalog(), extra={"Cache-Control": "no-cache"})
        if path == "/api/jobs":
            return self.send_json(recent_jobs())
        if path.startswith("/api/jobs/"):
            job = load_job(path.rsplit("/", 1)[-1])
            return self.send_json(job, 200) if job else self.send_json({"error": "Job not found"}, 404)
        if path in ("/api/settings", "/api/scan"):
            if role != "owner":
                return self.send_json({"error": "Only the app window can change settings."}, 403)
            return self.send_json(settings_view() if path == "/api/settings" else dict(LIB.scan_state, counts=LIB.counts()))
        if path.startswith("/media/"):
            parts = path.split("/")
            if len(parts) == 4 and parts[2] in ("thumbs", "previews", "logos"):
                base = LOGO_DIR if parts[2] == "logos" else LIB.cache / parts[2]
                f = (base / parts[3]).resolve()
                if base.resolve() in f.parents and f.is_file() and f.suffix.lower() in MEDIA:
                    return self.send_bytes(f.read_bytes(), MEDIA[f.suffix.lower()],
                                           extra={"Cache-Control": "private, max-age=60"})
        self.send_json({"error": "Not found"}, 404)

    def download(self, path):
        """Signed link for the printer: /dl/<job>/<expiry>/<signature>/<file>. Supports resume (Range)."""
        parts = path.split("/")
        if len(parts) != 6:
            return self.send_json({"error": "Not found"}, 404)
        _, _, job_id, expiry, sig, name = parts
        if not share.verify(OUT, job_id, expiry, sig, name):
            return self.send_json({"error": "This link is invalid or has expired. Ask Emporium for a new one."}, 403)
        job = load_job(job_id)
        res = (job or {}).get("result") or {}
        if name not in (res.get("tiff"), res.get("proof")):
            return self.send_json({"error": "Not found"}, 404)
        f = OUT / res["folder"] / name
        if not f.is_file():
            return self.send_json({"error": "File not found"}, 404)
        size = f.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "," not in rng:
            a, _, b = rng[6:].partition("-")
            try:
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                if start > end or start >= size:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        ctype = "application/pdf" if name.endswith(".pdf") else "image/tiff"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if not self.host_ok():
            return self.send_json({"error": "Not allowed"}, 403)
        path = urlparse(self.path).path
        try:
            data = self.body()
            if path == "/api/login":
                pin = str(CFG.get("access_pin", ""))
                if sharing() and secrets.compare_digest(str(data.get("pin", "")), pin):
                    token = secrets.token_hex(16)
                    TOKENS.add(token)
                    return self.send_json({"ok": True}, extra={
                        "Set-Cookie": f"emp={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})
                time.sleep(0.6)
                return self.send_json({"error": "That PIN is not right."}, 403)
            role = self.role()
            if not role:
                return self.send_json({"error": "Not signed in"}, 401)
            if path == "/api/proof":
                return self.proof(data, role)
            if path == "/api/print":
                return self.print_job(data)
            if path == "/api/email":
                job = load_job(str(data.get("job_id", "")))
                if not job or job.get("status") != "done":
                    raise ValueError("That job is not finished yet.")
                email_job(job)
                return self.send_json(job)
            if role != "owner":
                return self.send_json({"error": "Only the app window can do that."}, 403)
            if path == "/api/settings":
                return self.send_json(self.save_settings(data))
            if path == "/api/pick-folder":
                return self.send_json({"path": pick_folder()})
            if path == "/api/scan":
                LIB.start_scan(CFG["folders"])
                return self.send_json(dict(LIB.scan_state))
            if path == "/api/artwork":
                LIB.update(str(data.get("id", "")), data)
                return self.send_json(LIB.get(str(data.get("id", ""))) or {})
            if path == "/api/artworks/bulk":
                ids = [str(i) for i in (data.get("ids") or [])][:5000]
                LIB.bulk_styles(ids, data.get("add_styles") or [], data.get("remove_styles") or [])
                return self.send_json({"ok": True})
            if path == "/api/open":
                return self.send_json(self.open_something(data))
            self.send_json({"error": "Not found"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "Something went wrong. Please try again."}, 500)

    # settings -----------------------------------------------------
    def save_settings(self, data):
        global LISTENER
        before = (CFG["share_network"], CFG["access_pin"])
        if "folders" in data:
            seen, folders = set(), []
            for f in data["folders"] or []:
                p = Path(str(f)).expanduser()
                if not p.is_dir():
                    raise ValueError(f"I can't find the folder {p}")
                if str(p) not in seen:
                    seen.add(str(p)); folders.append(str(p))
            changed = folders != CFG["folders"]
            CFG["folders"] = folders
        else:
            changed = False
        if "output_dir" in data and data["output_dir"]:
            p = Path(str(data["output_dir"])).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ValueError("I can't use that folder for print files.")
            CFG["output_dir"] = str(p)
        if "access_pin" in data:
            pin = str(data["access_pin"]).strip()
            if pin and len(pin) < 4:
                raise ValueError("The PIN needs at least 4 characters.")
            if pin != CFG["access_pin"]:
                TOKENS.clear()
            CFG["access_pin"] = pin
        if "share_network" in data:
            want = bool(data["share_network"])
            if want and len(str(CFG["access_pin"])) < 4:
                raise ValueError("Set a PIN of at least 4 characters first. Anyone on the office network could otherwise open the app.")
            CFG["share_network"] = want
        apply_config()
        save_config()
        if changed:
            LIB.start_scan(CFG["folders"])
        if (CFG["share_network"], CFG["access_pin"]) != before and LISTENER:
            threading.Thread(target=LISTENER.rebind, daemon=True).start()
        return settings_view()

    def open_something(self, data):
        kind = data.get("kind")
        if kind == "output":
            return {"ok": open_path(OUT)}
        if kind == "logos":
            return {"ok": open_path(LOGO_DIR)}
        if kind == "data":
            return {"ok": open_path(DATA)}
        if kind == "proof":
            f = (OUT / "Proofs" / Path(str(data.get("name", ""))).name)
            if not f.is_file():
                raise ValueError("That proof is not there any more.")
            return {"ok": open_path(f)}
        if kind == "job":
            f = (OUT / str(data.get("folder", ""))).resolve()
            if OUT.resolve() not in f.parents or not f.is_dir():
                raise ValueError("That folder is not there any more.")
            return {"ok": open_path(f)}
        raise ValueError("Unknown request.")

    # proof and print -----------------------------------------------
    def common(self, data):
        art = LIB.get(str(data.get("artwork_id", "")))
        if not art:
            raise ValueError("That design is not in the library.")
        p = core.check_params(data)
        jw, jh = core.job_size(p["wall_w"], p["wall_h"], p["allowance"])
        recipe = core.check_recipe(data.get("recipe"), art["w"], art["h"], jw, jh)
        return art, p, jw, jh, recipe

    def proof(self, data, role):
        art, p, jw, jh, recipe = self.common(data)
        region = core.proof_region(preview_path(art["id"]), recipe, jw, jh)
        pdf = core.make_proof_pdf(region, art["name"], art["material"], jw, jh, logo_path(data.get("logo")),
                                  core.describe_edits(recipe, p["unit"]), p["unit"])
        stem = core.safe(art["name"]) + (f"_{core.safe(art['material'])}" if art["material"] else "")
        name = f"{stem}_{core.fmt(jw)}x{core.fmt(jh)}cm_proof.pdf"
        (OUT / "Proofs").mkdir(parents=True, exist_ok=True)
        (OUT / "Proofs" / name).write_bytes(pdf)
        self.send_bytes(pdf, "application/pdf", extra={"Content-Disposition": f'inline; filename="{name}"',
                                                       "X-Filename": name})

    def print_job(self, data):
        art, p, jw, jh, recipe = self.common(data)
        dpi = p.get("dpi")
        if not dpi:
            raise ValueError("Choose a print resolution (dpi).")
        if not data.get("approved"):
            raise ValueError("Tick \"Client approved the proof\" before sending to print.")
        if dpi < CFG["min_dpi"]:
            raise ValueError(f"The lowest allowed resolution is {CFG['min_dpi']} dpi.")
        sup = core.eff_dpi(art["w"], recipe["art_w"])
        if sup < dpi * 0.995:
            if sup < CFG["min_dpi"]:
                raise ValueError(f"The artwork does not have enough pixels at this size (about {int(sup)} dpi). "
                                 "Make the artwork smaller on the wall, use a smaller wall or choose another design.")
            raise ValueError(f"At this size the artwork only has enough pixels for about {int(sup)} dpi. "
                             f"Choose {int(sup)} dpi or lower, or make the artwork smaller on the wall.")
        job = create_job({
            "artwork_id": art["id"], "artwork_name": art["name"], "material": art["material"],
            "wall_w": p["wall_w"], "wall_h": p["wall_h"], "allowance": p["allowance"],
            "recipe": recipe, "unit": p["unit"], "dpi": dpi, "logo": data.get("logo") or "",
            "created_by": str(data.get("created_by", ""))[:60], "client_ref": str(data.get("client_ref", ""))[:120],
        })
        JOBQ.put(job["id"])
        self.send_json(job, 202)


class Listener:
    """The web server, which can be switched between 'this computer only' and 'office network' while running."""

    def __init__(self):
        self.srv = None
        self.thread = None

    def start(self):
        host = "0.0.0.0" if sharing() else "127.0.0.1"
        port = int(CFG["port"])
        last = None
        for p in range(port, port + 30):
            try:
                srv = ThreadingHTTPServer((host, p), Handler)
                break
            except OSError as e:
                last = e
        else:
            raise last
        srv.daemon_threads = True
        self.srv = srv
        RUNTIME.update(port=srv.server_address[1], host=host)
        self.thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def rebind(self):
        time.sleep(0.5)   # let the answer to the settings request go out first
        self.stop()
        self.start()


def start_background(scan=True):
    """Start the job worker, the web server and (optionally) a scan of the design folders. Returns the port."""
    global LISTENER
    requeue_unfinished()
    threading.Thread(target=worker, daemon=True).start()
    LISTENER = Listener()
    LISTENER.start()
    if scan and CFG["folders"]:
        LIB.start_scan(CFG["folders"])
    return RUNTIME["port"]


def main():
    init()
    port = start_background()
    print(f"\nEmporium Print Studio {VERSION} is running.")
    print(f"  Owner link (this computer): http://127.0.0.1:{port}/?owner={OWNER_TOKEN}")
    print(f"  Data folder: {DATA}\n  Print files: {OUT}\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
