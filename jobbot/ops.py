"""Running the service: backups and restore, structured logs, health, metrics and alerts.

Service-wide settings come from the environment (or DATA/.env), not from an account's settings:

    JOBBOT_BACKUP_HOUR     hour of the day (server clock) for the automatic backup, default 3
    JOBBOT_BACKUP_KEEP     daily backups kept, default 7 (plus one per week for 4 weeks)
    JOBBOT_BACKUP_COPY_TO  a second folder that gets a copy of every backup (another disk, a synced folder)
    JOBBOT_ALERT_EMAIL     where alerts are emailed (sent from the owner's first sending account)
    JOBBOT_METRICS_TOKEN   lets a monitoring system read /metrics without a user sign-in

Backups use SQLite's online backup, so the service keeps running while one is made. Each backup is checked
(integrity check and row counts) before it is kept, and `restore_drill` proves a backup can be restored by
restoring the newest one into a scratch folder and comparing it with the live database.
"""
import datetime as dt
import gzip
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from dotenv import dotenv_values

from . import DATA

COUNTED = ("postings", "applications", "accounts", "users", "payments", "acct_documents", "acct_settings")


def env(name, default=None):
    """A service setting: the real environment first, then DATA/.env."""
    if os.environ.get(name):
        return os.environ[name]
    return dotenv_values(DATA / ".env").get(name) or default


# ---------------------------------------------------------------- structured logs

class _Json(logging.Formatter):
    def format(self, record):
        out = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        for key, value in record.__dict__.items():
            if key.startswith("f_"):
                out[key[2:]] = value
        if record.exc_info:
            out["error"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


_configured = False


def log():
    """The service log: one JSON object per line in DATA/logs/service.jsonl, rotated at 10 MB, 10 files kept."""
    global _configured
    logger = logging.getLogger("jobbot")
    if not _configured:
        _configured = True
        folder = DATA / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(folder / "service.jsonl", maxBytes=10_000_000, backupCount=10,
                                                       encoding="utf-8", delay=True)
        handler.setFormatter(_Json())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def event(msg, level="info", **fields):
    """Write one structured line, e.g. event("run finished", account=3, status="done")."""
    getattr(log(), level)(msg, extra={f"f_{k}": v for k, v in fields.items()})


# ---------------------------------------------------------------- backups

def backup_dir():
    return DATA / "backup" / "auto"


def _counts(con):
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED if t in have}


def verify(path):
    """(ok, detail, counts) for a database file or a .db.gz backup."""
    path = Path(path)
    tmp = None
    try:
        if path.suffix == ".gz":
            tmp = Path(tempfile.mkdtemp(prefix="jobbot-verify-")) / "check.db"
            with gzip.open(path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            target = tmp
        else:
            target = path
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            result = con.execute("PRAGMA quick_check").fetchone()[0]
            counts = _counts(con)
        finally:
            con.close()
        return result == "ok", result, counts
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    finally:
        if tmp:
            shutil.rmtree(tmp.parent, ignore_errors=True)


def backup(reason="manual"):
    """Make, check and keep one compressed backup of the database. Returns a dict describing it."""
    from . import db
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = folder / f"jobbot-{stamp}.db"
    started = time.time()
    src = sqlite3.connect(db.DB_FILE, timeout=60)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst, pages=4096, sleep=0.01)   # in steps, so writers are never held up for long
    finally:
        dst.close()
        src.close()
    ok, detail, counts = verify(raw)
    if not ok:
        raw.unlink(missing_ok=True)
        raise_alert("backup", f"the backup made at {stamp} failed its check: {detail}", key=f"backup-{stamp}")
        raise RuntimeError(f"backup failed its check: {detail}")
    packed = raw.with_suffix(".db.gz")
    with open(raw, "rb") as f, gzip.open(packed, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g, 1 << 20)
    raw.unlink()
    (folder / (packed.name + ".json")).write_text(json.dumps({"made_at": stamp, "reason": reason, "counts": counts}), "utf-8")
    copy_to = env("JOBBOT_BACKUP_COPY_TO")
    copied = None
    if copy_to:
        try:
            Path(copy_to).mkdir(parents=True, exist_ok=True)
            shutil.copy2(packed, Path(copy_to) / packed.name)
            copied = str(Path(copy_to) / packed.name)
        except OSError as e:
            raise_alert("backup", f"the backup could not be copied to {copy_to}: {e}", key=f"backup-copy-{dt.date.today()}")
    prune()
    info = {"file": str(packed), "size_mb": round(packed.stat().st_size / 1e6, 1), "seconds": round(time.time() - started, 1),
            "counts": counts, "copied_to": copied}
    event("backup made", **info)
    return info


