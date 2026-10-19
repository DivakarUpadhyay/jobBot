"""Console commands for review, approval, sending and the full 30-minute cycle."""
import json
import time
from typing import Optional

import typer
from rich.table import Table

from . import cleanup as cl, db, leads as ld, mailer, render, report as rp, sendqueue as sq
from .cli import app, console, contacts, enrich, match, scrape, status, tailor
from .config import PROCESSES, enabled, load_config, set_setting


def _pending(con):
    """Tailored jobs with an email that still wait for your decision, best first."""
    return con.execute("""SELECT * FROM jobs WHERE status='matched' AND emails!='[]' AND (resume_path IS NOT NULL OR job_type='lead')
                          AND send_status IS NULL AND source!='freelancer' ORDER BY score DESC""").fetchall()


def _auto_approve(con, cfg):
    """Apply the review mode. Doubtful addresses (marked CHECK) and lead emails always wait for you."""
    mode = cfg["sending"]["review"]
    if mode == "manual":
        return 0
    floor = cfg["sending"]["semi_auto_min_score"] if mode == "semi" else 0
    # an email that is only the fixed template is not specific to the job description: it waits
    # until an AI engine is available again and has rewritten it
    need_ai = " AND tailored_by='ai'" if (cfg.get("ai") or {}).get("require_ai_for_sending") else ""
    cur = con.execute("""UPDATE jobs SET send_status='approved' WHERE status='matched' AND emails!='[]'
                         AND (resume_path IS NOT NULL OR job_type='lead') AND send_status IS NULL AND source!='freelancer'
                         AND score>=? AND COALESCE(email_source,'') NOT LIKE 'CHECK%' AND job_type!='lead'
                         -- service offers wait for `prospects approve --services`; an email the send step held back
                         -- (placeholder left, Chinese text abroad) waits until it is rewritten
                         AND source!='services' AND send_error IS NOT 'a placeholder is left in the email'
                         AND send_error IS NOT 'Chinese text in an email that is not for Taiwan'""" + need_ai, (floor,))
    con.commit()
    return cur.rowcount


@app.command()
def queue():
    """Emails waiting for your approval, and emails approved but not yet sent."""
    con = db.connect()
    t = Table("ID", "State", "Score", "Track", "Field", "Role", "Company", "To", "Email found")
    rows = _pending(con) + con.execute("SELECT * FROM jobs WHERE send_status='approved' ORDER BY score DESC").fetchall()
    for r in rows:
        src = r["email_source"] or "in the job post"
        t.add_row(str(r["id"]), r["send_status"] or "needs review", str(r["score"]), r["track"], r["category"],
                  r["title"][:36], r["company"][:20], json.loads(r["emails"])[0],
                  f"[yellow]{src[:44]}[/yellow]" if src.startswith("CHECK") else src[:44])
    console.print(t)
    console.print("Approve: jobbot approve <id> [<id> ...]   Reject: jobbot reject <id>   One by one: jobbot review")


@app.command()
def approve(ids: Optional[list[int]] = typer.Argument(None),
            all_pending: bool = typer.Option(False, "--all", help="Every waiting job application (not leads)."),
            leads: bool = typer.Option(False, "--leads", help="With --all: the waiting lead and collaboration emails instead.")):
    """Approve emails for sending. --all means job applications only; leads need --all --leads."""
    con = db.connect()
    ids = [r["id"] for r in _pending(con) if (r["job_type"] == "lead") == leads] if all_pending else (ids or [])
    done = 0
    for i in ids:
        done += con.execute("UPDATE jobs SET send_status='approved' WHERE id=? AND send_status IS NULL "
                            "AND emails!='[]' AND (resume_path IS NOT NULL OR job_type='lead')", (i,)).rowcount
    con.commit()
    console.print(f"Approved: {done}. They go out on the next `jobbot send`, within the daily limits.")


@app.command()
def reject(ids: list[int]):
    """Never send for these jobs."""
    con = db.connect()
    for i in ids:
        con.execute("UPDATE jobs SET send_status='rejected' WHERE id=? AND COALESCE(send_status,'')!='sent'", (i,))
    con.commit()
    console.print(f"Rejected: {len(ids)}")


