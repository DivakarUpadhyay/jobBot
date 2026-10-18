import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import browser, contacts as ct, db, filter as flt, mailer, render, tailor as tl
from .config import load_config, load_sources, set_source_enabled
from .sources import ADAPTERS, tw104
from .sources.base import Job

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Job bot: find jobs, tailor resume, email HR.")
source_app = typer.Typer(no_args_is_help=True, help="Manage job sources.")
app.add_typer(source_app, name="source")
console = Console()


def _fetch(src, cfg):
    params = dict(src.get("params") or {})
    if src["adapter"] == "generic":
        params.setdefault("name", str(src["name"])); params.setdefault("region", src.get("region", "Remote"))
    return ADAPTERS[src["adapter"]](cfg, params)


@app.command()
def scrape(only: Optional[str] = typer.Option(None, help="Run just this source.")):
    """Fetch new jobs from every enabled source, filter and score them."""
    cfg, con = load_config(), db.connect()
    table = Table("Source", "Fetched", "New", "Kept", "Skipped", "Note")
    wanted = [x.strip() for x in only.split(",")] if only else None   # one name, or several with commas
    master = render.load_master()
    total_kept = 0
    # One shared pool of postings (db.py): only the account with sources.fetch on (the service owner's) reads the
    # job sites; every account then judges the postings it has not judged yet against its own profile.
    fetch = bool((cfg.get("sources") or {}).get("fetch", True)) or bool(wanted)
    todo = [src for src in load_sources()
            if (str(src["name"]) in wanted if wanted else src.get("enabled"))] if fetch else []
    if (cfg.get("sources") or {}).get("respect_robots") and not wanted:
        from . import compliance
        refused = compliance.blocked_sources(con)   # robots.txt disallows them, or their terms forbid reading them
        todo = [src for src in todo if str(src["name"]) not in refused]

    def read(src):
        try:
            return src, _fetch(src, cfg), None
        except Exception as e:
            return src, [], str(e)[:200]

    # Sources are read several at a time (each is a different website); saving stays one at a time.
    from concurrent.futures import ThreadPoolExecutor
    with console.status(f"Reading {len(todo)} sources..."), ThreadPoolExecutor(6) as pool:
        fetched = list(pool.map(read, todo))
    for src, jobs, error in fetched:
        name = str(src["name"])
        if error:
            db.log_run(con, name, 0, 0, error); con.commit()
            table.add_row(name, "-", "-", "-", "-", f"[red]failed: {error[:60]}[/red]")
            continue
        added = kept = 0
        for job in jobs:
            if job.job_type == "lead":
                from . import leads
                result = leads.add_from_job(con, cfg, master, job)
                added += result != "known"
                kept += result == "new"
                continue
            verdict = flt.judge(job, cfg)
            if db.insert_job(con, job, verdict):
                added += 1
                kept += not verdict["skip"]
        db.log_run(con, name, len(jobs), added); con.commit()
        table.add_row(name, str(len(jobs)), str(added), str(kept), str(added - kept), "")
        total_kept += kept
    pooled, pooled_kept = _judge_pool(con, cfg)
    if pooled:
        table.add_row("shared pool", str(pooled), str(pooled), str(pooled_kept), str(pooled - pooled_kept), "postings read by the service")
    total_kept += pooled_kept
    console.print(table)
    return total_kept   # new jobs that passed the filter


def _judge_pool(con, cfg):
    """Judge, for this account, the shared postings it has not seen yet (found in the last max_age_days).
    Returns (judged, kept). Owner-only rows (prospects, leads, service offers) are never shared."""
    from .portal import GROUPS
    since = (dt.date.today() - dt.timedelta(days=int(cfg.get("max_age_days") or 30))).isoformat()
    want = ((con.execute("SELECT value FROM portal_prefs WHERE key='targets'").fetchone() or [None])[0])
    types = (json.loads(want) or {}).get("job_types") if want else None   # None: the owner, every kind of work
    adapters = {s["name"]: s["adapter"] for s in load_sources()}
    judged = kept = 0
    for p in db.new_postings(con, since):
        if p["kind"] in ("prospect", "lead") or p["source"] in ("services", "prospects", "prospects-india", "leads"):
            continue
        job = Job(source=p["source"], ext_id=p["ext_id"], title=p["title"] or "", company=p["company"] or "", url=p["url"] or "",
                  description=p["description"] or "", location=p["location"] or "", region=p["region"] or "",
                  remote=bool(p["remote"]), job_type=p["kind"] if p["kind"] in ("full", "part") else "", posted_at=p["posted_at"] or "")
        v = flt.judge(job, cfg, has_email=p["emails"] not in (None, "", "[]"))
        group = next((g for g, names in GROUPS.items() if adapters.get(p["source"]) in names), None)
        if types is not None and group and not types.get(group):
            v["skip"] = f"kind of work you did not choose: {group}"
        db.insert_job(con, job, v)
        judged += 1; kept += not v["skip"]
    con.commit()
    return judged, kept


