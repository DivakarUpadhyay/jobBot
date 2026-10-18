"""The REST API over the bot's data, for the admin page, a phone, or your own tools.

Start it with `jobbot serve` (API, admin page and the scheduler) or `jobbot api` (no scheduler).
It is a FastAPI application served by uvicorn; /docs lists every route and lets you try them.
It answers in JSON. Every request except the four marked "open" needs a sign-in token or an API
key, sent as "Authorization: Bearer <token>", and sees only the data of that user's own account.

  GET  /health                      open: is the service up
  GET  /admin                       open: the admin page (it asks you to sign in)
  GET  /setup, POST /setup          open: create the very first user, on the service's own computer only
  POST /login                       open: {"email", "password"} -> a token valid for 14 days
  POST /logout                      end this sign-in
  GET  /me                          who you are, your account, and whether the bot may run for it yet
  GET  /keys, POST /keys {"name"}, DELETE /keys/{id}      your API keys; a key is shown once, when made
  GET  /users, POST /users, PUT /users/{id}               owner: the people in your account and their roles
  GET  /schedules, POST /schedules, PUT /schedules/{id}, DELETE /schedules/{id}
                                    when the bot runs: {"kind": "daily", "at": "08:30", "days": [0,1,2,3,4]}
                                    or {"kind": "interval", "every_minutes": 120}
  GET  /cycles, POST /cycles        runs of the whole pipeline; POST starts one now
  POST /cycles/{id}/stop            stop a run that is waiting or going
  GET  /cycles/{id}/log?tail=200    what a run printed
  GET  /audit                       owner: every change made through the API, by whom
  GET  /accounts, POST /accounts    operator: the customers of the service

  GET  /status                      numbers for today and in total, loop state, per-account sending
  GET  /stats                       sent per day, by kind, field and source (for charts)
  GET  /jobs?type=&stage=&has_email=1&country=&source=&q=&days=14&limit=50&offset=0
  GET  /jobs/{id}                   one job with description and email draft
  GET  /sent                        every application that was sent
  GET  /sources?enabled=1&region=&reader=&q=    job sources (table `sources`) and their last run
  GET  /sources/{name}              one source with its reader settings and its last 10 runs
  POST /sources                     add a source: {"url": "...", "region": "Remote"} and the bot works out how to read it,
                                    or name the reader yourself: {"name", "adapter", "region", "params", "home", "enabled"}
  PUT  /sources/{name}              change region, enabled, home, params or note
  DELETE /sources/{name}            delete an added source (built-in ones can only be switched off)
  GET  /candidates?state=readable&region=&q=    every site on the list to try, its test result, and the source it became
  POST /candidates                  put a site on the list to try: {"url": "...", "region": "Remote"}
  GET  /countries                   discovery progress per country
  GET  /runs?limit=50               log of source reads
  POST /jobs/{id}/approve           approve a prepared email for sending
  POST /jobs/{id}/reject            never send for this job
  POST /jobs/{id}/prep              write the round-by-round interview guide for this job (about 30 seconds)
  GET  /processes                   every pipeline step: on/off and its last run
  POST /processes/{name}/enable     switch a step on        (name "bot" is the master switch)
  POST /processes/{name}/disable    switch a step off
  GET  /processes/log?limit=100     history of step runs
  POST /sources/{name}/enable       switch a job source on
  POST /sources/{name}/disable      switch a job source off
  GET  /catalogue?region=           known job sites tried first when looking for sources in a region
  POST /catalogue                   add one: {"url": "...", "region": "Japan"}
  DELETE /catalogue?url=...         remove one
  GET  /settings                    every setting (table `settings`): value, default, explanation, changed or not
  GET  /config                      the same settings as one nested object, and which ones differ from the default
  PUT  /config                      change settings: {"sending.per_run": 5, "sending.review": "manual"}
  DELETE /config/{key}              go back to the default
  GET  /profile                     the documents the bot writes from: resume, email-templates, resume-template
  GET  /profile/{name}              one document
  PUT  /profile/{name}              save a new version: {"content": ...}; refused if a resume can no longer be built
"""
import datetime as dt
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from contextvars import ContextVar

from . import access, control as ct, db, discover as dc, payments, portal, profilestore, report as rp, scheduler, sourcestore
from . import APP, use_home
from .config import PROCESSES, enabled, load_config, overrides, reset_setting, set_setting, setting_problem, settings_list
from .sources import ADAPTERS

LIST_FIELDS = ("id", "title", "company", "source", "url", "company_url", "location", "region", "remote", "job_type",
               "track", "category", "score", "status", "posted_at", "found_at", "salary", "hr_name", "email_source",
               "send_status", "sender", "to_email", "cc_email", "sent_at", "outcome", "auto_ack", "tailored_by")


def _row(r, full=False):
    d = {k: r[k] for k in LIST_FIELDS}
    d["emails"] = json.loads(r["emails"] or "[]")
    d["stage"] = rp.stage(r)
    d["type"] = rp.TYPE_NAMES.get(r["track"] or r["job_type"], r["job_type"])
    if full:
        d.update(interview_prep=r["interview_prep"], interview_prep_at=r["interview_prep_at"],
                 description=r["description"], email_subject=r["email_subject"], email_body=r["email_body"],
                 resume_path=r["resume_path"], score_notes=r["score_notes"], skip_reason=r["skip_reason"])
    return d


def status(con):
    day = db.today()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    return {
        "date": day,
        "jobs_total": one("SELECT COUNT(*) FROM jobs"),
        "jobs_found_today": one("SELECT COUNT(*) FROM jobs WHERE found_at LIKE ?", day + "%"),
        "jobs_with_email": one("SELECT COUNT(*) FROM jobs WHERE emails!='[]' AND status!='skipped'"),
        "waiting_for_review": one("SELECT COUNT(*) FROM jobs WHERE status='matched' AND emails!='[]' AND send_status IS NULL "
                                  "AND (resume_path IS NOT NULL OR job_type='lead')"),
        "approved_not_sent": one("SELECT COUNT(*) FROM jobs WHERE send_status='approved'"),
        "sent_today": one("SELECT COUNT(*) FROM jobs WHERE send_status='sent' AND sent_at LIKE ?", day + "%"),
        "sent_total": one("SELECT COUNT(*) FROM jobs WHERE send_status='sent'"),
        "replies": one("SELECT COUNT(*) FROM jobs WHERE outcome='replied'"),
        "auto_acknowledgements": one("SELECT COUNT(*) FROM jobs WHERE auto_ack=1"),
        "bounced": one("SELECT COUNT(*) FROM jobs WHERE outcome='bounced'"),
        "sources_active": one("SELECT COUNT(*) FROM sources WHERE enabled=1"),
        "sources_total": one("SELECT COUNT(*) FROM sources"),
        "leads_waiting": one("SELECT COUNT(*) FROM jobs WHERE job_type='lead' AND status='matched' AND send_status IS NULL"),
        "jobs_waiting": one("SELECT COUNT(*) FROM jobs WHERE job_type!='lead' AND status='matched' AND emails!='[]' "
                            "AND send_status IS NULL AND resume_path IS NOT NULL"),
        **_live(con),
    }


WHO = ContextVar("who", default=None)   # the signed-in user this request is being answered for