@app.command()
def review():
    """Go through waiting emails one by one: y = approve, n = reject, s = skip, q = stop."""
    con = db.connect()
    rows = _pending(con)
    if not rows:
        console.print("Nothing waiting for review.")
        return
    for n, r in enumerate(rows, 1):
        console.rule(f"{n}/{len(rows)}  job #{r['id']}  {r['track']} / {r['category']}  score {r['score']}")
        for line in (f"To:      {json.loads(r['emails'])[0]}   (found: {r['email_source'] or 'in the job post'})",
                     f"Subject: {r['email_subject']}", f"Resume:  {r['resume_path']}", f"Job:     {r['url']}", "",
                     r["email_body"]):
            console.print(line, markup=False, highlight=False)
        answer = typer.prompt("Send this? [y/n/s/q]", default="s").strip().lower()
        if answer == "q":
            break
        if answer in ("y", "n"):
            con.execute("UPDATE jobs SET send_status=? WHERE id=?", ("approved" if answer == "y" else "rejected", r["id"]))
            con.commit()


@app.command()
def send(dry_run: bool = typer.Option(False, help="Show what would be sent; send nothing."),
         now: bool = typer.Option(False, help="Ignore the office-hours rule for this run."),
         redirect: Optional[str] = typer.Option(None, help="TEST: deliver every email to this address instead of HR."),
         per_run: Optional[int] = typer.Option(None, help="Emails per account in this run (still inside the daily cap).")):
    """Send approved emails: split evenly across your accounts, in parallel, inside the daily limits."""
    cfg, con = load_config(), db.connect()
    allowed = [e.lower() for e in cfg["sending"].get("send_from") or []]
    accounts = [s for s in mailer.senders() if s["can_send"] and (not allowed or s["email"].lower() in allowed)]
    if not accounts:
        console.print("No sender to use: check send_from in config.yaml and the app passwords in .env.")
        raise typer.Exit(1)
    if per_run:
        cfg["sending"]["per_run"] = per_run
    auto = _auto_approve(con, cfg)
    if not enabled(cfg, "leads"):
        cfg.setdefault("leads", {})["per_day_per_account"] = 0
    if not enabled(cfg, "prospects"):
        cfg.setdefault("prospects", {})["per_day_per_account"] = 0
    batches, notes = sq.plan(con, cfg, accounts, ignore_hours=now or dry_run or bool(redirect))
    for line in notes:
        console.print(line)
    if auto:
        console.print(f"Auto-approved by review mode '{cfg['sending']['review']}': {auto}")
    stuck = con.execute("SELECT id, to_email, company FROM jobs WHERE send_status='sending'").fetchall()
    for r in stuck:
        console.print(f"[yellow]Job #{r['id']} to {r['to_email']} ({r['company'][:30]}) was interrupted while sending. "
                      f"It will not be sent again. Check the Sent folder, then run: jobbot settle {r['id']} sent  "
                      f"(or: failed)[/yellow]")
    total = sum(len(b) for b in batches.values())
    if not total:
        console.print(f"Nothing to send. Waiting for your review: {len(_pending(con))}")
        return
    t = Table("From", "To", "CC", "Job", "Role", "Label")
    for sender_email, batch in batches.items():
        for row, to in batch:
            t.add_row(sender_email, f"{redirect}  (test redirect)" if redirect else to,
                      "" if redirect else (mailer.cc_for(sender_email, cfg) or ""), f"#{row['id']}",
                      row["title"][:38], sq.label_for(row))
    console.print(t)
    if dry_run:
        console.print("Dry run: nothing sent.")
        return
    try:
        with sq.SendLock(), console.status(
                f"Sending {total} emails from {sum(bool(b) for b in batches.values())} accounts at once..."):
            results = sq.send_all(batches, accounts, cfg, redirect)
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
        return
    r = Table("Time", "From", "To", "Job", "Result")
    for sender_email, job_id, to, state, at in sorted(results, key=lambda x: x[4]):
        r.add_row(at, sender_email, to, f"#{job_id}", state)
    console.print(r)
    if not redirect:
        path, n = rp.write_tracker(con)
        console.print(f"Tracker updated: {n} applications sent in all -> {path}", markup=False, highlight=False)
        rp.write_jobs_book(con, cfg["max_age_days"])   # the job list shows "Email sent" straight away