@app.command()
def enrich(limit: int = typer.Option(120, help="Most jobs to read in one go.")):
    """Read the full job page for promising 104 and Yourator jobs: full description, HR name, any
    email, publish date. The job is then judged again, since a short listing can hide a
    Mandarin-only or outdated post."""
    cfg, con = load_config(), db.connect()
    rows = con.execute("SELECT * FROM jobs WHERE source='104' AND status='new' AND enriched=0 AND score>=? "
                       "ORDER BY score DESC LIMIT ?", (min(cfg["min_score"].values()), limit)).fetchall()
    done = dropped = with_email = 0
    with console.status(f"Reading {len(rows)} job pages on 104..."):
        for r in rows:
            try:
                d = tw104.detail(r["url"])
            except Exception:
                continue  # left un-enriched; retried on the next run
            job = Job(source=r["source"], ext_id=r["ext_id"], title=r["title"], company=r["company"], url=r["url"],
                      description=d["description"], location=r["location"], region=r["region"],
                      remote=bool(r["remote"]), job_type=r["job_type"], posted_at=r["posted_at"] or "")
            emails = flt.find_emails(d["description"] + "\n" + d["contact_text"])
            v = flt.judge(job, cfg, has_email=bool(emails))
            con.execute("""UPDATE jobs SET description=?, hr_name=?, emails=?, score=?, score_notes=?, category=?,
                           track=?, status=?, skip_reason=?, enriched=1 WHERE id=?""",
                        (d["description"], d["hr_name"], json.dumps(emails), v["score"], v["notes"], v["category"],
                         v["track"], "skipped" if v["skip"] else "new", v["skip"], r["id"]))
            done += 1; dropped += bool(v["skip"]); with_email += bool(emails)
            con.commit()   # save each page at once: other commands must not wait on a long read
            time.sleep(0.5)
    con.commit()
    console.print(f"104 job pages read: {done}, dropped after full read: {dropped}, with an email: {with_email}")

    # Yourator's list gives only a title and tags. The job page carries the full description,
    # which shows whether the post is really in English.
    from .sources import generic
    from .sources.base import get as http_get
    rows = con.execute("SELECT * FROM jobs WHERE source='yourator' AND status IN ('new','matched') AND enriched=0 "
                       "AND send_status IS NULL ORDER BY (status='matched') DESC, score DESC LIMIT ?", (limit,)).fetchall()
    done = dropped = 0
    with console.status(f"Reading {len(rows)} job pages on Yourator..."):
        for r in rows:
            try:
                page = http_get(r["url"])
                found = generic.jsonld_jobs(page.text, page.url, "yourator", "Taiwan")
            except Exception:
                continue   # retried on the next run
            if not found:
                con.execute("UPDATE jobs SET enriched=1 WHERE id=?", (r["id"],))
                con.commit()
                continue
            full = found[0]
            job = Job(source=r["source"], ext_id=r["ext_id"], title=r["title"], company=r["company"], url=r["url"],
                      description=full.description, location=r["location"], region=r["region"], remote=bool(r["remote"]),
                      job_type=full.job_type or r["job_type"], posted_at=full.posted_at or r["posted_at"] or "")
            emails = flt.find_emails(full.description)
            v = flt.judge(job, cfg, has_email=bool(emails) or r["emails"] != "[]")
            keep_status = r["status"] if not v["skip"] else "skipped"
            con.execute("""UPDATE jobs SET description=?, posted_at=?, emails=CASE WHEN emails='[]' THEN ? ELSE emails END,
                           score=?, score_notes=?, category=?, track=?, status=?, skip_reason=?, enriched=1 WHERE id=?""",
                        (full.description, job.posted_at, json.dumps(emails), v["score"], v["notes"], v["category"], v["track"],
                         keep_status, v["skip"], r["id"]))
            done += 1; dropped += bool(v["skip"])
            con.commit()
            time.sleep(0.4)
    con.commit()
    console.print(f"Yourator job pages read: {done}, dropped after full read: {dropped}")