def _live(con):
    """Is the bot running, what is it doing, and how much has each sending account sent today."""
    from . import mailer, sendqueue as sq
    cfg = load_config()
    try:
        running = sq.LOOP_LOCK.exists() and sq.pid_alive(int(sq.LOOP_LOCK.read_text().strip() or 0))
    except (OSError, ValueError):
        running = False
    who = WHO.get()
    running = running or bool(who and ct._one("SELECT 1 FROM cycles WHERE account_id=? AND status='running'", who["account_id"]))
    step = con.execute("SELECT step, started_at FROM process_log WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    last = con.execute("SELECT MAX(finished_at) FROM process_log").fetchone()[0]
    allowed = [e.lower() for e in cfg["sending"].get("send_from") or []]
    accounts = []
    for s in mailer.senders():
        if allowed and s["email"].lower() not in allowed:
            continue
        cap, week = sq.cap_today(con, s["email"], cfg)
        accounts.append({"email": s["email"], "sent_today": sq.sent_today(con, s["email"]), "cap": cap,
                         "cc": mailer.cc_for(s["email"], cfg), "stopped": sq.bounce_stop(con, s["email"], cfg),
                         "sent_total": con.execute("SELECT COUNT(*) FROM jobs WHERE sender=? AND send_status='sent'", (s["email"],)).fetchone()[0],
                         "bounced": con.execute("SELECT COUNT(*) FROM jobs WHERE sender=? AND outcome='bounced'", (s["email"],)).fetchone()[0]})
    return {"loop_running": bool(running), "current_step": step["step"] if step and running else None,
            "current_step_since": step["started_at"] if step and running else None, "last_activity": last,
            "sending_window": cfg["sending"]["hours"], "in_sending_hours": sq.in_hours(cfg), "accounts": accounts}


def stats(con):
    """Numbers for the charts on the admin page."""
    rows = lambda sql: [dict(r) for r in con.execute(sql)]
    return {
        "sent_by_day": rows("SELECT substr(sent_at,1,10) day, COUNT(*) n FROM jobs WHERE send_status='sent' GROUP BY 1 ORDER BY 1 DESC LIMIT 14")[::-1],
        "found_by_day": rows("SELECT substr(found_at,1,10) day, COUNT(*) n FROM jobs GROUP BY 1 ORDER BY 1 DESC LIMIT 14")[::-1],
        "sent_by_kind": rows("SELECT COALESCE(track, job_type) k, COUNT(*) n FROM jobs WHERE send_status='sent' GROUP BY 1 ORDER BY 2 DESC"),
        "sent_by_field": rows("SELECT category k, COUNT(*) n FROM jobs WHERE send_status='sent' GROUP BY 1 ORDER BY 2 DESC"),
        "sent_by_source": rows("SELECT source k, COUNT(*) n FROM jobs WHERE send_status='sent' GROUP BY 1 ORDER BY 2 DESC LIMIT 8"),
        "outcomes": {"sent": con.execute("SELECT COUNT(*) FROM jobs WHERE send_status='sent'").fetchone()[0],
                     "acknowledged": con.execute("SELECT COUNT(*) FROM jobs WHERE auto_ack=1").fetchone()[0],
                     "replied": con.execute("SELECT COUNT(*) FROM jobs WHERE outcome='replied'").fetchone()[0],
                     "bounced": con.execute("SELECT COUNT(*) FROM jobs WHERE outcome='bounced'").fetchone()[0]},
    }


def jobs(con, q):
    get = lambda k, default=None: (q.get(k) or [default])[0]
    where, args = ["1=1"], []
    if get("type"):
        where.append("COALESCE(track, job_type)=?"); args.append(get("type"))
    if get("kind") == "leads":
        where.append("job_type='lead'")
    elif get("kind") == "jobs":
        where.append("job_type!='lead'")
    if get("review") in ("1", "true", "yes"):   # exactly the emails that wait for the owner's decision
        where.append("status='matched' AND emails!='[]' AND send_status IS NULL AND (resume_path IS NOT NULL OR job_type='lead')")
    if get("has_email") in ("1", "true", "yes"):
        where.append("emails!='[]'")
    if get("source"):
        where.append("source=?"); args.append(get("source"))
    if get("country"):
        where.append("(location LIKE ? OR (region='taiwan' AND ?='taiwan'))"); args += [f"%{get('country')}%", get("country").lower()]
    if get("q"):
        where.append("(title LIKE ? OR company LIKE ?)"); args += [f"%{get('q')}%"] * 2
    if get("days"):
        since = (dt.date.today() - dt.timedelta(days=int(get("days")))).isoformat()
        where.append("COALESCE(NULLIF(posted_at,''), substr(found_at,1,10))>=?"); args.append(since)
    if get("status"):
        where.append("status=?"); args.append(get("status"))
    else:
        where.append("status!='skipped'")
    sql = " FROM jobs WHERE " + " AND ".join(where)
    rows = [_row(r) for r in con.execute(
        "SELECT *" + sql + " ORDER BY COALESCE(NULLIF(posted_at,''), substr(found_at,1,10)) DESC, score DESC LIMIT ? OFFSET ?",
        (*args, min(int(get("limit", 50)), 500), int(get("offset", 0))))]
    if get("stage"):
        rows = [r for r in rows if get("stage").lower() in r["stage"].lower()]
    return {"total": con.execute("SELECT COUNT(*)" + sql, args).fetchone()[0], "count": len(rows), "jobs": rows}


def _source(con, s, full=False):
    runs = con.execute("SELECT * FROM runs WHERE source=? ORDER BY id DESC LIMIT ?", (s["name"], 10 if full else 1)).fetchall()
    d = {"name": s["name"], "region": s["region"], "enabled": s["enabled"], "reader": s["adapter"], "home": s["home"],
         "note": s["note"], "added_at": s["added_at"], "changed_at": s["changed_at"],
         "last_run": dict(runs[0]) if runs else None}
    if full:
        d.update(params=s["params"], runs=[dict(r) for r in runs])
    return d


def sources(con, q=None):
    get = lambda k: ((q or {}).get(k) or [None])[0]
    out = sourcestore.load(con)
    if get("enabled") is not None:
        out = [s for s in out if s["enabled"] == (get("enabled").lower() in ("1", "true", "yes", "on"))]
    if get("region"):
        out = [s for s in out if s["region"].lower() == get("region").lower()]
    if get("reader"):
        out = [s for s in out if s["adapter"] == get("reader")]
    if get("q"):
        out = [s for s in out if get("q").lower() in f"{s['name']} {s['region']} {s['home'] or ''}".lower()]
    return [_source(con, s) for s in out]


SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
PAGE_KINDS, BOARD_KINDS = ("feed", "jsonld", "browser"), ("greenhouse", "lever", "ashby", "workable")


def _web_address(value):
    return isinstance(value, str) and urlparse(value).scheme in ("http", "https") and bool(urlparse(value).netloc)


def source_problem(adapter, params):
    """Why this reader and these settings cannot work, or None when they can."""
    if adapter not in ADAPTERS:
        return f"no such reader: {adapter}. Readers: {', '.join(ADAPTERS)}"
    if not isinstance(params, dict):
        return "params must be an object"
    if adapter == "generic":
        kind = params.get("kind")
        if kind in PAGE_KINDS and not all(_web_address(u) for u in params.get("urls") or [params.get("url")]):
            return f"params.url must be a web address for kind '{kind}'"
        if kind in BOARD_KINDS and not params.get("slug"):
            return f"params.slug is needed for kind '{kind}'"
        if kind not in PAGE_KINDS + BOARD_KINDS:
            return f"params.kind must be one of: {', '.join(PAGE_KINDS + BOARD_KINDS)}"
    return None


def add_source(con, body):
    """POST /sources. Returns (http code, answer)."""
    region = body.get("region") or "Remote"
    if "adapter" in body:
        name, params = str(body.get("name") or ""), body.get("params") or {}
        if not SOURCE_NAME.match(name):
            return 400, {"error": "name: lower-case letters, digits, dot, dash or underscore, at most 60"}
        if body.get("home") and not _web_address(body["home"]):
            return 400, {"error": "home must be a web address"}
        problem = source_problem(body["adapter"], params)
        if problem:
            return 400, {"error": problem}
        saved = sourcestore.add(name, body["adapter"], region, params, enabled=body.get("enabled", True),
                                home=body.get("home"), note=body.get("note") or "added through the API", con=con)
        return 201, _source(con, sourcestore.get(saved, con), full=True)
    url = body.get("url")
    if not _web_address(url):
        return 400, {"error": "give a url (http or https), or name the reader with adapter and params"}
    from .sources import generic
    name = str(body.get("name") or dc.slug(urlparse(url).netloc.removeprefix("www.").rsplit(".", 1)[0]))
    if not SOURCE_NAME.match(name):
        return 400, {"error": "name: lower-case letters, digits, dot, dash or underscore, at most 60"}
    params, found = generic.detect(url, name, region, use_browser=bool(body.get("browser")))
    if not params:
        return 422, {"error": "cannot read this site", "why": dc.why_unreadable(url)}
    saved = sourcestore.add(name, "generic", region, params, enabled=body.get("enabled", True), home=url,
                            note=body.get("note") or f"added through the API: {url}", con=con)
    return 201, _source(con, sourcestore.get(saved, con), full=True) | {
        "jobs_in_test": len(found), "sample": [{"title": j.title, "company": j.company, "location": j.location} for j in found[:5]]}


def change_source(con, name, body):
    """PUT /sources/{name}. Returns (http code, answer)."""
    old = sourcestore.get(name, con)
    if not old:
        return 404, {"error": "no such source"}
    unknown = [k for k in body if k not in ("region", "enabled", "home", "params", "note")]
    if unknown:
        return 400, {"error": "these cannot be changed: " + ", ".join(unknown)}
    if body.get("home") and not _web_address(body["home"]):
        return 400, {"error": "home must be a web address"}
    if "params" in body:
        problem = source_problem(old["adapter"], body["params"])
        if problem:
            return 400, {"error": problem}
    sourcestore.update(name, body, con)
    return 200, _source(con, sourcestore.get(name, con), full=True)


# Settings that may be changed through the API. Secrets (.env) are never exposed or editable.
EDITABLE = re.compile(r"^(sending|loop|ai|daily_target|min_score|leads|sources|search|profile|avoid)\.[\w.]+$|"
                      r"^(max_age_days|regions|open_to_relocation|remote_restricted_penalty)$")


def processes(con):
    cfg, out = load_config(), []
    for name, what in PROCESSES.items():
        last = con.execute("SELECT * FROM process_log WHERE step=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        out.append({"name": name, "what": what, "enabled": enabled(cfg, name),
                    "last_started": last["started_at"] if last else None, "last_finished": last["finished_at"] if last else None,
                    "last_ok": last["ok"] if last else None, "last_note": last["note"] if last else None})
    return out


