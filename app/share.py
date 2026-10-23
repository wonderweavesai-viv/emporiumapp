"""Signed download links and the email to the printer.

A link looks like  https://<public_url>/dl/<job>/<expiry>/<signature>/<file name>
The signature is an HMAC of job, expiry and file name with a secret kept in output/.secret, so links cannot be
guessed or edited, and they stop working after `link_days`. Nothing else in the app is reachable without the PIN.
"""
import hashlib
import hmac
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote


def _secret(out_dir: Path) -> bytes:
    p = out_dir / ".secret"
    if not p.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32))
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p.read_text().strip().encode()


def sign(out_dir: Path, job_id: str, expiry: int, name: str) -> str:
    msg = f"{job_id}|{expiry}|{name}".encode()
    return hmac.new(_secret(out_dir), msg, hashlib.sha256).hexdigest()[:40]


def verify(out_dir: Path, job_id: str, expiry: str, sig: str, name: str) -> bool:
    try:
        exp = int(expiry)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(sign(out_dir, job_id, exp, name), sig)


def make_link(out_dir: Path, base: str, job_id: str, name: str, days: int) -> str:
    exp = int(time.time() + days * 86400)
    return f"{base.rstrip('/')}/dl/{job_id}/{exp}/{sign(out_dir, job_id, exp, name)}/{quote(name)}"


def email_ready(cfg: dict):
    """Returns (ok, reason). reason explains in plain words what is missing."""
    e = cfg.get("email") or {}
    if not e.get("enabled"):
        return False, "Email to the printer is switched off in config.json."
    if not cfg.get("public_url"):
        return False, "public_url is not set in config.json, so download links cannot be made."
    if not e.get("to"):
        return False, "No printer address is set in config.json."
    if not (e.get("smtp_host") and e.get("from")):
        return False, "The email server settings are incomplete in config.json."
    return True, ""


def send_printer_email(cfg: dict, out_dir: Path, job: dict, sheet_text: str):
    e = cfg["email"]
    res = job["result"]
    days = int(e.get("link_days", 14))
    base = cfg["public_url"]
    links = [(res["tiff"], f"Print file ({res['size_mb']} MB)"), (res["proof"], "Approved proof (PDF)")]
    lines = [e.get("greeting", "Hello,"), "",
             f"Please find the print file for job {job['id']} below. The links work for {days} days.", ""]
    for name, label in links:
        lines += [f"{label}:", make_link(out_dir, base, job["id"], name, days), ""]
    lines += ["Job details", "-----------", sheet_text.strip(), "", e.get("signature", "Emporium")]

    msg = EmailMessage()
    msg["Subject"] = f"Print job {job['id']} - {job['artwork_name']}{' [EDITED]' if job.get('edits') else ''} - {job['dpi']:g} dpi"
    msg["From"] = e["from"]
    msg["To"] = ", ".join(e["to"])
    if e.get("cc"):
        msg["Cc"] = ", ".join(e["cc"])
    if e.get("reply_to"):
        msg["Reply-To"] = e["reply_to"]
    msg.set_content("\n".join(lines))

    host, port = e["smtp_host"], int(e.get("smtp_port", 587))
    rcpts = list(e["to"]) + list(e.get("cc") or [])
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
    with s:
        if port != 465 and e.get("starttls", True):
            s.starttls(context=ssl.create_default_context())
        if e.get("smtp_user"):
            s.login(e["smtp_user"], e.get("smtp_password", ""))
        s.send_message(msg, from_addr=e["from"], to_addrs=rcpts)
    return rcpts
