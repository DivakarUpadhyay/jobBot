"""Decide what may be sent now, split it across the sender accounts, and send in parallel.

Rules enforced here:
  - per-account daily cap that grows week by week (warm-up)
  - a few emails per account per run, random gap between them, office hours only
  - one company is only ever written to from one account, and not again within N days
  - one recipient per email, no CC/BCC (mailer refuses anything else)
  - bidding-platform projects are never emailed
"""
import datetime as dt
import imaplib
import json
import os
import random
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import HomePath, db, mailer, render

LABEL_ROOT = "Jobs"
TRACK_LABEL = {"full": "Full-time", "part": "Part-time", "freelance": "Freelance", "lead": "Leads",
               "collaboration": "Collaboration", "subcontract": "Subcontracting"}


# "%ef%bf%bc" (encoded junk) or a phone number glued to the front, e.g. "800-726-9662customerservice"
BAD_ADDRESS = re.compile(r"%[0-9a-f]{2}|^[\d-]{6,}[a-z]|accommodat|disabilit|accessib", re.I)   # + disability-accommodation inboxes


def company_key(row):
    return row["dedupe_key"].split("|")[0] or row["company"].lower()


def cap_today(con, sender_email, cfg):
    """Daily cap for this account, by weeks since its first send."""
    first = con.execute("SELECT MIN(sent_at) FROM jobs WHERE sender=? AND sent_at IS NOT NULL", (sender_email,)).fetchone()[0]
    week = 0 if not first else (dt.date.today() - dt.date.fromisoformat(first[:10])).days // 7
    caps = cfg["sending"]["warmup_caps"]
    return min(caps[min(week, len(caps) - 1)], cfg["sending"]["daily_ceiling"]), week + 1


def sent_today(con, sender_email):
    """Everything this account sent today: applications and follow-ups."""
    day = db.today() + "%"
    return con.execute("SELECT (SELECT COUNT(*) FROM jobs WHERE sender=? AND send_status='sent' AND sent_at LIKE ?) + "
                       "(SELECT COUNT(*) FROM jobs WHERE sender=? AND followup_at LIKE ?)",
                       (sender_email, day, sender_email, day)).fetchone()[0]


def _minutes(value):
    """9 -> 540, "08:30" -> 510"""
    if isinstance(value, (int, float)):
        return int(value * 60)
    h, _, m = str(value).partition(":")
    return int(h) * 60 + int(m or 0)


def in_hours(cfg, now=None):
    now = now or dt.datetime.now()
    s = cfg["sending"]
    return now.weekday() in s["days"] and _minutes(s["hours"][0]) <= now.hour * 60 + now.minute < _minutes(s["hours"][1])


def sendable(con, cfg):
    """Approved jobs with an email and a tailored resume that are new: the address was never
    written to before, and the company not within the configured window. Full-time and
    part-time alternate, so a batch is half and half while both kinds last."""
    days = cfg["sending"]["same_company_days"]
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    past = con.execute("SELECT dedupe_key, company, to_email, sent_at FROM jobs WHERE sent_at IS NOT NULL").fetchall()
    used_addresses = {(r["to_email"] or "").lower() for r in past}              # never twice, ever
    used_addresses |= {(r[0] or "").lower() for r in con.execute(
        "SELECT to_email FROM jobs WHERE send_status='sending'")}               # interrupted sends count as sent
    recent_companies = {company_key(r) for r in past if r["sent_at"] >= since}  # not again within the window
    rows = con.execute("""SELECT * FROM jobs WHERE status='matched' AND send_status='approved' AND emails!='[]'
                          AND (resume_path IS NOT NULL OR job_type='lead') AND source!='freelancer'
                          ORDER BY (region='taiwan') DESC, score DESC, posted_at DESC""").fetchall()   # Taiwan first, then newest
    kinds, seen = {"full": [], "part": [], "lead": [], "prospect": []}, set()
    for r in rows:
        to = json.loads(r["emails"])[0].lower()
        key = company_key(r)
        if BAD_ADDRESS.search(to.split("@")[0]):   # garbled when read from the page: it would only bounce
            continue
        if key in recent_companies or to in used_addresses or key in seen or to in seen:
            continue
        seen.update((key, to))
        kinds[r["job_type"] if r["job_type"] in kinds else "full"].append((r, to))
    out = []
    for i in range(max(len(kinds["full"]), len(kinds["part"]))):
        out += kinds["full"][i:i + 1] + kinds["part"][i:i + 1]
    quota = cfg["sending"].get("place_first") or {}
    if quota:   # each listed place gets its share at the front of the day's list
        first, rest, taken = [], [], {}
        for item in out:
            place = place_of(item[0], quota)
            if place and taken.get(place, 0) < quota[place]:
                taken[place] = taken.get(place, 0) + 1
                first.append(item)
            else:
                rest.append(item)
        out = first + rest
    # speculative emails: Taiwan and other countries take turns, so one country's long list does not hold up the other
    tw = [p for p in kinds["prospect"] if p[0]["region"] == "taiwan"]
    other = [p for p in kinds["prospect"] if p[0]["region"] != "taiwan"]
    prospects = [p for pair in zip(tw, other) for p in pair] + tw[len(other):] + other[len(tw):]
    return out + prospects + kinds["lead"]   # job applications first; speculative ones and leads use what is left