# The customer portal and the admin panel are separate projects (apps/customer-portal, apps/admin-panel), each
# served by its own process on its own port: `jobbot serve --site customer` and `jobbot serve --site admin`.
# Each answers only its own routes, so a customer's browser can never reach the operator's API and the other way
# round. `--site all` (the default) still serves both from one process, for the container in deploy/.
PAGES = {
    "customer": {"/": "customer-portal/index.html", "/app": "customer-portal/index.html"},
    "admin": {"/": "admin-panel/index.html", "/admin": "admin-panel/index.html", "/console": "admin-panel/console.html"},
}
PAGES["all"] = {"/app": "customer-portal/index.html", "/admin": "admin-panel/index.html", "/console": "admin-panel/console.html"}
CUSTOMER_ROUTES = ("/health", "/login", "/logout", "/me", "/plans", "/register", "/enquiries", "/portal/", "/schedules", "/docs", "/openapi.json")
SITE = {"name": "all"}


def site_refuses(path):
    """Why this site does not answer the path, or None. Customer site: public and /portal routes only.
    Admin site: everything except customer sign-up and the portal."""
    site = SITE["name"]
    if site == "customer" and not (path in CUSTOMER_ROUTES or path.startswith(("/portal/", "/schedules/"))):
        return "not found"
    if site == "admin" and (path in ("/register", "/app") or path.startswith("/portal/")):
        return "not found on the admin panel; customers use the customer portal"
    return None


def public(method, path, client, forwarded, body):
    """Requests answered without signing in. Returns (http code, answer), or None for everything else."""
    if method == "GET" and path == "/health":
        return 200, {"ok": True, "time": ct.now()}
    if path == "/setup" and method in ("GET", "POST"):
        needed = ct.user_count() == 0
        if method == "GET":
            return 200, {"needed": needed}
        if not needed:
            return 409, {"error": "the first user already exists; sign in instead"}
        if client not in ("127.0.0.1", "::1") or forwarded:   # never from the network, also not through a proxy
            return 403, {"error": "the first user can only be created on the computer the service runs on"}
        b = body()
        try:
            return 201, {"user": ct.user_create(1, b.get("email"), b.get("password"), "owner", b.get("name") or "", operator=True)}
        except ValueError as e:
            return 400, {"error": str(e)}
    if method == "GET" and path == "/plans":
        return 200, {"markets": portal.markets(), "plans": portal.plans()}
    if method == "POST" and path in ("/register", "/enquiries"):
        try:
            if path == "/register":
                return 201, portal.register(body(), forwarded or client)
            return 201, portal.enquiry_create(body(), forwarded or client)
        except PermissionError:
            return 429, {"error": "too many requests from here; try again in 15 minutes"}
        except ValueError as e:
            return 400, {"error": str(e)}
    if method == "POST" and path == "/login":
        b = body()
        try:
            token, expires, user = ct.login(b.get("email"), b.get("password"), forwarded or client)
        except PermissionError as e:
            return (429, {"error": "too many wrong tries; wait 15 minutes"}) if str(e) == "wait" else \
                   (401, {"error": "wrong email or password"})
        return 200, {"token": token, "expires_at": expires, "user": user}
    return None