def backups():
    """Every kept backup, newest first."""
    return sorted(backup_dir().glob("jobbot-*.db.gz"), reverse=True)


def prune():
    """Keep the newest JOBBOT_BACKUP_KEEP daily backups plus the newest of each of the last 4 weeks."""
    keep_days = int(env("JOBBOT_BACKUP_KEEP", 7))
    files = backups()
    keep, days, weeks = set(), [], []
    for f in files:
        day = dt.datetime.strptime(f.name[7:15], "%Y%m%d").date()
        if day not in days and len(days) < keep_days:
            days.append(day)
            keep.add(f)
        week = day.isocalendar()[:2]
        if week not in weeks and len(weeks) < 4:
            weeks.append(week)
            keep.add(f)
    for f in files:
        if f not in keep:
            f.unlink(missing_ok=True)
            Path(str(f) + ".json").unlink(missing_ok=True)


def restore(path, target=None):
    """Put a backup in place of the database (or at `target`). The live file is kept as jobbot.db.before-restore-<time>.
    The service and the loop must be stopped first; this refuses while the loop holds its lock."""
    from . import db, sendqueue as sq
    path = Path(path)
    target = Path(target or db.DB_FILE)
    if target == Path(db.DB_FILE) and sq.holder(DATA / "loop.lock"):
        raise RuntimeError("the loop is running; stop it (and the two sites) before restoring")
    ok, detail, counts = verify(path)
    if not ok:
        raise RuntimeError(f"that backup failed its check: {detail}")
    staging = target.with_name(target.name + ".restoring")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as src, open(staging, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    kept = None
    if target.exists():
        kept = target.with_name(f"{target.name}.before-restore-{dt.datetime.now():%Y%m%d-%H%M%S}")
        target.replace(kept)
    for extra in ("-wal", "-shm"):
        Path(str(target) + extra).unlink(missing_ok=True)
    staging.replace(target)
    event("backup restored", file=str(path), target=str(target), previous=str(kept))
    return {"restored": str(target), "previous_kept_as": str(kept) if kept else None, "counts": counts}


def restore_drill():
    """Restore the newest backup into a scratch folder and compare it with the live database. Proves restores work."""
    from . import db
    files = backups()
    if not files:
        raise RuntimeError("there is no backup yet")
    scratch = Path(tempfile.mkdtemp(prefix="jobbot-drill-"))
    try:
        restored = restore(files[0], scratch / "jobbot.db")
        live = sqlite3.connect(f"file:{db.DB_FILE}?mode=ro", uri=True)
        try:
            now_counts = _counts(live)
        finally:
            live.close()
        # rows only grow between a backup and now (postings, applications); fewer rows now would mean data was lost
        lost = {t: (n, now_counts.get(t, 0)) for t, n in restored["counts"].items() if t in ("postings", "accounts", "users")
                and now_counts.get(t, 0) < n}
        result = {"backup": files[0].name, "restored_counts": restored["counts"], "live_counts": now_counts, "ok": True,
                  "note": "restored and checked"}
        if lost:
            result.update(ok=False, note=f"the live database has fewer rows than the backup: {lost}")
        event("restore drill", **result)
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def backup_due():
    """True when today's automatic backup has not been made and its hour has come."""
    hour = int(env("JOBBOT_BACKUP_HOUR", 3))
    now = dt.datetime.now()
    if now.hour < hour:
        return False
    return not any(f.name.startswith(f"jobbot-{now:%Y%m%d}") for f in backups())


# ---------------------------------------------------------------- alerts

def raise_alert(kind, message, key=None, account_id=None):
    """Record a problem once (by key) and email it to JOBBOT_ALERT_EMAIL. Returns True if it was new."""
    from . import control as ct
    key = key or f"{kind}-{account_id}-{message[:80]}"
    n, _ = ct._write("INSERT OR IGNORE INTO alerts(at, kind, account_id, message, key) VALUES(?,?,?,?,?)",
                     ct.now(), kind, account_id, message[:1000], key)
    if not n:
        return False
    event("alert", level="warning", kind=kind, account=account_id, message=message)
    to = env("JOBBOT_ALERT_EMAIL")
    if to:
        try:
            _email_alert(to, kind, message)
            ct._write("UPDATE alerts SET emailed_at=? WHERE key=?", ct.now(), key)
        except Exception as e:
            event("alert email failed", level="error", error=str(e))
    return True


def _email_alert(to, kind, message):
    import smtplib
    from email.message import EmailMessage
    from . import mailer, use_home
    with use_home(DATA):
        senders = [s for s in mailer.senders() if s.get("password")]
    if not senders:
        raise RuntimeError("no sending account with a password in DATA/.env")
    s = senders[0]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s["email"], to, f"[JobPilot alert] {kind}"
    msg.set_content(f"{message}\n\nOpen the admin panel, System tab, to see all open alerts.")
    with smtplib.SMTP_SSL(s.get("smtp") or "smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(s["email"], s["password"])
        smtp.send_message(msg)


def open_alerts(limit=100):
    from . import control as ct
    return ct._all("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id DESC LIMIT ?", limit)


def resolve_alert(alert_id):
    from . import control as ct
    return ct._write("UPDATE alerts SET resolved_at=? WHERE id=? AND resolved_at IS NULL", ct.now(), alert_id)[0] == 1


def check():
    """Look for problems worth an alert: failed runs, accounts that stopped sending, a stale backup, a full disk.
    Cheap enough to call on every scheduler tick."""
    from . import control as ct
    found = []
    for run in ct._all("SELECT c.id, c.account_id, c.note, a.name FROM cycles c JOIN accounts a ON a.id=c.account_id "
                       "WHERE c.status='failed' AND c.finished_at>=?", _ago(hours=24)):
        if raise_alert("run failed", f"run {run['id']} for {run['name']} failed: {run['note'] or 'see its log'}",
                       key=f"run-{run['id']}", account_id=run["account_id"]):
            found.append(f"run {run['id']}")
    newest = backups()[:1]
    if newest and time.time() - newest[0].stat().st_mtime > 36 * 3600:
        if raise_alert("backup", f"the newest backup ({newest[0].name}) is more than 36 hours old",
                       key=f"backup-stale-{dt.date.today()}"):
            found.append("stale backup")
    free = shutil.disk_usage(DATA).free / 1e9
    if free < 2:
        if raise_alert("disk", f"only {free:.1f} GB free on the data disk", key=f"disk-{dt.date.today()}"):
            found.append("disk")
    found += _sending_stopped()
    return found


def _sending_stopped():
    """A sending account paused for bounces today is worth knowing about at once."""
    from . import control as ct, db, use_home
    found = []
    for acc in ct._all("SELECT id, name, home FROM accounts WHERE status='active'"):
        try:
            with use_home(ct.home_of(acc)):
                con = db.connect()
                try:
                    rows = con.execute("SELECT sender, COUNT(*) n FROM jobs WHERE outcome='bounced' AND outcome_at LIKE ? "
                                       "GROUP BY sender HAVING n>=3", (db.today() + "%",)).fetchall()
                finally:
                    con.close()
        except Exception:
            continue
        for r in rows:
            if raise_alert("sending stopped", f"{acc['name']}: {r['sender']} had {r['n']} bounces today and has paused",
                           key=f"bounce-{acc['id']}-{r['sender']}-{db.today()}", account_id=acc["id"]):
                found.append(f"bounces {r['sender']}")
    return found


def _ago(**kw):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)).isoformat(timespec="seconds")