PLACE_WORDS = {"Taiwan": ("taiwan", "台灣", "臺灣"), "Hong Kong": ("hong kong", "hong-kong", "香港"),
               "India": ("india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "delhi", "noida", "chennai",
                         "gurugram", "kolkata", "ahmedabad")}


def place_of(row, names):
    """Which of the named places a job is in, or None. Read from its region, location and source name."""
    if "Taiwan" in names and row["region"] == "taiwan":
        return "Taiwan"
    text = f"{row['location'] or ''} {row['source'] or ''}".lower()
    return next((n for n in names if n != "Taiwan" and any(w in text for w in PLACE_WORDS.get(n, (n.lower(),)))), None)


def plan(con, cfg, senders, ignore_hours=False):
    """Returns ({sender_email: [(row, to), ...]}, notes)."""
    notes, s_cfg = [], cfg["sending"]
    if not ignore_hours and not in_hours(cfg):
        return {}, ["Outside sending hours; nothing sent."]
    room, left_today, started = {}, 0, False
    for s in senders:
        cap, week = cap_today(con, s["email"], cfg)
        done = sent_today(con, s["email"])
        left = max(0, cap - done)
        started = started or done > 0
        room[s["email"]] = min(left, s_cfg["per_run"])
        notes.append(f"{s['email']}: warm-up week {week}, cap {cap}/day, {left} left today")
        if bounce_stop(con, s["email"], cfg):
            room[s["email"]] = left = 0
            notes.append(f"{s['email']}: STOPPED for today, too many bounced emails")
        left_today += left
    ready = sendable(con, cfg)
    # Hold everything until the list for the day is complete, so a batch goes out whole.
    need = min(left_today, s_cfg["batch_size"])
    deadline = s_cfg.get("send_anyway_after_hour")
    late = deadline is not None and dt.datetime.now().hour >= deadline
    if s_cfg.get("wait_for_full_batch") and not started and not ignore_hours and len(ready) < need and not late:
        notes.append(f"Batch not ready: {len(ready)} of {need} emails prepared and approved. Nothing sent yet"
                     + (f"; after {deadline}:00 the ready ones go out anyway." if deadline is not None else "."))
        return {}, notes
    batches = {s["email"]: [] for s in senders}
    lead_cap = (cfg.get("leads") or {}).get("per_day_per_account", 3)
    lead_room = {s["email"]: max(0, lead_cap - con.execute(
        "SELECT COUNT(*) FROM jobs WHERE sender=? AND job_type='lead' AND sent_at LIKE ?",
        (s["email"], db.today() + "%")).fetchone()[0]) for s in senders}
    prospect_cap = (cfg.get("prospects") or {}).get("per_day_per_account", 20)
    prospect_room = {s["email"]: max(0, prospect_cap - con.execute(
        "SELECT COUNT(*) FROM jobs WHERE sender=? AND job_type='prospect' AND sent_at LIKE ?",
        (s["email"], db.today() + "%")).fetchone()[0]) for s in senders}
    for row, to in ready:
        open_senders = [e for e in batches if len(batches[e]) < room[e]]
        if row["job_type"] in ("lead", "prospect"):
            kind_room = lead_room if row["job_type"] == "lead" else prospect_room
            open_senders = [e for e in open_senders
                            if sum(r["job_type"] == row["job_type"] for r, _ in batches[e]) < kind_room[e]]
            if not open_senders:
                continue
        if not open_senders:
            break
        # fewest in this run first, then most room left: keeps the split even
        pick = min(open_senders, key=lambda e: (len(batches[e]), -room[e]))
        batches[pick].append((row, to))
    return batches, notes


