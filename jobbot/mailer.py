"""Send one email at a time through the sender's own mail server (Gmail by default).

Sender accounts live in .env, numbered, so more can be added later:
    SENDER_1=divakarup1991@gmail.com
    SENDER_1_APP_PASSWORD=abcd efgh ijkl mnop
    SENDER_2=...
Other providers: add SENDER_n_SMTP=smtp.host.com (port 465, SSL) and SENDER_n_IMAP=imap.host.com.
A mailbox connected by signing in (oauth.py) has SENDER_n_OAUTH=google|microsoft and SENDER_n_OAUTH_TOKEN instead
of an app password. Passwords and tokens are stored encrypted (secrets_box.py).

Layout of every message: From = the sender, To = one address, CC = one of your own
accounts (optional, set in config), no BCC.
"""
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from dotenv import dotenv_values

from . import home, secrets_box


def cc_for(sender_email, cfg):
    """The address copied on an email from this sender (config sending.cc): either one address
    for everything, or a table of sender -> address. An account is never copied on its own email."""
    want = cfg["sending"].get("cc") or ""
    if isinstance(want, dict):
        want = next((v for k, v in want.items() if k.lower() == sender_email.lower()), "")
    want = (want or "").strip().lower()
    return want if want and want != sender_email.lower() else None


def senders():
    """List of dict(n, email, password, smtp, imap, oauth, oauth_token, can_send) for every SENDER_n in .env.
    A mailbox is connected either by an app password or by signing in (oauth.py); can_send says it has one of them."""
    from .oauth import PROVIDERS
    env = dotenv_values(home() / ".env")
    out = []
    for key, email in sorted(env.items()):
        if key.startswith("SENDER_") and key[7:].isdigit() and email:
            n = key[7:]
            provider = (env.get(f"SENDER_{n}_OAUTH") or "").strip().lower() or None
            smtp = env.get(f"SENDER_{n}_SMTP") or (PROVIDERS[provider]["smtp"][0] if provider in PROVIDERS else "smtp.gmail.com")
            s = {"n": int(n), "email": email.strip(),
                 "password": secrets_box.open_(env.get(f"SENDER_{n}_APP_PASSWORD") or "").replace(" ", ""),
                 "smtp": smtp, "oauth": provider if provider in PROVIDERS else None,
                 "oauth_token": env.get(f"SENDER_{n}_OAUTH_TOKEN") or "",
                 "imap": env.get(f"SENDER_{n}_IMAP") or (PROVIDERS[provider]["imap"] if provider in PROVIDERS else
                                                         "imap.gmail.com" if "gmail" in smtp else None)}
            s["can_send"] = bool(s["password"] or (s["oauth"] and s["oauth_token"]))
            out.append(s)
    return sorted(out, key=lambda s: s["n"])


class smtp_session:
    """A signed-in SMTP connection for one sender: app password, or XOAUTH2 for a mailbox connected by signing in."""

    def __init__(self, sender, timeout=60):
        self.sender, self.timeout, self.s = sender, timeout, None

    def __enter__(self):
        from . import oauth
        sender = self.sender
        if sender.get("oauth"):
            host, port, mode = oauth.PROVIDERS[sender["oauth"]]["smtp"]
            self.s = smtplib.SMTP_SSL(host, port, timeout=self.timeout) if mode == "ssl" else smtplib.SMTP(host, port, timeout=self.timeout)
            if mode == "starttls":
                self.s.starttls()
            self.s.ehlo()
            token = oauth.access_token(sender)
            self.s.auth("XOAUTH2", lambda challenge=None: oauth.xoauth2(sender["email"], token), initial_response_ok=True)
        else:
            self.s = smtplib.SMTP_SSL(sender["smtp"], 465, timeout=self.timeout)
            self.s.login(sender["email"], sender["password"])
        return self.s

    def __exit__(self, *exc):
        try:
            self.s.quit()
        except Exception:
            pass


def imap_session(sender, timeout=30):
    """A signed-in IMAP connection, or None when this mailbox has no known IMAP server."""
    import imaplib
    from . import oauth
    if not sender.get("imap"):
        return None
    m = imaplib.IMAP4_SSL(sender["imap"], timeout=timeout)
    if sender.get("oauth"):
        token = oauth.access_token(sender)
        m.authenticate("XOAUTH2", lambda _: oauth.xoauth2(sender["email"], token).encode())
    else:
        m.login(sender["email"], sender["password"])
    return m


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
    with smtp_session(sender) as s:
        s.send_message(msg)


def check_login(sender):
    with smtp_session(sender, timeout=30):
        pass