# ---------------------------------------------------------------- health and metrics

STARTED = time.time()
HEARTBEAT = {"scheduler": None}


def health(deep=False):
    """(ok, details). Shallow: the process answers. Deep: the database answers, the scheduler is ticking, disk has room."""
    out = {"ok": True, "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "uptime_s": int(time.time() - STARTED)}
    if not deep:
        return True, out
    problems = []
    try:
        from . import control as ct
        ct._one("SELECT 1")
        out["database"] = "ok"
    except Exception as e:
        out["database"] = f"error: {e}"
        problems.append("database")
    beat = HEARTBEAT["scheduler"]
    out["scheduler_last_tick_s"] = None if beat is None else int(time.time() - beat)
    if beat is not None and time.time() - beat > 300:
        problems.append("scheduler")
    out["disk_free_gb"] = round(shutil.disk_usage(DATA).free / 1e9, 1)
    if out["disk_free_gb"] < 1:
        problems.append("disk")
    newest = backups()[:1]
    out["last_backup"] = newest[0].name if newest else None
    out["open_alerts"] = len(open_alerts())
    out["ok"] = not problems
    if problems:
        out["problems"] = problems
    return not problems, out


def metrics():
    """Prometheus text format."""
    from . import control as ct, db
    lines = []

    def put(name, value, help_, labels=""):
        lines.append(f"# HELP jobbot_{name} {help_}")
        lines.append(f"# TYPE jobbot_{name} gauge")
        lines.append(f"jobbot_{name}{labels} {value}")

    put("uptime_seconds", int(time.time() - STARTED), "Seconds since the service started")
    put("accounts", ct._one("SELECT COUNT(*) n FROM accounts WHERE status='active'")["n"], "Active accounts")
    put("paid_accounts", ct._one("SELECT COUNT(*) n FROM accounts WHERE paid_until>=?", ct.now())["n"], "Accounts with a paid period running")
    for status in ("queued", "running"):
        put(f"runs_{status}", ct._one("SELECT COUNT(*) n FROM cycles WHERE status=?", status)["n"], f"Runs {status}")
    since = _ago(hours=24)
    for status in ("done", "failed", "skipped", "stopped"):
        put(f"runs_{status}_24h", ct._one("SELECT COUNT(*) n FROM cycles WHERE status=? AND finished_at>=?", status, since)["n"],
            f"Runs that ended {status} in the last 24 hours")
    con = sqlite3.connect(f"file:{db.DB_FILE}?mode=ro", uri=True, timeout=30)
    try:
        put("postings", con.execute("SELECT COUNT(*) FROM postings").fetchone()[0], "Job posts in the shared pool")
        put("emails_sent_today", con.execute("SELECT COUNT(*) FROM applications WHERE send_status='sent' AND sent_at LIKE ?",
                                             (dt.date.today().isoformat() + "%",)).fetchone()[0], "Emails sent today, all accounts")
        put("bounces_today", con.execute("SELECT COUNT(*) FROM applications WHERE outcome='bounced' AND outcome_at LIKE ?",
                                         (dt.date.today().isoformat() + "%",)).fetchone()[0], "Bounces recorded today, all accounts")
    finally:
        con.close()
    put("open_alerts", len(open_alerts()), "Alerts not yet resolved")
    newest = backups()[:1]
    put("last_backup_age_seconds", int(time.time() - newest[0].stat().st_mtime) if newest else -1, "Age of the newest backup")
    put("disk_free_bytes", shutil.disk_usage(DATA).free, "Free space on the data disk")
    return "\n".join(lines) + "\n"


def housekeeping():
    """Called by the scheduler on every tick: heartbeat, alert checks (every 5 minutes) and the daily backup."""
    HEARTBEAT["scheduler"] = time.time()
    last = HEARTBEAT.get("checked", 0)
    if time.time() - last >= 300:
        HEARTBEAT["checked"] = time.time()
        try:
            check()
        except Exception as e:
            event("alert check failed", level="error", error=f"{type(e).__name__}: {e}")
    if backup_due() and not HEARTBEAT.get("backing_up"):
        HEARTBEAT["backing_up"] = True
        try:
            backup("daily")
        except Exception as e:
            raise_alert("backup", f"the daily backup failed: {type(e).__name__}: {e}", key=f"backup-fail-{dt.date.today()}")
        finally:
            HEARTBEAT["backing_up"] = False