@app.command()
def inbox():
    """Check each sender's inbox for bounced emails and replies, and show totals per account."""
    cfg, con = load_config(), db.connect()
    t = Table("Account", "Sent today", "Cap", "Sent in all", "Bounced", "Replies", "State")
    for s in mailer.senders():
        note = ""
        if s["can_send"]:
            try:
                sq.check_inbox(s, con)
            except Exception as e:
                note = f"inbox not read: {type(e).__name__}"
        n = lambda where: con.execute(f"SELECT COUNT(*) FROM jobs WHERE sender=? AND {where}", (s["email"],)).fetchone()[0]
        cap, week = sq.cap_today(con, s["email"], cfg)
        state = "[red]stopped today (bounces)[/red]" if sq.bounce_stop(con, s["email"], cfg) else f"ok, week {week}"
        t.add_row(s["email"], str(sq.sent_today(con, s["email"])), str(cap), str(n("send_status='sent'")),
                  str(n("outcome='bounced'")), str(n("outcome='replied'")), note or state)
    console.print(t)
    for r in con.execute("SELECT id, title, company, to_email FROM jobs WHERE outcome='replied' ORDER BY outcome_at DESC LIMIT 10"):
        console.print(f"Reply from {r['to_email']}: #{r['id']} {r['title'][:50]} at {r['company'][:30]}", markup=False)


leads_app = typer.Typer(no_args_is_help=True, help="Lead generation from your lead sheets.")
app.add_typer(leads_app, name="leads")


@leads_app.command("import")
def leads_import(path: str = typer.Argument(..., help="Excel file with the lead columns (Client, Requirement, Email ...).")):
    """Read a lead sheet. Each usable lead gets a short inquiry email and waits for your review."""
    new, skipped, known = ld.import_file(path, load_config(), render.load_master())
    console.print(f"Leads added and ready for review: {new}. Skipped: {skipped}. Already imported before: {known}.")
    con = db.connect()
    for r in con.execute("SELECT skip_reason, COUNT(*) c FROM jobs WHERE source='leads' AND status='skipped' GROUP BY 1"):
        console.print(f"  skipped: {r['skip_reason']} ({r['c']})", markup=False)


@app.command()
def outreach(path: str = typer.Argument(..., help="Excel file: a header row with Email and Organisation / Company / Name."),
             kind: str = typer.Option("collaboration", help="Which wording to use: collaboration.")):
    """Import people or organisations to write to (collaboration). Each gets a draft that waits for your review."""
    new, skipped, known = ld.import_contacts(path, load_config(), render.load_master(), kind)
    console.print(f"Contacts added and ready for review: {new}. Skipped: {skipped}. Already imported before: {known}.")


@app.command()
def prep(job_id: int):
    """Write the interview preparation guide for one job, round by round, from its description and your resume."""
    from . import prep as pr
    cfg, con = load_config(), db.connect()
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        console.print(f"No job with id {job_id}")
        raise typer.Exit(1)
    try:
        with console.status(f"Writing interview prep for {r['title'][:50]} at {r['company'][:30]} ..."):
            text, engine = pr.generate(r, cfg)
    except RuntimeError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(1)
    path = pr.save(con, r, text)
    console.print(text, markup=False, highlight=False)
    console.rule()
    console.print(f"Written by {engine}. Saved: {path}", markup=False, highlight=False)


@leads_app.command("list")
def leads_list(limit: int = 30):
    """Imported leads and where each stands."""
    con = db.connect()
    t = Table("ID", "Stage", "Country", "Organisation", "Requirement", "Deadline", "Email")
    for r in con.execute("SELECT * FROM jobs WHERE source='leads' AND status='matched' ORDER BY deadline LIMIT ?", (limit,)):
        t.add_row(str(r["id"]), rp.stage(r), r["location"].split(", ")[-1], r["company"][:28], r["title"][:40],
                  r["deadline"] or "", json.loads(r["emails"])[0])
    console.print(t)