@app.command()
def match():
    """Pick today's best jobs: up to the daily target for full-time and for part-time."""
    cfg, con, day = load_config(), db.connect(), db.today()
    taken = {r["dedupe_key"] for r in con.execute("SELECT dedupe_key FROM jobs WHERE status='matched'")}
    for kind in ("full", "part"):
        have = con.execute("SELECT COUNT(*) FROM jobs WHERE status='matched' AND matched_on=? AND job_type=?",
                           (day, kind)).fetchone()[0]
        room, picked = cfg["daily_target"][kind] - have, 0
        rows = con.execute("SELECT id, dedupe_key FROM jobs WHERE status='new' AND job_type=? AND score>=? "
                           "ORDER BY (emails!='[]') DESC, score DESC, posted_at DESC",   # jobs that can be emailed first
                           (kind, cfg["min_score"][kind])).fetchall()
        for r in rows:
            if picked >= room:
                break
            if r["dedupe_key"] in taken:  # same role already matched from another source
                con.execute("UPDATE jobs SET status='skipped', skip_reason='duplicate' WHERE id=?", (r["id"],))
                continue
            taken.add(r["dedupe_key"])
            con.execute("UPDATE jobs SET status='matched', matched_on=? WHERE id=?", (day, r["id"]))
            picked += 1
        if cfg.get("apply_to_every_job_with_email"):
            extra = 0
            for r in con.execute("SELECT id, dedupe_key FROM jobs WHERE status='new' AND job_type=? AND emails!='[]' "
                                 "ORDER BY score DESC", (kind,)).fetchall():
                if r["dedupe_key"] in taken:
                    con.execute("UPDATE jobs SET status='skipped', skip_reason='duplicate' WHERE id=?", (r["id"],))
                    continue
                taken.add(r["dedupe_key"])
                con.execute("UPDATE jobs SET status='matched', matched_on=? WHERE id=?", (day, r["id"]))
                extra += 1
            if extra:
                console.print(f"{kind}-time: {extra} more matched because they show an HR email (any field)")
        short = cfg["daily_target"][kind] - have - picked
        console.print(f"{kind}-time: matched {picked} now, {have + picked}/{cfg['daily_target'][kind]} today"
                      + (f"  [yellow](short by {short}: not enough good matches)[/yellow]" if short > 0 else ""))
    con.commit()


@app.command()
def run():
    """One cycle: scrape, read full job pages, match, show status."""
    scrape(only=None)
    enrich(limit=120)
    match()
    status()


@app.command()
def status():
    """Today's numbers."""
    con, day = db.connect(), db.today()
    n = lambda where, *a: con.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", a).fetchone()[0]
    t = Table(f"Today {day}", "Full-time", "Part-time")
    t.add_row("Found today", *(str(n("found_at LIKE ? AND job_type=?", day + "%", k)) for k in ("full", "part")))
    t.add_row("Skipped by filter", *(str(n("found_at LIKE ? AND job_type=? AND status='skipped'", day + "%", k)) for k in ("full", "part")))
    t.add_row("Waiting (not yet matched)", *(str(n("status='new' AND job_type=?", k)) for k in ("full", "part")))
    t.add_row("Matched today", *(str(n("status='matched' AND matched_on=? AND job_type=?", day, k)) for k in ("full", "part")))
    t.add_row("  with an email in the post", *(str(n("status='matched' AND matched_on=? AND job_type=? AND emails!='[]'", day, k)) for k in ("full", "part")))
    console.print(t)
    by = Table("Track", "Field", "Matched today")
    for r in con.execute("SELECT track, category, COUNT(*) c FROM jobs WHERE status='matched' AND matched_on=? "
                         "GROUP BY 1,2 ORDER BY 1, c DESC", (day,)):
        by.add_row(r["track"], r["category"], str(r["c"]))
    console.print(by)
    reasons = con.execute("SELECT skip_reason, COUNT(*) c FROM jobs WHERE status='skipped' AND found_at LIKE ? "
                          "GROUP BY 1 ORDER BY c DESC", (day + "%",)).fetchall()
    if reasons:
        console.print("Skipped today: " + ", ".join(f"{r['skip_reason']} ({r['c']})" for r in reasons))