def service(who, method, path, q, body):
    """Accounts, users, keys, schedules and runs. Returns (http code, answer), or None for a path that is not one of these."""
    one = lambda k, default=None: (q.get(k) or [default])[0]
    mine, me = who["account_id"], who["user_id"]
    try:
        if path == "/me" and method == "GET":
            acc = ct.account(mine)
            return 200, {"user": ct.user(me), "account": {k: acc[k] for k in ("id", "slug", "name", "timezone", "status")},
                         "not_ready": ct.ready_to_run(acc)}
        if path == "/logout" and method == "POST":
            return 200, {"signed_out": ct.token_revoke(me, who["token_id"])}
        if path == "/keys":
            if method == "GET":
                return 200, ct.tokens(me)
            if method == "POST":
                key, _ = ct.token_create(me, "key", str(body().get("name") or "API key")[:60])
                return 201, {"key": key, "note": "Keep it somewhere safe: it is shown only this once."}
        m = re.fullmatch(r"/keys/(\d+)", path)
        if m and method == "DELETE":
            gone = ct.token_revoke(me, int(m.group(1)))
            return (200 if gone else 404), {"revoked": gone}
        if path == "/users":
            if method == "GET":
                return 200, ct.users(mine)
            if method == "POST":
                b = body()
                return 201, ct.user_create(mine, b.get("email"), b.get("password"), b.get("role") or "member", b.get("name") or "")
        m = re.fullmatch(r"/users/(\d+)", path)
        if m and method == "PUT":
            if int(m.group(1)) == me:
                return 400, {"error": "you cannot change your own role or switch yourself off"}
            b = body()
            ok = ct.user_update(mine, int(m.group(1)), b.get("role"), b.get("active"))
            return (200, ct.user(int(m.group(1)))) if ok else (404, {"error": "no such user in your account"})
        if path == "/accounts":
            if method == "GET":
                return 200, ct.accounts()
            if method == "POST":
                b = body()
                email = (b.get("email") or "").strip().lower()
                problem = ct.password_problem(b.get("password")) or (None if ct.EMAIL.match(email) else "that is not an email address") \
                    or ("a user with that email already exists" if ct._one("SELECT 1 FROM users WHERE email=?", email) else None)
                if problem:
                    return 400, {"error": problem}
                acc = ct.account_create(b.get("name") or "", b.get("timezone") or "UTC")
                return 201, {"account": acc, "owner": ct.user_create(acc["id"], email, b["password"], "owner", b.get("owner_name") or "")}
        customer = not who["operator"] and mine != 1
        if path.startswith("/schedules") and method in ("POST", "PUT", "DELETE") and customer:
            problem = access.paid_problem(ct.account(mine))
            if problem:
                return 402, {"error": problem, "payment_required": True}
            b = body() if method != "DELETE" else {}  # customers may not run the bot more often than once an hour, nor keep more than 3 schedules
            if b.get("kind") == "interval" and isinstance(b.get("every_minutes"), int) and b["every_minutes"] < 60:
                return 400, {"error": "the bot can run at most once an hour"}
            if method == "POST" and len(scheduler.schedules(mine)) >= 3:
                return 400, {"error": "at most 3 schedules per account"}
        if path == "/schedules":
            if method == "GET":
                return 200, scheduler.schedules(mine)
            if method == "POST":
                return 201, scheduler.schedule_save(who, body())
        m = re.fullmatch(r"/schedules/(\d+)", path)
        if m and method == "PUT":
            saved = scheduler.schedule_save(who, body(), int(m.group(1)))
            return (200, saved) if saved else (404, {"error": "no such schedule"})
        if m and method == "DELETE":
            gone = scheduler.schedule_delete(mine, int(m.group(1)))
            return (200 if gone else 404), {"removed": gone}
        if path == "/cycles":
            if method == "GET":
                return 200, scheduler.cycles(mine, min(int(one("limit", 30)), 200))
            if method == "POST":
                run, new = scheduler.request(mine, "manual", me)
                return (201 if new else 200), {"queued": new, "cycle": run} | ({} if new else {"note": "a run is already waiting or going"})
        m = re.fullmatch(r"/cycles/(\d+)/stop", path)
        if m and method == "POST":
            stopped = scheduler.stop(mine, int(m.group(1)))
            return (200 if stopped else 409), {"stopped": stopped}
        m = re.fullmatch(r"/cycles/(\d+)/log", path)
        if m and method == "GET":
            text = scheduler.log_tail(mine, int(m.group(1)), min(int(one("tail", 200)), 2000))
            return (200, {"log": text}) if text is not None else (404, {"error": "no log for this run"})
        if path == "/audit" and method == "GET":
            return 200, ct.audit_log(mine, min(int(one("limit", 100)), 1000))
        if path.startswith("/portal/"):
            return portal_route(who, method, path, q, body)
        if path.startswith("/operator/"):
            return operator_route(who, method, path, q, body)
    except ValueError as e:
        return 400, {"error": str(e)}
    return None


def portal_route(who, method, path, q, body):
    """The customer portal: their own account, through the routes under /portal."""
    acc = ct.account(who["account_id"])
    # Paid access (access.py): first set-up is free; after it, changes need a paid plan. Checked before any route runs.
    if method != "GET":
        free = ("/portal/billing", "/portal/checkout", "/portal/tickets", "/portal/onboarding/complete", "/portal/bot/off")
        paid_only = ("/portal/mailbox", "/portal/bot/on", "/portal/bot/run", "/portal/ai")
        if not (path in free or re.fullmatch(r"/portal/payments/\d+/confirm", path)):
            problem = access.paid_problem(acc) if path.startswith(paid_only) else access.edit_problem(acc)
            if problem:
                return 402, {"error": problem, "payment_required": True}
    if path == "/portal/access" and method == "GET":
        return 200, access.status(acc)
    if path == "/portal/onboarding/complete" and method == "POST":
        return 200, access.complete(acc)
    if path == "/portal/details":
        if method == "GET":
            return 200, access.details_get()
        if method == "PUT":
            return 200, access.details_put(body())
    if path == "/portal/cover-letter":
        if method == "GET":
            return 200, access.cover_get()
        if method == "PUT":
            return 200, access.cover_put(body())
    if path == "/portal/cover-letter/upload" and method == "POST":
        return 200, access.cover_upload(body())
    if path == "/portal/suggest-targets" and method == "POST":
        return 200, access.suggest_targets()
    if path == "/portal/ai" and method == "POST":
        return 200, access.ai_assist(body())
    if path == "/portal/overview" and method == "GET":
        return 200, portal.overview(who)
    if path == "/portal/targets":
        if method == "GET":
            return 200, portal.targets_get()
        if method == "PUT":
            return 200, portal.targets_put(body(), acc)
    if path == "/portal/resume":
        if method == "GET":
            return 200, {"content": profilestore.get("resume"), "changed_at": profilestore.changed_at("resume")}
        if method == "PUT":
            return 200, portal.resume_put(body())
    if path == "/portal/resume/upload" and method == "POST":
        return 200, portal.resume_upload(body())
    if path == "/portal/mailbox":
        if method == "GET":
            return 200, portal.mailbox_get()
        if method == "PUT":
            return 200, portal.mailbox_put(body(), acc)
    if path == "/portal/mailbox/test" and method == "POST":
        return 200, portal.mailbox_test()
    if path == "/portal/bot" and method == "GET":
        return 200, portal.bot_state(who["account_id"])
    m = re.fullmatch(r"/portal/bot/(on|off|run)", path)
    if m and method == "POST":
        if m.group(1) == "run":
            run, new = scheduler.request(who["account_id"], "manual", who["user_id"])
            return (201 if new else 200), {"queued": new, "cycle": run}
        return 200, portal.bot_switch(who, m.group(1) == "on")
    if path == "/portal/queue" and method == "GET":
        return 200, portal.queue(q)
    if path == "/portal/applications" and method == "GET":
        return 200, portal.applications(q)
    m = re.fullmatch(r"/portal/queue/(\d+)/(approve|reject|pick)", path)
    if m and method == "POST":
        job_id = int(m.group(1))
        ok = portal.pick(job_id) if m.group(2) == "pick" else portal.decide(job_id, m.group(2) == "approve")
        return (200 if ok else 409), {"changed": ok}
    m = re.fullmatch(r"/portal/jobs/(\d+)", path)
    if m and method == "GET":
        job = portal.job_detail(int(m.group(1)))
        return (200, job) if job else (404, {"error": "no such job"})
    m = re.fullmatch(r"/portal/jobs/(\d+)/tailored", path)
    if m and method == "PUT":
        saved = portal.tailored_put(int(m.group(1)), body())
        return (200, saved) if saved else (404, {"error": "no such job"})
    m = re.fullmatch(r"/portal/jobs/(\d+)/retailor", path)
    if m and method == "POST":
        ok = portal.retailor(int(m.group(1)))
        return (200 if ok else 409), {"changed": ok, "note": "the next run tailors it again" if ok else "already sent"}
    if path == "/portal/billing" and method == "GET":
        return 200, portal.billing(who)
    if path == "/portal/checkout" and method == "POST":
        if who["role"] != "owner":
            return 403, {"error": "only the account owner may buy a plan"}
        b = body()
        return 201, payments.checkout(acc, b.get("plan"), b.get("gateway"))
    m = re.fullmatch(r"/portal/payments/(\d+)/confirm", path)
    if m and method == "POST":
        paid = payments.confirm(who["account_id"], int(m.group(1)), body())
        return (200, paid) if paid else (404, {"error": "no such payment"})
    if path == "/portal/tickets":
        if method == "GET":
            return 200, portal.tickets_mine(who)
        if method == "POST":
            return 201, portal.ticket_create(who, body())
    return None