def label_for(row):
    return f"{LABEL_ROOT}/{TRACK_LABEL.get(row['track'], 'Other')}/{row['category'].replace('/', '-')}"


def apply_label(sender, to, label):
    """File the just-sent message under a Gmail label (only you see labels). Best effort."""
    if "gmail" not in sender["smtp"]:
        return
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        m.login(sender["email"], sender["password"])
        _, boxes = m.list()
        sent_box = next((re.search(r'"([^"]+)"$', b.decode()).group(1) for b in boxes if b"\\Sent" in b), None)
        m.select(f'"{sent_box}"')
        for _ in range(4):  # the copy can take a moment to appear in Sent
            _, data = m.uid("SEARCH", None, "X-GM-RAW", f'"to:{to} newer_than:1d"')
            uids = data[0].split()
            if uids:
                m.uid("STORE", uids[-1], "+X-GM-LABELS", f'("{label}")')
                break
            time.sleep(3)
        m.logout()
    except Exception:
        pass


def _send_batch(sender, batch, cfg, master, results, redirect, lock):
    con = db.connect()   # the account comes from JOBBOT_HOME, which every thread of this run shares
    gap = cfg["sending"]["gap_seconds"]
    name = master["name"].replace(" R.", "")
    with render.PdfPrinter(cfg["browser"]["channel"]) as pdf:
        for i, (row, to) in enumerate(batch):
            if i:
                time.sleep(random.uniform(*gap))
            if not redirect:
                # Claim this address first. If the bot is stopped between here and Gmail's answer,
                # the job stays 'sending' and is never sent again automatically.
                try:
                    claimed = con.execute("UPDATE jobs SET send_status='sending', sender=?, to_email=? "
                                          "WHERE id=? AND send_status='approved'", (sender["email"], to, row["id"])).rowcount
                    con.commit()
                except sqlite3.IntegrityError:
                    con.rollback()
                    con.execute("UPDATE jobs SET send_status='rejected', send_error='duplicate: this address was already "
                                "emailed' WHERE id=?", (row["id"],))
                    con.commit()
                    claimed = 0
                if not claimed:
                    with lock:
                        results.append((sender["email"], row["id"], to, "skipped (already sent or in progress)", ""))
                    continue
            try:
                # resume carries the address it is sent from, so the PDF and the reply address agree
                out = None   # leads go without an attachment
                if row["job_type"] != "lead" and row["track"] != "services":   # service offers go without a resume
                    tailored = json.loads(row["tailored_json"]) if row["tailored_json"] else None
                    html = render.to_html(render.build(row["region"], email=sender["email"], master=master,
                                                       tailored=tailored))
                    render.check_region_rule(html, row["region"])
                    out = Path(row["resume_path"]).parent / f"send_{sender['n']}" / Path(row["resume_path"]).name
                    pdf.save(html, out)
                cc = None if redirect else mailer.cc_for(sender["email"], cfg)
                body = row["email_body"].replace("{sender}", sender["email"])   # prospect emails sign with the sending address
                msg = mailer.build(sender["email"], name, redirect or to, row["email_subject"], body, out,
                                   cc=cc)
                mailer.send(sender, msg)
                if not redirect:
                    con.execute("UPDATE jobs SET send_status='sent', sender=?, to_email=?, cc_email=?, sent_at=?, "
                                "resume_path=? WHERE id=?",
                                (sender["email"], to, cc, db.now(), str(out) if out else row["resume_path"], row["id"]))
                    con.commit()
                apply_label(sender, redirect or to, label_for(row))
                with lock:
                    results.append((sender["email"], row["id"], redirect or to, "sent", dt.datetime.now().strftime("%H:%M:%S")))
            except Exception as e:
                if not redirect:
                    con.execute("UPDATE jobs SET send_status='failed', to_email=NULL, sender=?, send_error=? WHERE id=?",
                                (sender["email"], f"{type(e).__name__}: {str(e)[:150]}", row["id"]))
                    con.commit()
                with lock:
                    results.append((sender["email"], row["id"], to, f"FAILED {type(e).__name__}", ""))
    con.close()