@app.command()
def settle(job_id: int, result: str = typer.Argument(..., help="sent or failed")):
    """Record what happened to an email that was interrupted mid-send, after you checked the Sent folder."""
    con = db.connect()
    if result == "sent":
        n = con.execute("UPDATE jobs SET send_status='sent', sent_at=COALESCE(sent_at, ?) WHERE id=? AND send_status='sending'",
                        (db.now(), job_id)).rowcount
    elif result == "failed":
        n = con.execute("UPDATE jobs SET send_status='failed', to_email=NULL, send_error='interrupted, not in Sent folder' "
                        "WHERE id=? AND send_status='sending'", (job_id,)).rowcount
    else:
        raise typer.BadParameter("use: sent or failed")
    con.commit()
    console.print(f"Job #{job_id}: recorded as {result}." if n else f"Job #{job_id} is not in the interrupted state.")


@app.command()
def sent(limit: int = typer.Option(40, help="How many of the latest to show.")):
    """Every application that was sent, newest first, and update the Excel tracker."""
    con = db.connect()
    t = Table("No.", "Sent", "Kind", "Company", "Role", "To", "From", "Status")
    rows = con.execute("SELECT * FROM jobs WHERE send_status='sent' ORDER BY sent_at").fetchall()
    for n, r in list(enumerate(rows, 1))[::-1][:limit]:
        t.add_row(str(n), r["sent_at"][5:16].replace("T", " "), rp.TRACK_NAMES.get(r["track"], r["track"]),
                  r["company"][:24], r["title"][:34], r["to_email"], r["sender"].split("@")[0], rp.delivery(r)[:34])
    console.print(t)
    path, n = rp.write_tracker(con)
    console.print(f"{n} applications sent in all. Tracker: {path}", markup=False, highlight=False)


@app.command()
def report(day: Optional[str] = typer.Argument(None, help="Date as YYYY-MM-DD; default: every date with data.")):
    """Write the date-wise CSV files (open in Excel): all jobs scraped that day with emails and status."""
    con = db.connect()
    for d in ([day] if day else rp.days_with_data(con)):
        jobs_path, n_jobs, emails_path, n_emails = rp.write_day(con, d)
        console.print(f"{d}: {n_jobs} jobs -> {jobs_path}", markup=False, highlight=False)
        console.print(f"{d}: {n_emails} emails sent -> {emails_path}", markup=False, highlight=False)
    path, n = rp.write_tracker(con)
    console.print(f"Tracker: {n} applications sent in all -> {path}", markup=False, highlight=False)


def discover_step():
    """Look for job sources in the next few countries (rotation over every country)."""
    n = (load_config().get("sources") or {}).get("countries_per_cycle", 0)
    if n:
        from .cli_sources import source_discover
        source_discover(region=None, countries=n, no_search=False)


@app.command()
def jobs_excel(days: Optional[int] = typer.Option(None, help="How many days back; default: max_age_days in config.")):
    """Write the Excel list of all recent jobs: position, company, type, dates, place, HR email,
    job link, company website, job description and stage."""
    cfg, con = load_config(), db.connect()
    path, n, with_email = rp.write_jobs_book(con, days or cfg["max_age_days"])
    console.print(f"Job list: {n} jobs from the last {days or cfg['max_age_days']} days, {with_email} with an HR email -> {path}",
                  markup=False, highlight=False)


@app.command()
def clean():
    """Delete old generated resume PDFs and the files of applications already sent. Records are kept."""
    pdfs, folders = cl.run(db.connect())
    console.print(f"Cleaned up: {pdfs} PDF files in {folders} folders deleted. "
                  f"The record of every sent application is kept in Applications-Sent.xlsx.")


ITERATION = {"id": None}