def operator_route(who, method, path, q, body):
    """The admin panel: the whole service. Only the operator gets here (control.allowed)."""
    if path == "/operator/overview" and method == "GET":
        return 200, portal.operator_overview()
    m = re.fullmatch(r"/operator/accounts/(\d+)", path)
    if m and method == "PUT":
        acc = portal.account_update(int(m.group(1)), body())
        return (200, acc) if acc else (404, {"error": "no such account"})
    if path == "/operator/plans" and method == "GET":
        return 200, portal.plans(include_hidden=True)
    if path == "/operator/plans" and method == "POST":
        return 201, portal.plan_create(body())
    if path == "/operator/markets" and method == "GET":
        return 200, portal.markets()
    if path == "/operator/markets" and method == "POST":
        return 201, portal.market_save(body())
    m = re.fullmatch(r"/operator/plans/([\w-]+)", path)
    if m and method == "PUT":
        plan = portal.plan_update(m.group(1), body())
        return (200, plan) if plan else (404, {"error": "no such plan"})
    if path == "/operator/enquiries" and method == "GET":
        return 200, portal.enquiries(q)
    m = re.fullmatch(r"/operator/enquiries/(\d+)", path)
    if m and method == "PUT":
        e = portal.enquiry_update(int(m.group(1)), body(), who)
        return (200, e) if e else (404, {"error": "no such enquiry"})
    if path == "/operator/payments" and method == "GET":
        return 200, {"payments": payments.history(), "collected_30_days": payments.collected()}
    if path == "/operator/gateways" and method == "GET":
        return 200, payments.gateways(include_inactive=True)
    m = re.fullmatch(r"/operator/gateways/([\w-]+)", path)
    if m and method == "PUT":
        g = payments.gateway_update(m.group(1), body())
        return (200, g) if g else (404, {"error": "no such gateway"})
    if path == "/operator/tickets" and method == "GET":
        return 200, portal.tickets_all(q)
    m = re.fullmatch(r"/operator/tickets/(\d+)", path)
    if m and method == "PUT":
        t = portal.ticket_update(int(m.group(1)), body())
        return (200, t) if t else (404, {"error": "no such ticket"})
    return None