def send_all(batches, senders, cfg, redirect=None):
    """One thread per sender account, so the accounts send at the same time."""
    master, results, lock = render.load_master(), [], threading.Lock()
    by_email = {s["email"]: s for s in senders}
    threads = [threading.Thread(target=_send_batch, args=(by_email[e], b, cfg, master, results, redirect, lock))
               for e, b in batches.items() if b]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def check_inbox(sender, con, days=7):
    """Read bounce notices and replies for emails this account sent. Opens the inbox read-only.
    Returns (bounced, replied) counts newly recorded."""
    rows = con.execute("SELECT id, to_email FROM jobs WHERE sender=? AND send_status='sent' AND outcome IS NULL",
                       (sender["email"],)).fetchall()
    if not rows or "gmail" not in sender["smtp"]:
        return 0, 0
    waiting = {r["to_email"].lower(): r["id"] for r in rows}
    bounced = replied = 0
    m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        m.login(sender["email"], sender["password"])
        m.select("INBOX", readonly=True)
        _, data = m.uid("SEARCH", None, "X-GM-RAW", f'"from:mailer-daemon newer_than:{days}d"')
        for uid in data[0].split()[-50:]:
            _, parts = m.uid("FETCH", uid, "(BODY.PEEK[TEXT]<0.6000>)")
            text = b" ".join(p[1] for p in parts if isinstance(p, tuple)).decode("utf-8", "replace").lower()
            for addr, job_id in list(waiting.items()):
                if addr in text:
                    con.execute("UPDATE jobs SET outcome='bounced', outcome_at=? WHERE id=?", (db.now(), job_id))
                    waiting.pop(addr); bounced += 1
        for addr, job_id in list(waiting.items()):
            _, data = m.uid("SEARCH", None, "X-GM-RAW", f'"from:{addr} newer_than:30d"')
            for uid in data[0].split()[-5:]:
                _, parts = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (AUTO-SUBMITTED)])")
                head = b" ".join(p[1] for p in parts if isinstance(p, tuple)).lower()
                if b"auto-" in head:
                    # "we received your application" robots do not count as a reply, but are noted
                    con.execute("UPDATE jobs SET auto_ack=1 WHERE id=?", (job_id,))
                    continue
                con.execute("UPDATE jobs SET outcome='replied', outcome_at=? WHERE id=?", (db.now(), job_id))
                replied += 1
                break
        con.commit()
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return bounced, replied


def bounce_stop(con, sender_email, cfg):
    """True when this account bounced too much today and must stop sending until tomorrow."""
    sent = sent_today(con, sender_email)
    bounced = con.execute("SELECT COUNT(*) FROM jobs WHERE sender=? AND outcome='bounced' AND sent_at LIKE ?",
                          (sender_email, db.today() + "%")).fetchone()[0]
    return bounced >= 2 and bounced / max(sent, 1) > cfg["sending"]["max_bounce_rate"]


LOCK = HomePath("send.lock")


class SendLock:
    """Only one send may run at a time (a scheduled cycle and a manual run must not overlap)."""

    def __enter__(self):
        import os
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 3 * 3600:
            try:
                owner = int(LOCK.read_text().strip() or 0)
            except ValueError:
                owner = 0
            # a lock left by a send that was killed must not block sending for hours
            if owner and owner != os.getpid() and pid_alive(owner):
                raise RuntimeError("Another send is already running. Nothing sent by this run.")
        LOCK.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        LOCK.unlink(missing_ok=True)


def pid_alive(pid):
    if os.name != "nt":   # a Linux server: signal 0 only asks whether the process exists
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return int(pid) > 0
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == 259   # STILL_ACTIVE


LOOP_LOCK = HomePath("loop.lock")


class LoopLock:
    """Only one repeating loop at a time (the scheduled task and a manual start must not both run)."""

    def __enter__(self):
        import os
        if LOOP_LOCK.exists():
            try:
                other = int(LOOP_LOCK.read_text().strip())
            except ValueError:
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                raise RuntimeError(f"The loop is already running (process {other}). Stop that one first, or use "
                                   f"`jobbot cycle` without --loop for a single run.")
        LOOP_LOCK.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        LOOP_LOCK.unlink(missing_ok=True)