# Steps that send email are never run a second time by themselves: a failure there is looked at, not repeated.
NO_RETRY = ("send", "leads", "followup")


def _step(name, fn, label=None):
    """Run one pipeline step unless it is switched off; record it. A step that fails is tried again
    (loop.step_retries times, never for the steps that send email); if it still fails, that is
    reported and the iteration carries on. Returns (ok, value)."""
    label = label or name
    if not enabled(load_config(), name):
        console.print(f"[yellow]{label}: switched off, skipped.[/yellow]")
        return True, None
    con = db.connect()
    row = con.execute("INSERT INTO process_log(iteration, step, started_at) VALUES(?,?,?)",
                      (ITERATION["id"], name, db.now())).lastrowid
    con.commit()
    loop_cfg = load_config().get("loop") or {}
    tries = 1 + (0 if name in NO_RETRY else int(loop_cfg.get("step_retries", 1)))
    for attempt in range(1, tries + 1):
        ok, value, note = True, None, ""
        try:
            value = fn()
        except typer.Exit:
            pass   # a step with nothing to do ends itself
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {str(e)[:200]}"
            if attempt < tries:
                wait = int(loop_cfg.get("retry_wait_seconds", 20))
                console.print(f"[yellow]{label} failed ({note[:120]}); trying again in {wait} seconds.[/yellow]")
                time.sleep(wait)
                continue
            console.print(f"[red]{label} failed, continuing: {note[:140]}[/red]")
        if attempt > 1:
            note = (note + " " if note else "") + f"(attempt {attempt} of {tries})"
        break
    con.execute("UPDATE process_log SET finished_at=?, ok=?, note=? WHERE id=?", (db.now(), int(ok), note, row))
    con.commit(); con.close()
    return ok, value


def find_jobs(cfg):
    """Existing sources first. Only when they bring nothing new does the bot go looking for new
    sources (next countries in rotation) and read those."""
    _, kept = _step("scrape", lambda: scrape(only=None), "Reading existing sources")
    if kept:
        console.print(f"Existing sources brought {kept} new jobs; no need to look for new sources this time.")
        return kept
    if not enabled(cfg, "discover"):
        console.print("No new jobs in the existing sources, and looking for new sources is switched off.")
        return 0
    console.print("No new jobs in the existing sources. Looking for new sources...")
    from . import cli_sources
    s_cfg = cfg.get("sources") or {}
    before = len(cli_sources.added_all)
    for _ in range(s_cfg.get("discovery_rounds_when_empty", 2)):
        _step("discover", lambda: cli_sources.source_discover(region=None, countries=s_cfg.get("countries_per_cycle", 2),
                                                              no_search=False), "Looking for new sources")
        fresh = cli_sources.added_all[before:]
        if fresh:
            _, kept = _step("scrape", lambda: scrape(only=",".join(fresh)), "Reading new sources")
            if kept:
                console.print(f"New sources brought {kept} new jobs.")
                return kept
            before = len(cli_sources.added_all)
    console.print("No new jobs anywhere this time.")
    return 0