class Exchange:
    """One request and its answer, independent of the web server that carried it."""

    def __init__(self, method, target, headers, client, raw=b""):
        self.method, self.path, self.headers, self.client, self._raw = method, target, headers, client, raw
        self._read = self._code = self._answer = None

    def _body(self):
        if self._read is None:
            self._read = json.loads(self._raw) if self._raw.strip() else {}
            if not isinstance(self._read, dict):
                raise ValueError("the request body must be a JSON object")
        return self._read

    def run(self):
        """Every request goes through here: open routes, then who is asking, then whether they may, then
        the answer. Returns (http code, answer); the answer is bytes for the admin page, else JSON data."""
        method, url = self.method, urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        try:
            pages = PAGES[SITE["name"]]
            if method == "GET" and path in pages:   # the pages are open; each asks for sign-in itself
                self._code, self._answer = 200, (APP / "apps" / pages[path]).read_bytes()
                return self._code, self._answer
            refused = site_refuses(path)
            if refused:
                self._send(404, {"error": refused})
                return self._code, self._answer
            answer = public(method, path, self.client, self.headers.get("X-Forwarded-For"), self._body)
            if answer:
                self._send(*answer)
                return self._code, self._answer
            sent = self.headers.get("Authorization") or ""
            who = ct.authenticate(sent[7:].strip() if sent.lower().startswith("bearer ") else self.headers.get("X-API-Key"))
            if not who:
                self._send(401, {"error": "sign in first", "how": 'POST /login {"email": ..., "password": ...}, then send '
                                                                           'the token as "Authorization: Bearer <token>"'})
                return self._code, self._answer
            refused = ct.allowed(who, method, path)
            if not refused and SITE["name"] == "admin" and not who["operator"] and who["account_id"] != 1 and path not in ("/me", "/logout"):
                refused = "customer accounts sign in on the customer portal, not the admin panel"
            if refused:
                self._send(403, {"error": refused})
                return self._code, self._answer
            mark = WHO.set(who)
            try:
                with use_home(who["home"]):   # from here on, every read and write is in this account's own folder
                    answer = service(who, method, path, parse_qs(url.query), self._body)
                    if answer:
                        self._send(*answer)
                    else:
                        getattr(self, "_" + method.lower())()
            finally:
                WHO.reset(mark)
                if method != "GET":
                    ct.audit(who, f"{method} {path}", self._code or 0)
        except Exception as e:
            if self._code is None:
                bad_input = isinstance(e, (ValueError, json.JSONDecodeError))
                self._send(400 if bad_input else 500, {"error": str(e) if bad_input else f"{type(e).__name__}: {e}"})
        finally:
            if self._code is None:
                self._send(404, {"error": "unknown path", "see": "/docs"})
        return self._code, self._answer

    def _put(self):
        m = re.fullmatch(r"/sources/([^/]+)", urlparse(self.path).path.rstrip("/"))
        if m:
            con = db.connect()
            try:
                return self._send(*change_source(con, unquote(m.group(1)), self._body()))
            except Exception as e:
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})
            finally:
                con.close()
        m = re.fullmatch(r"/profile/([\w-]+)", urlparse(self.path).path.rstrip("/"))
        if m:
            try:
                body = self._body()
                if "content" not in body:
                    return self._send(400, {"error": 'send the new version as {"content": ...}'})
                profilestore.put(m.group(1), body["content"])
                return self._send(200, {"name": m.group(1), "saved": True, "changed_at": profilestore.changed_at(m.group(1))})
            except Exception as e:
                return self._send(400, {"error": str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"})
        if urlparse(self.path).path.rstrip("/") != "/config":
            return self._send(404, {"error": "unknown path", "see": "/"})
        try:
            changes = self._body()
            bad = [k for k in changes if not EDITABLE.match(k)]
            if bad:
                return self._send(400, {"error": "these settings cannot be changed here: " + ", ".join(bad)})
            for key, value in changes.items():   # only settings that exist, with the right kind of value
                problem = setting_problem(key, value)
                if problem:
                    return self._send(400, {"error": problem})
            for key, value in changes.items():
                set_setting(key, value)
            self._send(200, {"changed": list(changes), "overrides": overrides()})
        except Exception as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})

    def _delete(self):
        m = re.fullmatch(r"/sources/([^/]+)", urlparse(self.path).path.rstrip("/"))
        if m:
            result = sourcestore.remove(unquote(m.group(1)))
            return self._send({"removed": 200, "missing": 404, "built-in": 409}[result],
                              {"name": unquote(m.group(1)), "removed": result == "removed"} |
                              ({"error": "a built-in source can only be switched off"} if result == "built-in" else {}))
        if urlparse(self.path).path.rstrip("/") == "/catalogue":
            url = (parse_qs(urlparse(self.path).query).get("url") or [""])[0]
            gone = sourcestore.remove_catalogue(url)
            return self._send(200 if gone else 404, {"url": url, "removed": gone})
        m = re.fullmatch(r"/config/([\w.%]+)", urlparse(self.path).path.rstrip("/"))
        if not m:
            return self._send(404, {"error": "unknown path", "see": "/"})
        self._send(200, {"reset": bool(reset_setting(unquote(m.group(1)))), "overrides": overrides()})

    def _send(self, code, body):
        self._code, self._answer = code, body

    def _get(self):
        url = urlparse(self.path)
        q, path, con = parse_qs(url.query), url.path.rstrip("/") or "/", db.connect()
        try:
            if path == "/":
                return self._send(200, {"docs": "/docs", "endpoints": [f"{m:<6} {route:<30} {what}" for m, route, what, _ in ROUTES]})
            if path == "/status":
                return self._send(200, status(con))
            if path == "/stats":
                return self._send(200, stats(con))
            if path == "/jobs":
                return self._send(200, jobs(con, q))
            m = re.fullmatch(r"/jobs/(\d+)", path)
            if m:
                r = con.execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
                return self._send(200, _row(r, full=True)) if r else self._send(404, {"error": "no such job"})
            if path == "/sent":
                return self._send(200, [_row(r) | {"delivery": rp.delivery(r), "email_subject": r["email_subject"]}
                                        for r in con.execute("SELECT * FROM jobs WHERE send_status='sent' ORDER BY sent_at DESC")])
            if path == "/sources":
                return self._send(200, sources(con, q))
            m = re.fullmatch(r"/sources/([^/]+)", path)
            if m:
                s = sourcestore.get(unquote(m.group(1)), con)
                return self._send(200, _source(con, s, full=True)) if s else self._send(404, {"error": "no such source"})
            if path == "/candidates":
                one = lambda k: (q.get(k) or [None])[0]
                return self._send(200, sourcestore.candidates(con, one("state"), one("region"), one("q")))
            if path == "/countries":
                dc.ensure_table(con)
                done = {r["region"]: dict(r) for r in con.execute("SELECT * FROM discovery")}
                return self._send(200, [{"country": c, **(done.get(c) or {"checked_at": None})} for c in dc.all_countries()])
            if path == "/processes":
                return self._send(200, processes(con))
            if path == "/processes/log":
                return self._send(200, [dict(r) for r in con.execute(
                    "SELECT * FROM process_log ORDER BY id DESC LIMIT ?", (min(int((q.get("limit") or [100])[0]), 1000),))])
            if path == "/catalogue":
                return self._send(200, sourcestore.catalogue((q.get("region") or [None])[0], con))
            if path == "/settings":
                return self._send(200, settings_list())
            if path == "/profile":
                return self._send(200, [{"name": n, "changed_at": profilestore.changed_at(n, con)} for n in profilestore.DOCUMENTS])
            m = re.fullmatch(r"/profile/([\w-]+)", path)
            if m:
                if m.group(1) not in profilestore.DOCUMENTS:
                    return self._send(404, {"error": "no such document", "documents": list(profilestore.DOCUMENTS)})
                return self._send(200, {"name": m.group(1), "changed_at": profilestore.changed_at(m.group(1), con),
                                        "content": profilestore.get(m.group(1), con)})
            if path == "/config":
                return self._send(200, {"effective": load_config(), "overrides": overrides()})
            if path == "/runs":
                return self._send(200, [dict(r) for r in con.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (min(int((q.get("limit") or [50])[0]), 500),))])
            self._send(404, {"error": "unknown path", "see": "/"})
        except Exception as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})
        finally:
            con.close()

    def _post(self):
        path = urlparse(self.path).path.rstrip("/")
        m = re.fullmatch(r"/processes/(\w+)/(enable|disable)", path)
        if m:
            if m.group(1) not in PROCESSES:
                return self._send(404, {"error": "no such step", "steps": list(PROCESSES)})
            set_setting(f"process.{m.group(1)}", m.group(2) == "enable")
            return self._send(200, {"name": m.group(1), "enabled": m.group(2) == "enable"})
        if path in ("/sources", "/candidates", "/catalogue"):
            con = db.connect()
            try:
                body = self._body()
                if path == "/sources":
                    return self._send(*add_source(con, body))
                if not _web_address(body.get("url")):
                    return self._send(400, {"error": "give a url (http or https)"})
                add = sourcestore.add_catalogue if path == "/catalogue" else sourcestore.add_candidate
                new = add(body["url"], body.get("region") or "Remote", con)
                return self._send(201 if new else 200, {"url": body["url"], "added": new})
            except Exception as e:
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})
            finally:
                con.close()
        m = re.fullmatch(r"/sources/([^/]+)/(enable|disable)", path)
        if m:
            ok = sourcestore.set_enabled(unquote(m.group(1)), m.group(2) == "enable")
            return self._send(200 if ok else 404, {"name": unquote(m.group(1)), "enabled": m.group(2) == "enable", "found": ok})
        m = re.fullmatch(r"/jobs/(\d+)/prep", path)
        if m:
            from . import prep
            con = db.connect()
            try:
                r = con.execute("SELECT * FROM jobs WHERE id=?", (m.group(1),)).fetchone()
                if not r:
                    return self._send(404, {"error": "no such job"})
                text, engine = prep.generate(r, load_config())
                return self._send(200, {"id": r["id"], "written_by": engine, "file": str(prep.save(con, r, text)),
                                        "interview_prep": text})
            except Exception as e:
                return self._send(503, {"error": str(e)})
            finally:
                con.close()
        m = re.fullmatch(r"/jobs/(\d+)/(approve|reject)", path)
        if not m:
            return self._send(404, {"error": "unknown path", "see": "/"})
        con = db.connect()
        try:
            job_id, action = int(m.group(1)), m.group(2)
            if action == "approve":
                n = con.execute("UPDATE jobs SET send_status='approved' WHERE id=? AND send_status IS NULL AND emails!='[]' "
                                "AND (resume_path IS NOT NULL OR job_type='lead')", (job_id,)).rowcount
            else:
                n = con.execute("UPDATE jobs SET send_status='rejected' WHERE id=? AND COALESCE(send_status,'') "
                                "NOT IN ('sent','sending')", (job_id,)).rowcount
            con.commit()
            r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r:
                return self._send(404, {"error": "no such job"})
            self._send(200 if n else 409, {"changed": bool(n), "job": _row(r)})
        finally:
            con.close()