@app.command("list")
def list_jobs(kind: Optional[str] = typer.Option(None, "--type", help="full or part"),
              state: str = typer.Option("matched", "--status", help="matched, new or skipped"),
              track: Optional[str] = typer.Option(None, help="full, part or freelance"),
              bids: bool = typer.Option(False, "--bids", help="Only bidding-platform projects (bid on the site)."),
              limit: int = 30):
    """Show jobs, best score first."""
    con = db.connect()
    q, args = "SELECT * FROM jobs WHERE status=?", [state]
    if kind:
        q += " AND job_type=?"; args.append(kind)
    if track:
        q += " AND track=?"; args.append(track)
    if bids:
        q += " AND source IN ('freelancer')"
    t = Table("ID", "Score", "Track", "Field", "Title", "Company", "Source", "Where", "Email", "Why")
    for r in con.execute(q + " ORDER BY score DESC, posted_at DESC LIMIT ?", (*args, limit)):
        t.add_row(str(r["id"]), str(r["score"]), r["track"], r["category"], r["title"][:42], r["company"][:22],
                  r["source"], "TW" if r["region"] == "taiwan" else "remote",
                  "yes" if r["emails"] != "[]" else "", (r["skip_reason"] or r["score_notes"] or "")[:40])
    console.print(t)


@app.command()
def show(job_id: int):
    """Full details of one job."""
    r = db.connect().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise typer.Exit(console.print(f"No job with id {job_id}") or 1)
    for k in ("title", "company", "source", "url", "location", "region", "track", "category", "salary",
              "posted_at", "score", "score_notes", "status", "skip_reason", "hr_name", "emails", "resume_path",
              "tailored_by", "tailor_note"):
        console.print(f"{k:12} {r[k]}", markup=False, highlight=False)
    if r["email_subject"]:
        console.rule("Email draft")
        console.print(f"Subject: {r['email_subject']}\n\n{r['email_body']}", markup=False, highlight=False)
    console.rule("Job description")
    console.print((r["description"] or "")[:1500], markup=False, highlight=False)


def _slug(text):
    text = text.replace(".NET", "DotNet").replace("C#", "CSharp")
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", text)).strip("_")[:60]


@app.command()
def resume(job_id: Optional[int] = typer.Argument(None, help="Job to build the resume for."),
           region: str = typer.Option("taiwan", help="Without a job id: taiwan or abroad.")):
    """Build the resume PDF. Gold Card lines appear only for jobs in Taiwan."""
    cfg, con = load_config(), db.connect()
    name = _slug(render.load_master()["name"].replace("R.", ""))
    if job_id is None:
        out = render.OUTPUT / "base" / f"{name}_Resume_{region}.pdf"
    else:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise typer.Exit(console.print(f"No job with id {job_id}") or 1)
        region = r["region"]
        out = render.OUTPUT / db.today() / f"{name}_{_slug(r['title'])}_{r['id']}.pdf"
    render.render(region, out, channel=cfg["browser"]["channel"])
    if job_id is not None:
        con.execute("UPDATE jobs SET resume_path=? WHERE id=?", (str(out), job_id)); con.commit()
    console.print(f"Saved {out}  (region: {region}, Gold Card {'included' if region == 'taiwan' else 'left out'})")