@app.command()
def cycle(loop: bool = typer.Option(False, help="Keep going: a new iteration starts after each one finishes."),
          minutes: Optional[int] = typer.Option(None, help="Pause between iterations with --loop; default from config.")):
    """The whole process: clean up, find jobs (existing sources, else new ones), find HR emails,
    list in Excel, write resume and email per job, send, record. With --loop it repeats: when an
    iteration has finished, it waits the configured pause and starts the next one."""
    if loop:
        try:
            lock = sq.LoopLock().__enter__()
        except RuntimeError as e:
            console.print(f"[yellow]{e}[/yellow]")
            raise typer.Exit(1)
        import atexit
        atexit.register(lambda: lock.__exit__())
        # Ask Windows not to go to sleep while the loop is running (a sleeping PC stops the bot).
        # It ends by itself when the loop stops. Closing the lid or running out of battery still sleeps.
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000040)
        except Exception:
            pass
    n = 0
    while True:
        cfg = load_config()
        n += 1
        ITERATION["id"] = db.now()
        pause = minutes if minutes is not None else (cfg.get("loop") or {}).get("pause_minutes", 10)
        console.rule(f"Iteration {n} started {ITERATION['id']}")
        if not enabled(cfg, "bot"):
            console.print("[yellow]The bot is switched off (master switch). Nothing done.[/yellow]")
            results = []
        else:
            results = [_step("clean", clean, "Clean up")]
            # What is already prepared goes out first, so the 08:30 start sends at once
            # instead of waiting for the search to finish.
            results.append(_step("send", lambda: send(dry_run=False, now=False, redirect=None, per_run=None),
                                 "Sending what is ready"))
            find_jobs(cfg)
            # 3 find emails  4 list in Excel  5 resume + email  6 send  7 records
            for name, label, fn in (
                    ("enrich", "Full job pages", lambda: enrich(limit=120)),
                    ("contacts", "HR email search", lambda: contacts(limit=(cfg.get("loop") or {}).get("email_lookups", 150), recheck=False)),
                    ("match", "Matching", match),
                    ("report", "Excel job list", lambda: jobs_excel(days=None)),
                    ("tailor", "Resume and email writing", lambda: tailor(job_id=None, limit=(cfg.get("ai") or {}).get("per_run", 12), redo=False, no_ai=False)),
                    ("inbox", "Inbox check", inbox),
                    ("send", "Sending", lambda: send(dry_run=False, now=False, redirect=None, per_run=None)),
                    ("followup", "Follow-ups", lambda: followups(dry_run=False, now=False)),
                    ("report", "Reports", lambda: (status(), report(day=None), jobs_excel(days=None)))):
                results.append(_step(name, fn, label))
        failed = sum(not ok for ok, _ in results)
        console.rule(f"Iteration {n} finished {db.now()}" + (f" with {failed} step(s) failed" if failed else " successfully"))
        if not loop:
            break
        console.print(f"Next iteration starts in {pause} minutes. Ctrl+C to stop.")
        time.sleep(pause * 60)


@app.command()
def followups(dry_run: bool = typer.Option(False, help="Show which follow-ups are due; send nothing."),
              now: bool = typer.Option(False, help="Ignore the office-hours rule for this run.")):
    """Send the single follow-up for applications that got no answer after a week."""
    from . import followup as fu
    cfg, con = load_config(), db.connect()
    allowed = [e.lower() for e in cfg["sending"].get("send_from") or []]
    accounts = [s for s in mailer.senders() if s["can_send"]]   # the account that applied must follow up
    if not dry_run and not now and not sq.in_hours(cfg):
        console.print("Outside sending hours; no follow-ups sent.")
        return
    room = {}
    for s in accounts:
        cap, _ = sq.cap_today(con, s["email"], cfg)
        room[s["email"]] = 0 if sq.bounce_stop(con, s["email"], cfg) else max(0, cap - sq.sent_today(con, s["email"]))
    results = fu.run(con, cfg, accounts, room, dry_run)
    waiting = len(fu.due(con, cfg))
    if not results:
        console.print(f"No follow-ups to send now. Due and still waiting: {waiting}.")
        return
    t = Table("Job", "To", "Result")
    for job_id, to, state in results:
        t.add_row(f"#{job_id}", to, state)
    console.print(t)


process_app = typer.Typer(no_args_is_help=True, help="Switch pipeline steps on and off.")
app.add_typer(process_app, name="process")


@process_app.command("list")
def process_list():
    """Every step, whether it is on, and how its last run went."""
    cfg, con = load_config(), db.connect()
    t = Table("Step", "On", "What it does", "Last run", "Result")
    for name, what in PROCESSES.items():
        last = con.execute("SELECT * FROM process_log WHERE step=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        t.add_row(name, "yes" if enabled(cfg, name) else "[red]no[/red]", what,
                  (last["started_at"] or "").replace("T", " ") if last else "",
                  "" if not last or last["ok"] is None else "ok" if last["ok"] else f"[red]{last['note'][:40]}[/red]")
    console.print(t)