# Every route: method, path, what it does, section in /docs. A path not listed here does not exist.
ROUTES = [
    ("GET", "/health", "Is the service up (open)", "Service"),
    ("GET", "/", "The site's own page: the customer portal on the customer site, the admin panel on the admin site (open)", "Service"),
    ("GET", "/admin", "The admin panel for the service operator (open; it asks you to sign in)", "Service"),
    ("GET", "/app", "The customer portal: sign up, resume, targets, mailbox, bot on/off (open; it asks you to sign in)", "Service"),
    ("GET", "/console", "The detailed bot console of one account (open; it asks you to sign in)", "Service"),
    ("GET", "/plans", "The plans on sale, with their limits (open)", "Sign-up and enquiries"),
    ("POST", "/register", "Create a customer account and sign in; starts a 14-day trial (open)", "Sign-up and enquiries"),
    ("POST", "/enquiries", "Send an enquiry to the service from anywhere (open)", "Sign-up and enquiries"),
    ("GET", "/portal/overview", "Customer: checklist, plan, today's numbers, bot state, latest applications", "Customer portal"),
    ("GET", "/portal/access", "Customer: paid or not, set-up finished or not, and what is locked", "Customer portal"),
    ("POST", "/portal/onboarding/complete", "Customer: finish the first set-up (after it, changes need a paid plan)", "Customer portal"),
    ("GET", "/portal/details", "Customer: personal and career details", "Customer portal"),
    ("PUT", "/portal/details", "Customer: save personal and career details", "Customer portal"),
    ("GET", "/portal/cover-letter", "Customer: their cover letter", "Customer portal"),
    ("PUT", "/portal/cover-letter", "Customer: save the cover letter {text}", "Customer portal"),
    ("POST", "/portal/cover-letter/upload", "Customer: read a cover letter from a PDF, DOCX or TXT {filename, data_base64}", "Customer portal"),
    ("POST", "/portal/suggest-targets", "Customer: job titles, keywords and skills suggested from the resume", "Customer portal"),
    ("POST", "/portal/ai", "Customer (paid): AI help on the resume and cover letter only {task, text?, request?}", "Customer portal"),
    ("GET", "/portal/targets", "Customer: what the bot looks for", "Customer portal"),
    ("PUT", "/portal/targets", "Customer: countries, job types, keywords, titles, skills, daily limit, review mode", "Customer portal"),
    ("GET", "/portal/resume", "Customer: the resume the bot writes from", "Customer portal"),
    ("PUT", "/portal/resume", "Customer: save an edited resume {content}", "Customer portal"),
    ("POST", "/portal/resume/upload", "Customer: upload a PDF, DOCX or TXT resume {filename, data_base64}", "Customer portal"),
    ("GET", "/portal/mailbox", "Customer: connected sending mailboxes (never the passwords)", "Customer portal"),
    ("PUT", "/portal/mailbox", "Customer (owner): connect mailboxes {mailboxes: [{email, app_password, smtp}]}", "Customer portal"),
    ("POST", "/portal/mailbox/test", "Customer: test signing in to each mailbox", "Customer portal"),
    ("GET", "/portal/bot", "Customer: is the bot on, what is it doing", "Customer portal"),
    ("POST", "/portal/bot/on", "Customer: turn the bot on (runs every 2 hours)", "Customer portal"),
    ("POST", "/portal/bot/off", "Customer: turn the bot off and stop a run in progress", "Customer portal"),
    ("POST", "/portal/bot/run", "Customer: run once now", "Customer portal"),
    ("GET", "/portal/queue", "Customer: prepared emails; stage = review | approved | matched | found", "Customer portal"),
    ("POST", "/portal/queue/{job_id}/approve", "Customer: approve one prepared email", "Customer portal"),
    ("POST", "/portal/queue/{job_id}/reject", "Customer: never send for this job", "Customer portal"),
    ("POST", "/portal/queue/{job_id}/pick", "Customer: choose a found job by hand; the next run prepares it", "Customer portal"),
    ("GET", "/portal/jobs/{job_id}", "Customer: one job in full, before or after sending (post, tailored resume, email, outcome)", "Customer portal"),
    ("PUT", "/portal/jobs/{job_id}/tailored", "Customer: tailor the resume and email for this job by hand", "Customer portal"),
    ("POST", "/portal/jobs/{job_id}/retailor", "Customer: discard this job's tailoring; the next run writes it again", "Customer portal"),
    ("GET", "/portal/applications", "Customer: every application sent, with replies and bounces", "Customer portal"),
    ("GET", "/portal/billing", "Customer: plan, paid period, plans and payment methods for their country, payments", "Customer portal"),
    ("POST", "/portal/checkout", "Customer (owner): start paying for a plan {plan, gateway}", "Customer portal"),
    ("POST", "/portal/payments/{payment_id}/confirm", "Customer: finish a test payment {result: success|fail}", "Customer portal"),
    ("GET", "/portal/tickets", "Customer: their support requests and feedback, with replies", "Customer portal"),
    ("POST", "/portal/tickets", "Customer: send a support request or feedback {kind, category, subject, message, rating}", "Customer portal"),
    ("GET", "/operator/overview", "Operator: every customer with plan, bot state and numbers; service totals", "Admin panel"),
    ("GET", "/operator/payments", "Operator: every payment, and money collected in the last 30 days", "Admin panel"),
    ("GET", "/operator/gateways", "Operator: payment gateways and the markets they serve", "Admin panel"),
    ("PUT", "/operator/gateways/{code}", "Operator: switch a gateway on or off, or change its markets", "Admin panel"),
    ("GET", "/operator/tickets", "Operator: support requests and feedback; filter with kind and status", "Admin panel"),
    ("PUT", "/operator/tickets/{ticket_id}", "Operator: reply to a ticket or set its status {reply, status}", "Admin panel"),
    ("PUT", "/operator/accounts/{account_id}", "Operator: change a customer's plan or status {plan, status}", "Admin panel"),
    ("GET", "/operator/plans", "Operator: every plan, hidden ones too", "Admin panel"),
    ("POST", "/operator/plans", "Operator: create a plan for a market (starts switched off)", "Admin panel"),
    ("GET", "/operator/markets", "Operator: every market, live or planned", "Admin panel"),
    ("POST", "/operator/markets", "Operator: add or change a market {code, name, currency, position, note}", "Admin panel"),
    ("PUT", "/operator/plans/{name}", "Operator: change a plan's price, limits or features", "Admin panel"),
    ("GET", "/operator/enquiries", "Operator: enquiries; filter with status and q", "Admin panel"),
    ("PUT", "/operator/enquiries/{enquiry_id}", "Operator: set an enquiry's status or note {status, note}", "Admin panel"),
    ("GET", "/", "List of every route", "Service"),
    ("GET", "/setup", "Does the very first user still have to be created (open)", "Sign-in"),
    ("POST", "/setup", "Create the very first user; only on the service's own computer (open)", "Sign-in"),
    ("POST", "/login", "Sign in with email and password; returns a token valid for 14 days (open)", "Sign-in"),
    ("POST", "/logout", "End this sign-in", "Sign-in"),
    ("GET", "/me", "Who you are, your account, and whether the bot may run for it yet", "Sign-in"),
    ("GET", "/keys", "Your API keys (never the keys themselves)", "Sign-in"),
    ("POST", "/keys", "Make an API key; it is shown this once", "Sign-in"),
    ("DELETE", "/keys/{key_id}", "Revoke an API key", "Sign-in"),
    ("GET", "/users", "Owner: the people in your account", "Account"),
    ("POST", "/users", "Owner: add a person with a role", "Account"),
    ("PUT", "/users/{user_id}", "Owner: change a person's role, or switch them off", "Account"),
    ("GET", "/audit", "Owner: every change made through the API, and by whom", "Account"),
    ("GET", "/accounts", "Operator: the customers of the service", "Account"),
    ("POST", "/accounts", "Operator: create a customer account with its first user", "Account"),
    ("GET", "/schedules", "When the bot runs", "Schedule and runs"),
    ("POST", "/schedules", "Add a schedule: daily at a time on weekdays, or every N minutes", "Schedule and runs"),
    ("PUT", "/schedules/{schedule_id}", "Change a schedule, or switch it on or off", "Schedule and runs"),
    ("DELETE", "/schedules/{schedule_id}", "Delete a schedule", "Schedule and runs"),
    ("GET", "/cycles", "Runs of the whole pipeline, newest first", "Schedule and runs"),
    ("POST", "/cycles", "Run now", "Schedule and runs"),
    ("POST", "/cycles/{cycle_id}/stop", "Stop a run that is waiting or going", "Schedule and runs"),
    ("GET", "/cycles/{cycle_id}/log", "What a run printed", "Schedule and runs"),
    ("GET", "/status", "Numbers for today and in total, and what the bot is doing", "Jobs"),
    ("GET", "/stats", "Sent per day, by kind, field and source", "Jobs"),
    ("GET", "/jobs", "Jobs; filter with type, stage, has_email, country, source, q, days, limit, offset", "Jobs"),
    ("GET", "/jobs/{job_id}", "One job with description and email draft", "Jobs"),
    ("POST", "/jobs/{job_id}/approve", "Approve a prepared email for sending", "Jobs"),
    ("POST", "/jobs/{job_id}/reject", "Never send for this job", "Jobs"),
    ("POST", "/jobs/{job_id}/prep", "Write the interview guide for this job", "Jobs"),
    ("GET", "/sent", "Every application that was sent", "Jobs"),
    ("GET", "/runs", "Log of source reads", "Jobs"),
    ("GET", "/sources", "Job sources and their last read; filter with enabled, region, reader, q", "Sources"),
    ("POST", "/sources", "Add a source from a URL, or by naming the reader", "Sources"),
    ("GET", "/sources/{name}", "One source with its settings and last 10 reads", "Sources"),
    ("PUT", "/sources/{name}", "Change a source", "Sources"),
    ("DELETE", "/sources/{name}", "Delete an added source", "Sources"),
    ("POST", "/sources/{name}/enable", "Switch a source on", "Sources"),
    ("POST", "/sources/{name}/disable", "Switch a source off", "Sources"),
    ("GET", "/candidates", "Sites on the list to try, with their test result", "Sources"),
    ("POST", "/candidates", "Put a site on the list to try", "Sources"),
    ("GET", "/catalogue", "Known job sites tried first when looking for sources", "Sources"),
    ("POST", "/catalogue", "Add a known site", "Sources"),
    ("DELETE", "/catalogue", "Remove a known site (?url=...)", "Sources"),
    ("GET", "/countries", "Discovery progress per country", "Sources"),
    ("GET", "/processes", "Every pipeline step: on or off, and its last run", "Settings"),
    ("GET", "/processes/log", "History of step runs", "Settings"),
    ("POST", "/processes/{name}/enable", "Switch a step on", "Settings"),
    ("POST", "/processes/{name}/disable", "Switch a step off", "Settings"),
    ("GET", "/settings", "Every setting: value, default, explanation", "Settings"),
    ("GET", "/config", "The settings as one nested object", "Settings"),
    ("PUT", "/config", "Change settings", "Settings"),
    ("DELETE", "/config/{key}", "Put a setting back to its default", "Settings"),
    ("GET", "/profile", "The documents the bot writes from", "Settings"),
    ("GET", "/profile/{name}", "One document: resume, email-templates or resume-template", "Settings"),
    ("PUT", "/profile/{name}", "Save a new version of a document", "Settings"),
]
OPEN = {("GET", "/health"), ("GET", "/"), ("GET", "/admin"), ("GET", "/app"), ("GET", "/console"), ("GET", "/setup"), ("POST", "/setup"),
        ("POST", "/login"), ("GET", "/plans"), ("POST", "/register"), ("POST", "/enquiries")}