@app.command()
def contacts(limit: int = typer.Option(150, help="Most jobs to look up in one go."),
             recheck: bool = typer.Option(False, help="Look again at jobs where nothing was found before.")):
    """Look for an HR email for jobs that have none: the company site (found from the post,
    a web search, or its name), its careers and contact pages, and other pages that quote a
    hiring address on the company domain. Addresses are only read from pages, never guessed.
    Covers matched jobs first, then good jobs still waiting, so the next match can prefer
    jobs that can actually be emailed. Bidding projects are skipped."""
    cfg, con = load_config(), db.connect()
    if recheck:
        con.execute("UPDATE jobs SET contact_checked=0 WHERE emails='[]'")
    rows = con.execute("""SELECT id, company, description, region FROM jobs
                          WHERE status IN ('matched','new') AND emails='[]' AND contact_checked=0
                          AND source!='freelancer' AND score>=?
                          ORDER BY (status='matched') DESC, score DESC LIMIT ?""",
                       ((cfg.get("loop") or {}).get("email_lookup_min_score", min(cfg["min_score"].values())), limit)).fetchall()
    found = 0
    with console.status(f"Looking for HR emails for {len(rows)} jobs..."):
        for job_id, emails, where, site in ct.find_many(rows):
            con.execute("UPDATE jobs SET contact_checked=1, emails=?, email_source=?, company_url=COALESCE(NULLIF(?, ''), company_url) "
                        "WHERE id=?", (json.dumps(emails), where, site, job_id))
            con.commit()   # save each result at once, so a stopped run loses nothing
            found += bool(emails)
    total = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('matched','new') AND emails!='[]'").fetchone()[0]
    console.print(f"Looked up {len(rows)} jobs, found an email for {found}. Jobs with an email now: {total}")


@app.command()
def tailor(job_id: Optional[int] = typer.Argument(None, help="One job; default is every matched job not done yet."),
           limit: int = typer.Option(100, help="Most jobs to do in one go."),
           redo: bool = typer.Option(False, help="Do again even if already done."),
           no_ai: bool = typer.Option(False, "--no-ai", help="Template only: no AI call, instant.")):
    """Write the job-specific resume PDF and the email for matched jobs."""
    cfg, con = load_config(), db.connect()
    ai_cfg = cfg.get("ai") or {}
    chain, notes = ([], []) if no_ai else tl.engines(cfg)
    for line in notes:
        console.print(f"[yellow]{line}[/yellow]")
    key = bool(chain)
    console.print("AI engines, in order: " + ", ".join(f"{p}:{m}" for p, m, _ in chain) if chain else
                  "Mode: basic (no working AI key in .env). Skills are reordered per job; wording is not rewritten.")
    if job_id is not None:
        rows = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchall()
    else:
        # Only jobs with an HR email get a resume: nothing else can be sent, and the free AI quota is small.
        rows = con.execute("SELECT * FROM jobs WHERE status='matched' AND emails!='[]' AND job_type!='lead'"
                           + ("" if redo else " AND (resume_path IS NULL OR (tailored_by='basic' AND send_status IS NULL))" if no_ai else
                              " AND (resume_path IS NULL OR (tailored_by='basic' AND send_status IS NULL))")
                           + " ORDER BY score DESC, (resume_path IS NULL) DESC LIMIT ?",
                           (limit,)).fetchall()  # best match first; template-only drafts are tried again with AI
    master = render.load_master()
    name = _slug(master["name"].replace("R.", ""))
    done = {"ai": 0, "basic": 0}
    fell_back = 0
    with render.PdfPrinter(cfg["browser"]["channel"]) as pdf:
        check = bool((cfg.get("match") or {}).get("ai_check")) and job_id is None
        dropped = waiting = 0
        for r in rows:
            if check:
                # accuracy before volume: the AI confirms the job fits this candidate before anything is written
                if not chain:
                    waiting += 1
                    continue
                fit, reason = tl.fit_check(r, master, chain, cfg["profile"]["titles"])
                if fit is None:
                    waiting += 1
                    continue          # decided on the next run, never sent unchecked
                if not fit:
                    con.execute("UPDATE jobs SET status='skipped', skip_reason=? WHERE id=?", ("AI: not a fit - " + reason, r["id"]))
                    con.commit()
                    dropped += 1
                    continue
                con.execute("UPDATE jobs SET score_notes=COALESCE(score_notes,'') || ? WHERE id=?", ("; AI fit: " + reason, r["id"]))
            t = tl.tailor(r, cfg, master, chain)
            html = render.to_html(render.build(r["region"], tailored=t, master=master))
            render.check_region_rule(html, r["region"])
            out = render.OUTPUT / db.today() / str(r["id"]) / f"{name}_{_slug(t['role_title'])}.pdf"
            pdf.save(html, out)
            con.execute("UPDATE jobs SET resume_path=?, email_subject=?, email_body=?, tailored_by=?, tailor_note=?, "
                        "tailored_json=? WHERE id=?",
                        (str(out), t["email_subject"], t["email_body"], t["mode"], t["note"],
                         json.dumps({k: t[k] for k in ("headline", "summary", "skills", "experience") if t.get(k)},
                                    ensure_ascii=False), r["id"]))
            con.commit()
            done[t["mode"]] += 1
            fell_back += bool(key and t["mode"] == "basic")
            if key:
                time.sleep(ai_cfg.get("seconds_between_calls", 5))  # stay inside the free tier's per-minute limit
    if check:
        console.print(f"AI fit check: {dropped} jobs dropped as not a fit" + (f", {waiting} wait for the AI" if waiting else ""))
    console.print(f"Resumes written: {sum(done.values())} (AI: {done['ai']}, basic: {done['basic']})"
                  + (f"  [yellow]{fell_back} fell back to basic; see `jobbot show <id>`[/yellow]" if fell_back else ""))
    console.print(f"Folder: {render.OUTPUT / db.today()}")