@process_app.command("on")
def process_on(name: str):
    """Switch a step on."""
    _switch(name, True)


@process_app.command("off")
def process_off(name: str):
    """Switch a step off. The bot skips it from the next iteration."""
    _switch(name, False)


def _switch(name, on):
    if name not in PROCESSES:
        console.print(f"No step named {name}. Steps: {', '.join(PROCESSES)}")
        raise typer.Exit(1)
    set_setting(f"process.{name}", on)
    console.print(f"{name}: {'on' if on else 'off'}")


@app.command()
def api(port: int = typer.Option(8765, help="Port on this PC.")):
    """Start the REST API and the admin page (this PC only), without the scheduler."""
    from . import api as api_mod
    console.print(f"Admin page: http://127.0.0.1:{port}/admin     API: http://127.0.0.1:{port}/     (Ctrl+C to stop)")
    api_mod.serve(port)


@app.command()
def serve(port: int = typer.Option(0, help="Port to listen on (default: customer 8765, admin 8766, all 8765)."),
          host: str = typer.Option("127.0.0.1", help="127.0.0.1 = this computer only. On a server, keep this and put an "
                                                     "HTTPS reverse proxy in front."),
          site: str = typer.Option("all", help="customer = the customer portal only; admin = the admin panel only "
                                               "(it also runs the scheduler); all = both in one process."),
          scheduler: Optional[bool] = typer.Option(None, "--scheduler/--no-scheduler",
                                                   help="Start the accounts' runs when due. Default: on for admin and all.")):
    """The whole service: REST API, admin page, and the scheduler that starts each account's runs
    when they fall due. This is the one program to keep running on a server."""
    from . import api as api_mod, control
    if not control.user_count():
        console.print(f"No user exists yet. Open http://127.0.0.1:{port}/admin on this computer to create the first one, "
                      "or run: jobbot user add <email>")
    port = port or (8766 if site == "admin" else 8765)
    with_scheduler = scheduler if scheduler is not None else site != "customer"   # exactly one process should start runs
    page = {"customer": "/", "admin": "/", "all": "/admin and /app"}.get(site, "/")
    console.print(f"{site} site on http://{host}:{port}{page if page == '/' else ' (' + page + ')'}"
                  f"{'  with the scheduler' if with_scheduler else ''}   (Ctrl+C to stop)")
    api_mod.serve(port, host, with_scheduler=with_scheduler, site=site)


account_app = typer.Typer(no_args_is_help=True, help="The customers of the service. Each has its own data folder.")
user_app = typer.Typer(no_args_is_help=True, help="The people who can sign in.")
app.add_typer(account_app, name="account")
app.add_typer(user_app, name="user")


@account_app.command("list")
def account_list():
    """Every account, where its data is, and whether the bot may run for it."""
    from . import control
    t = Table("ID", "Name", "Data folder", "Time zone", "Status", "Users", "Ready to run")
    for a in control.accounts():
        t.add_row(str(a["id"]), a["name"], a["home"], a["timezone"], a["status"], str(a["users"]),
                  control.ready_to_run(a) or "yes")
    console.print(t)


@account_app.command("add")
def account_add(name: str, timezone: str = typer.Option("UTC", help="The customer's time zone, e.g. Asia/Taipei.")):
    """Create a customer account with an empty resume and neutral settings. Add its first user with `jobbot user add`."""
    from . import control
    try:
        a = control.account_create(name, timezone)
    except Exception as e:
        raise typer.Exit(console.print(f"Not created: {e}") or 1)
    console.print(f"Account {a['id']} '{a['name']}' created. Data folder: {a['home']}. "
                  f"Next: jobbot user add <email> --account {a['id']}")


def _new_password(generate):
    import secrets
    if generate:
        return secrets.token_urlsafe(12)
    return typer.prompt("Password (at least 10 characters)", hide_input=True, confirmation_prompt=True)


