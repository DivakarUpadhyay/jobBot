"""Send one email at a time through the sender's own mail server (Gmail by default).

Sender accounts live in .env, numbered, so more can be added later:
    SENDER_1=divakarup1991@gmail.com
    SENDER_1_APP_PASSWORD=abcd efgh ijkl mnop
    SENDER_2=...
Other providers: add SENDER_n_SMTP=smtp.host.com (port 465, SSL).

Layout of every message: From = the sender, To = one address, CC = one of your own
accounts (optional, set in config), no BCC.
"""
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from dotenv import dotenv_values

from . import home


def cc_for(sender_email, cfg):
    """The address copied on an email from this sender (config sending.cc): either one address
    for everything, or a table of sender -> address. An account is never copied on its own email."""
    want = cfg["sending"].get("cc") or ""
    if isinstance(want, dict):
        want = next((v for k, v in want.items() if k.lower() == sender_email.lower()), "")
    want = (want or "").strip().lower()
    return want if want and want != sender_email.lower() else None


def senders():
    """List of dict(n, email, password, smtp) for every SENDER_n in .env."""
    env = dotenv_values(home() / ".env")
    out = []
    for key, email in sorted(env.items()):
        if key.startswith("SENDER_") and key[7:].isdigit() and email:
            n = key[7:]
            out.append({"n": int(n), "email": email.strip(),
                        "password": (env.get(f"SENDER_{n}_APP_PASSWORD") or "").replace(" ", ""),
                        "smtp": env.get(f"SENDER_{n}_SMTP") or "smtp.gmail.com"})
    return sorted(out, key=lambda s: s["n"])


def build(sender_email, sender_name, to, subject, body, attachment=None, cc=None):
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    if attachment:
        path = Path(attachment)
        kind = (mimetypes.guess_type(path.name)[0] or "application/octet-stream").split("/")
        msg.add_attachment(path.read_bytes(), maintype=kind[0], subtype=kind[1], filename=path.name)
    return msg


def send(sender, msg):
    """sender: one dict from senders(). Raises on failure."""
    own = {x["email"].lower() for x in senders()}
    if "," in msg["To"] or msg["Bcc"] or (msg["Cc"] and msg["Cc"].lower() not in own):
        raise ValueError("Each email goes to exactly one address; CC may only be one of your own accounts.")
    with smtplib.SMTP_SSL(sender["smtp"], 465, timeout=60) as s:
        s.login(sender["email"], sender["password"])
        s.send_message(msg)


def check_login(sender):
    with smtplib.SMTP_SSL(sender["smtp"], 465, timeout=30) as s:
        s.login(sender["email"], sender["password"])