@app.command()
def verify(count: int = typer.Option(6, help="How many matched jobs to open."),
           headless: bool = typer.Option(False, help="Do not show the Chrome window.")):
    """Open matched jobs in Chrome and check the saved title and company against the live page."""
    cfg, con = load_config(), db.connect()
    # spread across sources: best job from each, then next best, up to count
    rows = con.execute("""SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY source ORDER BY score DESC) rn
                          FROM jobs WHERE status='matched') ORDER BY rn, score DESC LIMIT ?""", (count,)).fetchall()
    if not rows:
        raise typer.Exit(console.print("No matched jobs yet. Run `jobbot run` first.") or 1)
    # 104 shows a "verify you are human" page to automated Chrome, so those are checked
    # against 104's own job detail feed instead of the page.
    results = browser.check_jobs([r for r in rows if r["source"] != "104"], cfg["browser"]["channel"], headless)
    for r in (r for r in rows if r["source"] == "104"):
        res = {"id": r["id"], "source": "104", "title": r["title"], "http": None, "title_found": False,
               "company_found": False, "note": "checked via 104 detail feed"}
        try:
            d = tw104.detail(r["url"])
            res.update(http=200, title_found=d["title"] == r["title"], company_found=d["company"] == r["company"])
        except Exception as e:
            res["note"] = type(e).__name__
        results.append(res)
    t = Table("ID", "Source", "Title", "HTTP", "Title on page", "Company on page", "Note")
    mark = lambda ok: "[green]yes[/green]" if ok else "[red]no[/red]"
    for r in results:
        t.add_row(str(r["id"]), r["source"], r["title"][:40], str(r["http"]), mark(r["title_found"]),
                  mark(r["company_found"]), r["note"])
    console.print(t)
    console.print(f"Screenshots: {browser.SHOTS}")


@app.command()
def export():
    """Write all matched jobs to an Excel file."""
    from openpyxl import Workbook
    con, wb = db.connect(), Workbook()
    ws = wb.active
    cols = ["id", "matched_on", "job_type", "track", "category", "score", "title", "company", "source", "region",
            "location", "salary", "posted_at", "url", "emails", "score_notes", "resume_path"]
    ws.append(cols)
    for r in con.execute("SELECT * FROM jobs WHERE status='matched' ORDER BY matched_on DESC, score DESC"):
        ws.append([", ".join(json.loads(r[c])) if c == "emails" else r[c] for c in cols])
    out = render.OUTPUT / f"jobs_{db.today()}.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    console.print(f"Saved {out}")


@source_app.command("list")
def source_list():
    """All sources and their last run."""
    con = db.connect()
    t = Table("Name", "Region", "On", "Last run", "Fetched", "New", "Error")
    for s in sorted(load_sources(), key=lambda x: (x.get("region", "") not in ("Taiwan", "Remote"), x.get("region", ""))):
        last = con.execute("SELECT * FROM runs WHERE source=? ORDER BY id DESC LIMIT 1", (str(s["name"]),)).fetchone()
        t.add_row(str(s["name"]), s.get("region", ""), "yes" if s.get("enabled") else "no",
                  last["started_at"] if last else "-", str(last["fetched"]) if last else "-",
                  str(last["added"]) if last else "-", (last["error"] or "")[:40] if last else "")
    console.print(t)


