"""Schedules and the queue of runs, so the bot can be started, stopped and timed from anywhere.

A schedule belongs to one account: every day at a time on chosen weekdays, or every N minutes.
When one falls due, a run is queued. The service (`jobbot serve`) starts queued runs, each as its
own process working only in that account's folder, a few at a time, and records how each ended.
Nothing here depends on Windows Task Scheduler or on one particular computer."""
import datetime as dt
import os
import subprocess
import sys
import threading
from zoneinfo import ZoneInfo

from . import APP
from . import control as ct

RUN_COMMAND = [sys.executable, "-m", "jobbot", "cycle"]   # one full iteration for the account in JOBBOT_HOME
MAX_PARALLEL = 2          # runs at the same time, across all accounts
MIN_INTERVAL = 15         # minutes; a shorter interval would hammer the job sites
TICK_SECONDS = 15
OPEN = ("queued", "running")

_procs = {}               # cycle id -> the process this service started for it
_lock = threading.Lock()  # one tick at a time


# ---------- schedules ----------

def schedule_problem(s):
    """Why this schedule cannot be saved, or None when it can."""
    if s.get("kind") == "daily":
        try:
            dt.time.fromisoformat(s.get("at") or "")
        except ValueError:
            return "at must be a time such as 08:30"
        days = s.get("days")
        if not isinstance(days, list) or not days or any(d not in range(7) for d in days):
            return "days must be a list of weekdays, 0 = Monday ... 6 = Sunday"
    elif s.get("kind") == "interval":
        if not isinstance(s.get("every_minutes"), int) or s["every_minutes"] < MIN_INTERVAL:
            return f"every_minutes must be a whole number, at least {MIN_INTERVAL}"
    else:
        return "kind must be daily or interval"
    return None


def next_run(s, timezone, after=None):
    """When this schedule next falls due, as a UTC time string. `after` defaults to now."""
    after = after or dt.datetime.now(dt.timezone.utc)
    if s["kind"] == "interval":
        return (after + dt.timedelta(minutes=s["every_minutes"])).isoformat(timespec="seconds")
    local = after.astimezone(ZoneInfo(timezone))
    at, days = dt.time.fromisoformat(s["at"]), s["days"] if isinstance(s["days"], list) else _days(s["days"])
    for ahead in range(8):
        day = local.date() + dt.timedelta(days=ahead)
        when = dt.datetime.combine(day, at, tzinfo=local.tzinfo)
        if when > local and day.weekday() in days:
            return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    raise ValueError("no day to run on")


def _days(text):
    return [int(d) for d in (text or "").split(",") if d != ""]


def _schedule(row):
    return row | {"days": _days(row["days"]) if row["kind"] == "daily" else None, "enabled": bool(row["enabled"])}


def schedules(account_id):
    return [_schedule(r) for r in ct._all("SELECT * FROM schedules WHERE account_id=? ORDER BY id", account_id)]


