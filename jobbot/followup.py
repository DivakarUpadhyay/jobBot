"""One short follow-up per application, a week after it was sent, when nothing came back.

It goes as a reply in the same email thread, from the same account, to the same address, with no
attachment. An application gets at most one follow-up, ever. Leads and collaboration emails get none.
Follow-ups count toward the account's daily cap and have their own small daily limit.
"""
import datetime as dt
import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import make_msgid

from . import db, mailer, profilestore


def due(con, cfg):
    """Applications sent at least `after_days` ago with no reply, no bounce and no follow-up yet."""
    f = cfg.get("followup") or {}
    before = (dt.date.today() - dt.timedelta(days=f.get("after_days", 7))).isoformat() + "T99"
    return con.execute("""SELECT * FROM jobs WHERE send_status='sent' AND outcome IS NULL AND followup_at IS NULL
                          AND job_type IN ('full','part') AND sent_at<=? ORDER BY sent_at""", (before,)).fetchall()


def original(sender, to):
    """(Message-ID, subject) of the application as it sits in the Sent folder, or (None, None)."""
    try:
        if "gmail" not in (sender.get("imap") or ""):   # finds the original with Gmail's own search
            return None, None
        m = mailer.imap_session(sender)
        _, boxes = m.list()
        sent_box = next((re.search(r'"([^"]+)"$', b.decode()).group(1) for b in boxes if b"\\Sent" in b), None)
        m.select(f'"{sent_box}"', readonly=True)
        _, data = m.uid("SEARCH", None, "X-GM-RAW", f'"to:{to}"')
        uids = data[0].split()
        if not uids:
            m.logout()
            return None, None
        _, parts = m.uid("FETCH", uids[0], "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)])")
        m.logout()
        head = email.message_from_bytes(parts[0][1])
        return head.get("Message-ID"), str(make_header(decode_header(head.get("Subject", ""))))
    except Exception:
        return None, None


def write(row, master):
    t = profilestore.get("email-templates")
    c = master["contact"]
    role = re.sub(r"^(Application for |Re: )", "", row["email_subject"] or row["title"]).split(" – ")[0]
    vals = {"greeting": f"Dear {row['hr_name']}," if row["hr_name"] else "Dear Hiring Team,", "title": role,
            "company": row["company"], "sent_on": dt.date.fromisoformat(row["sent_at"][:10]).strftime("%d %B").lstrip("0"),
            "name": master["name"], "phone": c["phone"], "link": "\n".join(c["links"])}
    return (t["followup"]["body"].format(**vals).strip() + "\n\n" + t["signature"].format(**vals).strip())


def build(row, sender, master, cfg, message_id, subject):
    msg = mailer.build(sender["email"], master["name"].replace(" R.", ""), row["to_email"],
                       subject if subject.lower().startswith("re:") else "Re: " + subject, write(row, master),
                       cc=mailer.cc_for(sender["email"], cfg))
    if message_id:   # keeps it in the same conversation as the application
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id
    return msg


def run(con, cfg, senders, room, dry_run=False):
    """room: {sender_email: emails this account may still send today}. Returns list of (job_id, to, state)."""
    limit = (cfg.get("followup") or {}).get("per_day_per_account", 3)
    by_email = {s["email"]: s for s in senders}
    done_today = {e: con.execute("SELECT COUNT(*) FROM jobs WHERE sender=? AND followup_at LIKE ?",
                                 (e, db.today() + "%")).fetchone()[0] for e in by_email}
    from . import render
    master, out = render.load_master(), []
    for row in due(con, cfg):
        sender = by_email.get(row["sender"])
        if not sender:
            continue   # the account that sent it is not in use; nobody else may write to this company
        if room.get(sender["email"], 0) <= 0 or done_today[sender["email"]] >= limit:
            continue
        if dry_run:
            out.append((row["id"], row["to_email"], "would send"))
            room[sender["email"]] -= 1; done_today[sender["email"]] += 1
            continue
        message_id, subject = original(sender, row["to_email"])
        try:
            # claim first, so an interruption can never cause a second follow-up
            con.execute("UPDATE jobs SET followup_at=? WHERE id=? AND followup_at IS NULL", (db.now(), row["id"]))
            con.commit()
            mailer.send(sender, build(row, sender, master, cfg, message_id, subject or row["email_subject"]))
            out.append((row["id"], row["to_email"], "sent"))
        except Exception as e:
            out.append((row["id"], row["to_email"], f"FAILED {type(e).__name__}"))
        room[sender["email"]] -= 1; done_today[sender["email"]] += 1
    return out