@source_app.command("on")
def source_on(name: str):
    """Switch a source on."""
    console.print(f"{name}: on" if set_source_enabled(name, True) else f"No source named {name}")


@source_app.command("off")
def source_off(name: str):
    """Switch a source off."""
    console.print(f"{name}: off" if set_source_enabled(name, False) else f"No source named {name}")


@source_app.command("test")
def source_test(name: str):
    """Trial fetch: show a few jobs without saving anything."""
    cfg = load_config()
    src = next((s for s in load_sources() if str(s["name"]) == name), None)
    if not src:
        raise typer.Exit(console.print(f"No source named {name}") or 1)
    jobs = _fetch(src, cfg)
    console.print(f"{name}: {len(jobs)} jobs fetched")
    t = Table("Title", "Company", "Type", "Where", "Posted", "Verdict")
    for job in jobs[:8]:
        v = flt.judge(job, cfg)
        t.add_row(job.title[:45], job.company[:22], job.job_type, job.location[:22], job.posted_at,
                  v["skip"] or f"score {v['score']}")
    console.print(t)


sender_app = typer.Typer(no_args_is_help=True, help="Manage the email accounts the bot sends from.")
app.add_typer(sender_app, name="sender")


@sender_app.command("list")
def sender_list():
    """Sender accounts found in .env and whether each can log in."""
    accounts = mailer.senders()
    if not accounts:
        raise typer.Exit(console.print("No senders in .env yet. Add SENDER_1=... and SENDER_1_APP_PASSWORD=...") or 1)
    t = Table("#", "Email", "Mail server", "Login")
    for s in accounts:
        if not s["can_send"]:
            state = "[red]no app password or sign-in in .env[/red]"
        else:
            try:
                mailer.check_login(s); state = "[green]ok[/green]"
            except Exception as e:
                state = f"[red]failed: {type(e).__name__}[/red]"
        t.add_row(str(s["n"]), s["email"], s["smtp"], state)
    console.print(t)


@sender_app.command("test")
def sender_test(job_id: Optional[int] = typer.Argument(None, help="Use this job's email and resume; default: best tailored job."),
                swap: bool = typer.Option(False, help="Send from account 2 to account 1 instead.")):
    """Send one real application email from one of your accounts to the other, so you can see
    exactly what HR would receive. Nothing goes to anyone else."""
    accounts = [s for s in mailer.senders() if s["can_send"]]
    if len(accounts) < 2:
        raise typer.Exit(console.print("Need two senders with app passwords in .env for this test.") or 1)
    src, dst = (accounts[1], accounts[0]) if swap else (accounts[0], accounts[1])
    con = db.connect()
    r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone() if job_id else con.execute(
        "SELECT * FROM jobs WHERE resume_path IS NOT NULL ORDER BY (tailored_by='ai') DESC, score DESC LIMIT 1").fetchone()
    if not r or not r["resume_path"]:
        raise typer.Exit(console.print("No tailored job found. Run `jobbot tailor` first.") or 1)
    cfg, master = load_config(), render.load_master()
    # the resume carries the address it is sent from, so replies and the PDF agree
    pdf = render.OUTPUT / "test" / Path(r["resume_path"]).name
    html = render.to_html(render.build(r["region"], email=src["email"], master=master,
                                       tailored=json.loads(r["tailored_json"]) if r["tailored_json"] else None))
    render.check_region_rule(html, r["region"])
    render.to_pdf(html, pdf, cfg["browser"]["channel"])
    msg = mailer.build(src["email"], master["name"].replace(" R.", ""), dst["email"], r["email_subject"],
                       r["email_body"], pdf)
    mailer.send(src, msg)
    console.print(f"Sent from {src['email']} to {dst['email']}")
    console.print(f"Subject: {r['email_subject']}\nAttachment: {pdf.name}\nJob: #{r['id']} {r['title'][:60]} ({r['track']}, {r['category']})",
                  markup=False, highlight=False)


@app.command("db-merge")
def db_merge():
    """One-time move to the single database: control.db and every account's database into jobbot.db
    (backups first). Stop the loop and the sites before running it."""
    from . import merge_db
    result = merge_db.run()
    if isinstance(result, str):
        console.print(result)
    else:
        for k, v in result.items():
            console.print(f"{k}: {v}")