MAX_BODY = 1_500_000   # bytes; the largest request is a resume upload (700 KB file, sent as base64)


def create_app():
    """The FastAPI application. Each route hands the request to Exchange, which holds the rules."""
    from fastapi import Depends, FastAPI, Request
    from fastapi.responses import JSONResponse, Response
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from starlette.concurrency import run_in_threadpool

    app = FastAPI(title="Job Bot API", version="2.0",
                  description="Find jobs, tailor the resume, send applications. Every route except the ones marked "
                              "open needs \"Authorization: Bearer <token>\" (from POST /login) or an API key, and works "
                              "only on the data of that user's own account.")
    bearer = HTTPBearer(auto_error=False, description="The token from POST /login, or an API key from POST /keys")

    @app.middleware("http")
    async def harden(request: Request, call_next):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"error": "the request is too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        if request.url.path in PAGES[SITE["name"]]:
            # The pages may talk only to this service, load nothing from elsewhere and never be framed.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        if request.url.path not in PAGES[SITE["name"]]:
            response.headers["Cache-Control"] = "no-store"   # answers hold personal data: never kept by a browser or proxy
        return response

    async def answer(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "the request is too large"}, status_code=413)
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        exchange = Exchange(request.method, target, request.headers, request.client.host if request.client else "", raw)
        code, body = await run_in_threadpool(exchange.run)   # database and web reads block, so not on the event loop
        if isinstance(body, bytes):
            return Response(body, status_code=code, media_type="text/html; charset=utf-8")
        return Response(json.dumps(body, ensure_ascii=False, default=str), status_code=code,
                        media_type="application/json; charset=utf-8")

    async def signed_in(request: Request, _: HTTPAuthorizationCredentials = Depends(bearer)):
        return await answer(request)   # the dependency only tells /docs that the route needs a token; Exchange checks it

    for method, path, what, section in ROUTES:
        app.add_api_route(path, answer if (method, path) in OPEN else signed_in, methods=[method], summary=what,
                          tags=[section], name=f"{method} {path}")
    return app


def serve(port=8765, host="127.0.0.1", with_scheduler=False, site="all"):
    """Listen until stopped. host 127.0.0.1 = this computer only; to be reached from elsewhere, keep that
    and put an HTTPS reverse proxy in front (see deploy/), rather than opening it to the network directly."""
    import threading
    import uvicorn
    import os
    if site not in PAGES:
        raise ValueError("site must be customer, admin or all")
    SITE["name"] = site
    config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning", access_log=False, proxy_headers=True,
                            forwarded_allow_ips=os.environ.get("JOBBOT_TRUSTED_PROXY", "127.0.0.1"))
    # Bind first, and exclusively: a second copy fails here and starts no second scheduler. Windows lets two
    # programs share a port that uvicorn opens with SO_REUSEADDR, so the port is claimed with SO_EXCLUSIVEADDRUSE.
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise SystemExit(f"port {port} is already in use: this site is running already")
    sock.listen(2048)
    sock.set_inheritable(True)
    stop = threading.Event()
    if with_scheduler:
        threading.Thread(target=scheduler.run_forever, args=(stop,), daemon=True).start()
    try:
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        stop.set()