@user_app.command("add")
def user_add(email: str, account: int = typer.Option(1, help="Account id; 1 is the owner's own."),
             role: str = typer.Option("owner", help="owner, member or viewer."),
             generate: bool = typer.Option(False, help="Make up a password and print it once, instead of asking for one.")):
    """Create a sign-in. The very first user also becomes the service operator, who can create accounts."""
    from . import control
    first = control.user_count() == 0
    password = _new_password(generate)
    try:
        u = control.user_create(account, email, password, role, operator=first)
    except ValueError as e:
        raise typer.Exit(console.print(f"Not created: {e}") or 1)
    console.print(f"User {u['email']} created in account {u['account_id']} as {u['role']}" + (" and service operator." if first else "."))
    if generate:
        console.print(f"Password (shown only now): {password}", markup=False, highlight=False)


@user_app.command("list")
def user_list(account: int = typer.Option(1, help="Account id.")):
    """The users of one account."""
    from . import control
    t = Table("ID", "Email", "Role", "Operator", "Active", "Last sign-in")
    for u in control.users(account):
        t.add_row(str(u["id"]), u["email"], u["role"], "yes" if u["operator"] else "", "yes" if u["active"] else "no",
                  (u["last_login_at"] or "never")[:16].replace("T", " "))
    console.print(t)


@user_app.command("password")
def user_password(email: str, generate: bool = typer.Option(False, help="Make up a password and print it once.")):
    """Set a new password for a user and end all their sign-ins."""
    from . import control
    password = _new_password(generate)
    if control.password_problem(password):
        raise typer.Exit(console.print(control.password_problem(password)) or 1)
    row = control._one("SELECT id FROM users WHERE email=?", email.strip().lower())
    if not row:
        raise typer.Exit(console.print(f"No user {email}") or 1)
    control._write("UPDATE users SET password_hash=? WHERE id=?", control.hash_password(password), row["id"])
    control._write("UPDATE tokens SET revoked_at=? WHERE user_id=? AND kind='session' AND revoked_at IS NULL", control.now(), row["id"])
    console.print(f"Password changed for {email}; they must sign in again.")
    if generate:
        console.print(f"Password (shown only now): {password}", markup=False, highlight=False)


loop_app = typer.Typer(no_args_is_help=True, help="The repeating loop started by the Windows task 'JobBot Loop'.")
app.add_typer(loop_app, name="loop")


def _loop_pid():
    pid = sq.holder(sq.LOOP_LOCK)
    return pid if pid and pid > 0 else None


@loop_app.command("status")
def loop_status():
    """Is the loop running, and what did it do last."""
    pid = _loop_pid()
    console.print(f"The loop is running (process {pid})." if pid else "The loop is not running.")
    con = db.connect()
    for r in con.execute("SELECT step, started_at, finished_at, ok, note FROM process_log ORDER BY id DESC LIMIT 6").fetchall()[::-1]:
        state = "running" if r["finished_at"] is None else "ok" if r["ok"] else f"FAILED {r['note'][:60]}"
        console.print(f"  {r['started_at'].replace('T', ' ')}  {r['step']:<10} {state}", markup=False, highlight=False)
    console.print("Full output: logs/loop.log", markup=False)


@loop_app.command("stop")
def loop_stop():
    """Stop the loop now. It starts again at your next Windows sign-in, or with: jobbot loop start"""
    import subprocess
    pid = _loop_pid()
    if not pid:
        console.print("The loop is not running.")
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    con = db.connect()
    stuck = con.execute("SELECT id, to_email FROM jobs WHERE send_status='sending'").fetchall()
    console.print("Loop stopped.")
    for r in stuck:
        console.print(f"[yellow]Job #{r['id']} to {r['to_email']} was interrupted while sending; check the Sent folder, "
                      f"then run: jobbot settle {r['id']} sent   (or: failed)[/yellow]")


@loop_app.command("start")
def loop_start():
    """Start the loop in the background through the Windows task."""
    import subprocess
    if _loop_pid():
        console.print("The loop is already running.")
        return
    r = subprocess.run(["schtasks", "/Run", "/TN", "JobBot Loop"], capture_output=True, text=True)
    console.print("Loop started." if r.returncode == 0 else f"Could not start the task: {r.stderr.strip()[:120]}")