def schedule_save(who, s, schedule_id=None):
    """Create a schedule, or change one of this account's. Returns it, or None if there is no such schedule."""
    old = ct._one("SELECT * FROM schedules WHERE id=? AND account_id=?", schedule_id, who["account_id"]) if schedule_id else None
    if schedule_id and not old:
        return None
    s = {**(_schedule(old) if old else {"enabled": True, "name": ""}), **s}
    reason = schedule_problem(s)
    if reason:
        raise ValueError(reason)
    daily = s["kind"] == "daily"
    fields = (s.get("name") or "", s["kind"], s["at"] if daily else None, ",".join(map(str, sorted(set(s["days"])))) if daily else None,
              None if daily else s["every_minutes"], int(bool(s["enabled"])), next_run(s, who["timezone"]))
    if old:
        ct._write("UPDATE schedules SET name=?, kind=?, at=?, days=?, every_minutes=?, enabled=?, next_run_at=? WHERE id=?",
                  *fields, schedule_id)
    else:
        _, schedule_id = ct._write("INSERT INTO schedules(name, kind, at, days, every_minutes, enabled, next_run_at, account_id, "
                                   "created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   *fields, who["account_id"], ct.now(), who["user_id"])
    return _schedule(ct._one("SELECT * FROM schedules WHERE id=?", schedule_id))


def schedule_delete(account_id, schedule_id):
    return bool(ct._write("DELETE FROM schedules WHERE id=? AND account_id=?", schedule_id, account_id)[0])


# ---------- runs ----------

def cycles(account_id, limit=30):
    return ct._all("SELECT * FROM cycles WHERE account_id=? ORDER BY id DESC LIMIT ?", account_id, limit)


def cycle(account_id, cycle_id):
    return ct._one("SELECT * FROM cycles WHERE id=? AND account_id=?", cycle_id, account_id)


def request(account_id, trigger="manual", user_id=None, schedule_id=None):
    """Queue a run for this account. Returns (the run, True), or (the run already waiting or going, False)."""
    con = ct.connect()
    try:
        con.execute("BEGIN IMMEDIATE")   # two requests at once must not both queue a run
        waiting = con.execute("SELECT * FROM cycles WHERE account_id=? AND status IN ('queued','running') LIMIT 1",
                              (account_id,)).fetchone()
        if waiting:
            con.rollback()
            return dict(waiting), False
        new = con.execute("INSERT INTO cycles(account_id, schedule_id, trigger, status, requested_at, requested_by) "
                          "VALUES(?,?,?,'queued',?,?)", (account_id, schedule_id, trigger, ct.now(), user_id)).lastrowid
        con.commit()
        return dict(con.execute("SELECT * FROM cycles WHERE id=?", (new,)).fetchone()), True
    finally:
        con.close()


def _finish(cycle_id, status, note="", exit_code=None):
    ct._write("UPDATE cycles SET status=?, note=?, exit_code=?, finished_at=? WHERE id=? AND status IN ('queued','running')",
              status, note, exit_code, ct.now(), cycle_id)


def _kill(pid):
    """Stop a run and everything it started (the browser it drives, for one)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def stop(account_id, cycle_id):
    """Stop a run of this account that is waiting or going. Returns False if there is nothing to stop."""
    run = cycle(account_id, cycle_id)
    if not run or run["status"] not in OPEN:
        return False
    if run["status"] == "running" and run["pid"]:
        _kill(run["pid"])
    _finish(cycle_id, "stopped", "stopped on request")
    _procs.pop(cycle_id, None)
    return True


def log_tail(account_id, cycle_id, lines=200):
    run = cycle(account_id, cycle_id)
    if not run or not run["log_file"] or not os.path.exists(run["log_file"]):
        return None
    with open(run["log_file"], encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:])


def _alive(pid):
    from .sendqueue import pid_alive
    return bool(pid) and pid_alive(pid)


def _start(run):
    # Claim the run first, so two services ticking at the same moment cannot both start it.
    if not ct._write("UPDATE cycles SET status='running', started_at=? WHERE id=? AND status='queued'", ct.now(), run["id"])[0]:
        return
    acc = ct.account(run["account_id"])
    home = ct.home_of(acc)
    reason = ct.ready_to_run(acc)
    if not reason and (home / "loop.lock").exists():
        try:
            if _alive(int((home / "loop.lock").read_text().strip() or 0)):
                reason = "the continuous loop is already running for this account"
        except (OSError, ValueError):
            pass
    if reason:
        return _finish(run["id"], "skipped", reason)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    log = home / "logs" / f"cycle-{run['id']}.log"
    env = {**os.environ, "JOBBOT_HOME": str(home), "PYTHONIOENCODING": "utf-8", "COLUMNS": "160"}
    if os.name != "nt":
        env["TZ"] = acc["timezone"]   # the run's clock is the customer's, so sending hours are theirs (Windows ignores this)
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(RUN_COMMAND, cwd=APP, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                                   else {"start_new_session": True}))
    _procs[run["id"]] = proc
    ct._write("UPDATE cycles SET pid=?, log_file=? WHERE id=?", proc.pid, str(log), run["id"])


def tick():
    """One pass: note runs that ended, queue schedules that fell due, start queued runs. Safe to call at any time."""
    with _lock:
        for run in ct._all("SELECT * FROM cycles WHERE status='running'"):
            proc = _procs.get(run["id"])
            if proc is not None:
                code = proc.poll()
                if code is not None:
                    _procs.pop(run["id"], None)
                    _finish(run["id"], "done" if code == 0 else "failed", "" if code == 0 else f"ended with code {code}", code)
            elif run["pid"] and not _alive(run["pid"]):   # started by an earlier service that has since stopped
                _finish(run["id"], "failed", "the service was restarted while this run was going; how it ended is not known")
        moment = ct.now()
        for s in ct._all("SELECT s.*, a.timezone, a.status account_status FROM schedules s JOIN accounts a ON a.id=s.account_id "
                         "WHERE s.enabled=1 AND s.next_run_at<=?", moment):
            if s["account_status"] == "active":
                request(s["account_id"], "schedule", schedule_id=s["id"])
            ct._write("UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=?", moment, next_run(_schedule(s), s["timezone"]), s["id"])
        room = MAX_PARALLEL - ct._one("SELECT COUNT(*) n FROM cycles WHERE status='running'")["n"]
        for run in ct._all("SELECT * FROM cycles WHERE status='queued' ORDER BY id LIMIT ?", max(room, 0)):
            try:
                _start(run)
            except Exception as e:
                _finish(run["id"], "failed", f"could not start: {type(e).__name__}: {e}"[:300])


def run_forever(stop_event):
    while not stop_event.wait(TICK_SECONDS):
        try:
            tick()
        except Exception as e:   # one bad pass must not end the service
            print(f"scheduler: {type(e).__name__}: {e}", flush=True)
