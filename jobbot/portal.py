"""The service as a product: what customers and the service operator use, on top of the bot.

Customers (any person who registers) get their own account, folder and database. Everything about them is
data, never code: the resume they upload, the jobs they target, the mailbox the bot sends from, and whether
their bot is on. The operator sees every account, the plans and their limits, and every enquiry made through
the public form. All of it is reached through the REST routes in api.py:

    open        POST /register, POST /enquiries, GET /plans
    customer    /portal/...   (their own account only)
    operator    /operator/... (the whole service)
"""
import base64
import datetime as dt
import io
import json
import re
import zipfile

from . import control as ct
from . import db, home, profilestore, scheduler, secrets_box, sourcestore
from .config import load_config, set_setting, setting_problem

# Which sources belong to which kind of work, by the reader they use.
GROUPS = {
    "government": {"notices"},
    "freelance": {"freelancer"},
    "leads": {"ted", "ted_awards", "uk_contracts", "uk_find_a_tender"},
}
JOB_TYPES = ("full_time", "part_time", "freelance", "government", "leads")
TOPICS = ("job seeker", "business", "partnership", "support", "other")
ENQUIRY_STATUSES = ("new", "in progress", "answered", "closed", "spam")
MAX_UPLOAD = 700_000   # bytes of the resume file itself (the request carries it as base64)


# ---------- open: sign-up, plans, enquiries ----------

def plans(include_hidden=False, market=None):
    rows = ct._all("SELECT * FROM plans ORDER BY position")
    return [p for p in rows if (include_hidden or p["active"]) and (not market or p["market"] == market)]


def markets():
    """Every market in launch order; live = it has at least one active plan, so customers there can sign up."""
    live = {p["market"] for p in plans()}
    return [m | {"live": m["code"] in live} for m in ct._all("SELECT * FROM markets ORDER BY position, code")]


def market_save(body):
    code = (body.get("code") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code) or not (body.get("name") or "").strip() or not re.fullmatch(r"[A-Z]{3}", (body.get("currency") or "").upper()):
        raise ValueError("a market needs a 2-letter country code, a name and a 3-letter currency code")
    ct._write("INSERT INTO markets(code, name, currency, position, note) VALUES(?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
              "name=excluded.name, currency=excluded.currency, position=excluded.position, note=excluded.note",
              code, body["name"].strip(), body["currency"].upper(), int(body.get("position") or 99), body.get("note") or "")
    return ct._one("SELECT * FROM markets WHERE code=?", code)


def plan_create(body):
    """A new plan, for example when a market launches. Starts switched off until the operator turns it on."""
    name = (body.get("name") or "").strip().lower()
    market = ct._one("SELECT * FROM markets WHERE code=?", (body.get("market") or "").upper())
    if not re.fullmatch(r"[a-z0-9-]{2,30}", name) or ct._one("SELECT 1 FROM plans WHERE name=?", name):
        raise ValueError("the plan needs a new short name (letters, digits, dashes)")
    if not market:
        raise ValueError("add the market first")
    ct._write("INSERT INTO plans(name, title, daily_cap, mailboxes, price_month, currency, features, position, active, market) "
              "VALUES(?,?,?,?,?,?,?,?,0,?)", name, body.get("title") or name, int(body.get("daily_cap") or 10),
              int(body.get("mailboxes") or 1), float(body.get("price_month") or 0), market["currency"], body.get("features") or "",
              int(body.get("position") or 50), market["code"])
    return ct._one("SELECT * FROM plans WHERE name=?", name)


def is_paid(account):
    return (plan_of(account) or {}).get("price_month", 0) > 0


def plan_of(account):
    return ct._one("SELECT * FROM plans WHERE name=?", account.get("plan") or "trial") or plans()[0]


def register(body, client):
    """A new customer: account, owner user, a 14-day trial, and a session token so they are signed in at once."""
    if ct._too_many(("register", client)):
        raise PermissionError("wait")
    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip()
    problem = (None if name else "your name is needed") or ct.password_problem(body.get("password")) \
        or (None if ct.EMAIL.match(email) else "that is not an email address") \
        or ("an account with that email already exists; sign in instead" if ct._one("SELECT 1 FROM users WHERE email=?", email) else None)
    if problem:
        raise ValueError(problem)
    if body.get("accept_terms") not in (True, "on", "true", 1):
        raise ValueError("please accept the Terms of Service and the Privacy Policy")
    known = {m["code"]: m for m in markets()}
    market = (body.get("market") or "IN").upper()
    if market not in known:
        raise ValueError("choose your country")
    trial = next((p["name"] for p in plans(market=market) if p["price_month"] == 0), None)
    if not trial:   # checked before anything is created
        raise ValueError(f"sign-up for {known[market]['name']} opens soon; leave your details in the contact form")
    timezone = body.get("timezone") or "UTC"
    try:
        acc = ct.account_create(name, timezone)
    except Exception:
        acc = ct.account_create(name, "UTC")
    # The free plan sets up the profile only; the bot runs after a payment (access.py), so there is no trial end date.
    from .api import TERMS_VERSION
    ct._write("UPDATE accounts SET plan=?, market=?, trial_ends_at=NULL, contact_email=?, terms_version=?, terms_accepted_at=? WHERE id=?",
              trial, market, email, TERMS_VERSION, ct.now(), acc["id"])
    user = ct.user_create(acc["id"], email, body["password"], "owner", name)
    with ct._failures_lock:   # count sign-ups per address, so one client cannot create accounts by the hundred
        ct._failures.setdefault(("register", client), []).append(dt.datetime.now().timestamp())
    token, expires = ct.token_create(user["id"], "session", "signed up", ct.SESSION_DAYS)
    return {"token": token, "expires_at": expires, "user": user, "account": ct.account(acc["id"])}


def enquiry_create(body, client):
    """Anyone, anywhere: a question or request for the operator. A hidden 'website' field catches simple bots."""
    if body.get("website"):
        return {"received": True}          # a bot filled the invisible field: say thank you, keep nothing
    if ct._too_many(("enquiry", client)):
        raise PermissionError("wait")
    name, email = (body.get("name") or "").strip()[:100], (body.get("email") or "").strip().lower()[:200]
    message = (body.get("message") or "").strip()
    topic = body.get("topic") if body.get("topic") in TOPICS else "other"
    if not name or not ct.EMAIL.match(email) or len(message) < 5:
        raise ValueError("name, a valid email and a message are needed")
    _, enquiry_id = ct._write(
        "INSERT INTO enquiries(created_at, name, email, phone, country, company, topic, message, client) VALUES(?,?,?,?,?,?,?,?,?)",
        ct.now(), name, email, (body.get("phone") or "")[:40], (body.get("country") or "")[:60], (body.get("company") or "")[:120],
        topic, message[:4000], client)
    with ct._failures_lock:
        ct._failures.setdefault(("enquiry", client), []).append(dt.datetime.now().timestamp())
    return {"received": True, "id": enquiry_id}


# ---------- customer: their own account ----------

def _prefs_table(con):
    pass   # portal_prefs is one of the account's views in the single database (db.py)


def _pref(con, key, default=None):
    _prefs_table(con)
    r = con.execute("SELECT value FROM portal_prefs WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def _set_pref(con, key, value):
    _prefs_table(con)
    as_json = json.dumps(value, ensure_ascii=False)
    if not con.execute("UPDATE portal_prefs SET value=?, changed_at=? WHERE key=?", (as_json, ct.now(), key)).rowcount:
        con.execute("INSERT INTO portal_prefs(key, value, changed_at) VALUES(?,?,?)", (key, as_json, ct.now()))
    con.commit()


DEFAULT_TARGETS = {
    "countries": ["Remote"], "relocate": False, "remote_only_countries": [],
    "job_types": {"full_time": True, "part_time": True, "freelance": False, "government": False, "leads": False},
    "keywords": [], "part_time_keywords": [], "titles": [], "skills": [], "avoid_titles": [],
    "min_score": 30, "daily_limit": 10, "review": "manual",
}


def targets_get():
    con = db.connect()
    try:
        cfg = load_config()
        saved = _pref(con, "targets", {})
        t = {**DEFAULT_TARGETS, **saved}
        if not saved:   # first visit: show what the account is set to now
            t |= {"countries": cfg.get("regions") or ["Remote"], "relocate": bool(cfg.get("open_to_relocation")),
                  "keywords": cfg["search"]["keywords"], "part_time_keywords": cfg["search"]["part_time_keywords"],
                  "titles": cfg["profile"]["titles"], "skills": cfg["profile"]["skills_primary"],
                  "daily_limit": cfg["sending"]["daily_ceiling"], "review": cfg["sending"].get("review", "manual")}
        return t
    finally:
        con.close()


def _words(value, limit=60):
    items = value if isinstance(value, list) else re.split(r"[,\n]", value or "")
    return [w.strip()[:60] for w in items if isinstance(w, str) and w.strip()][:limit]


def targets_put(body, account):
    """Save what the customer wants and turn it into the bot's settings and source switches."""
    t = {**targets_get(), **{k: v for k, v in body.items() if k in DEFAULT_TARGETS}}
    t["countries"] = _words(t["countries"], 80) or ["Remote"]
    t["remote_only_countries"] = _words(t["remote_only_countries"], 40)
    for key in ("keywords", "part_time_keywords", "titles", "skills", "avoid_titles"):
        t[key] = _words(t[key])
    t["job_types"] = {k: bool((t.get("job_types") or {}).get(k)) for k in JOB_TYPES}
    cap = plan_of(account)["daily_cap"]
    t["daily_limit"] = max(1, min(int(t.get("daily_limit") or 1), cap))
    t["min_score"] = max(0, min(int(t.get("min_score") or 30), 100))
    t["review"] = "auto" if t.get("review") == "auto" and is_paid(account) else "manual"   # automatic sending is a paid feature

    cfg = load_config()
    changes = {
        "regions": t["countries"], "open_to_relocation": bool(t["relocate"]),
        "remote_only_countries": {c: [c] for c in t["remote_only_countries"]},
        "search.keywords": t["keywords"] or cfg["search"]["keywords"],
        "search.part_time_keywords": t["part_time_keywords"] or cfg["search"]["part_time_keywords"],
        "profile.titles": t["titles"] or cfg["profile"]["titles"], "profile.skills_primary": t["skills"],
        "avoid.title_words": t["avoid_titles"] or cfg["avoid"]["title_words"],
        "min_score.full": t["min_score"], "min_score.part": max(0, t["min_score"] - 5),
        "daily_target.full": 50 if t["job_types"]["full_time"] else 0,
        "daily_target.part": 50 if t["job_types"]["part_time"] or t["job_types"]["freelance"] else 0,
        "sending.daily_ceiling": t["daily_limit"], "sending.review": t["review"],
        # a customer's jobs must fit their own roles, checked by rule and then by the AI; no favoured country
        "match.strict": True, "match.ai_check": True, "priority_countries.bonus": 0,
    }
    skipped = []
    for key, value in changes.items():
        problem = setting_problem(key, value)
        if problem:
            skipped.append(f"{key}: {problem}")
        else:
            set_setting(key, value)
    con = db.connect()
    try:
        _set_pref(con, "targets", t)
        switched = _switch_groups(con, t["job_types"])
    finally:
        con.close()
    return {"targets": t, "sources_switched": switched, "not_applied": skipped, "plan_daily_cap": cap}


def _switch_groups(con, job_types):
    """Sources are shared by every account in the single database, so a customer never switches them: the kinds
    of work they chose filter the shared postings instead (cli._judge_pool). Kept for the API's answer: 0."""
    return 0


# --- resume ---

def _text_from_upload(filename, data):
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.page_count > 15:
                raise ValueError("a resume of more than 15 pages is not accepted")
            return "\n".join(p.get_text() for p in doc)
    if not (name.endswith((".txt", ".docx")) or data[:2] == b"PK"):
        raise ValueError("upload a PDF, DOCX or TXT file")
    if name.endswith(".docx") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.getinfo("word/document.xml").file_size > 8_000_000:   # a zip that unpacks to something huge is refused
                raise ValueError("that document is too large to read")
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    return data.decode("utf-8", "replace")


RESUME_PROMPT = """Turn this resume into JSON with exactly these keys and kinds of value, using only facts that are in
the text (never invent employers, dates, numbers or skills; leave a field empty if the text does not say):
{"name": str, "headline": str, "contact": {"email": str, "phone": str, "location": str, "links": [str]},
 "summary": str, "summary_taiwan_only": "", "skills": [{"label": str, "items": [str]}],
 "experience": [{"title": str, "dates": str, "company": str, "bullets": [str], "stack": str}],
 "projects": [{"name": str, "text": str}], "education": [{"degree": str, "year": str, "school": str}],
 "additional": [{"label": str, "text": str, "taiwan_only": false}]}
Answer with the JSON only.

RESUME TEXT:
"""


def _structure(text):
    """Resume text -> the resume document, by the account's AI engine when there is one, else by simple rules."""
    from . import tailor
    chain, _ = tailor.engines(load_config())
    if chain:
        try:
            answer, engine = tailor.ask_text(RESUME_PROMPT + text[:15000], chain, effort="low")
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", answer.strip(), flags=re.M).strip())
            return data, f"read by {engine}"
        except Exception as e:
            note = f"AI reading failed ({str(e)[:80]}); basic reading used"
    else:
        note = "no AI key configured; basic reading used"
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    email = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", text)
    phone = re.search(r"\+?\d[\d ()-]{7,}\d", text)
    return {"name": lines[0][:80] if lines else "", "headline": lines[1][:120] if len(lines) > 1 else "",
            "contact": {"email": email.group(0) if email else "", "phone": phone.group(0) if phone else "", "location": "",
                        "links": re.findall(r"https?://\S+", text)[:3] or [""]},
            "summary": " ".join(lines[2:6])[:600]}, note


def _fit(data, template):
    """Shape data like the template: every key present, every value of the template's kind."""
    if isinstance(template, dict):
        data = data if isinstance(data, dict) else {}
        return {k: _fit(data.get(k), v) for k, v in template.items()}
    if isinstance(template, list):
        item = template[0] if template else None
        if not isinstance(data, list) or not data:
            return [_fit({}, item)] if isinstance(item, dict) else list(template)
        if isinstance(item, (dict, list)):
            return [_fit(x, item) for x in data]
        return [str(x) for x in data if x not in (None, "")] if isinstance(item, str) else data
    if isinstance(template, bool):
        return bool(data) if data is not None else template
    if isinstance(template, str):
        return str(data) if data not in (None, "") else ""
    return data if data is not None else template


def resume_upload(body):
    raw = base64.b64decode(body.get("data_base64") or "", validate=False)
    if not raw:
        raise ValueError("send the file as data_base64")
    if len(raw) > MAX_UPLOAD:
        raise ValueError("the file is too large (700 KB at most)")
    text = _text_from_upload(body.get("filename"), raw)
    if len(text.strip()) < 50:
        raise ValueError("no text could be read from that file; upload a PDF, DOCX or TXT with selectable text")
    folder = home() / "uploads"
    folder.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", body.get("filename") or "resume")[-80:]
    (folder / f"{dt.date.today():%Y%m%d}_{safe}").write_bytes(raw)
    data, how = _structure(text)
    resume = _fit(data, profilestore.get("resume"))
    if not resume["name"].strip():
        resume["name"] = "Your Name"
    problem = profilestore.problem("resume", resume)
    if problem:
        return {"saved": False, "resume": resume, "how": how, "problem": problem}
    profilestore.put("resume", resume)
    return {"saved": True, "resume": resume, "how": how,
            "check": "Read every field before turning the bot on: the bot writes only from what is here."}


def resume_put(body):
    content = body.get("content")
    problem = profilestore.problem("resume", content)
    if problem:
        raise ValueError(problem)
    profilestore.put("resume", content)
    return {"saved": True}


# --- mailbox ---

def _env_path():
    return home() / ".env"


def _read_env():
    path = _env_path()
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def mailbox_get():
    from . import mailer
    return [{"email": s["email"], "smtp": s["smtp"], "has_password": bool(s["password"]), "connected_by": s["oauth"] or "app password",
             "ready": s["can_send"]} for s in mailer.senders()]


def mailbox_put(body, account):
    """Replace the account's sending mailboxes. Passwords live only in the account's own .env file, never in the database."""
    boxes = body.get("mailboxes")
    if not isinstance(boxes, list) or not boxes:
        raise ValueError('send {"mailboxes": [{"email": ..., "app_password": ..., "smtp": "smtp.gmail.com"}]}')
    limit = plan_of(account)["mailboxes"]
    if len(boxes) > limit:
        raise ValueError(f"your plan allows {limit} mailbox(es)")
    old = {s["email"]: s for s in mailbox_get_full()}
    lines = [l for l in _read_env() if not re.match(r"\s*SENDER_\d+", l)]
    for n, b in enumerate(boxes, 1):
        email = (b.get("email") or "").strip().lower()
        if not ct.EMAIL.match(email):
            raise ValueError(f"not an email address: {email}")
        password = (b.get("app_password") or "").replace(" ", "") or (old.get(email) or {}).get("password", "")
        if not password:
            raise ValueError(f"an app password is needed for {email}")
        if re.search(r"\s", password) or len(password) > 200:
            raise ValueError(f"the app password for {email} is not valid")
        smtp = (b.get("smtp") or "smtp.gmail.com").strip().lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", smtp) or len(smtp) > 100:
            raise ValueError(f"not a mail server name: {smtp}")
        lines += [f"SENDER_{n}={email}", f"SENDER_{n}_APP_PASSWORD={secrets_box.seal(password)}"]   # encrypted at rest
        if smtp != "smtp.gmail.com":
            lines.append(f"SENDER_{n}_SMTP={smtp}")
    _env_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    emails = [(b.get("email") or "").strip().lower() for b in boxes]
    set_setting("sending.send_from", emails)
    set_setting("sending.cc", {})
    return {"mailboxes": mailbox_get()}


def mailbox_add_oauth(account, provider, email, refresh_token):
    """Add (or renew) a mailbox connected by signing in. The refresh token is stored encrypted in the account's .env."""
    from . import mailer
    current = mailer.senders()
    existing = next((s for s in current if s["email"].lower() == email), None)
    if not existing and len(current) >= plan_of(account)["mailboxes"]:
        raise ValueError(f"your plan allows {plan_of(account)['mailboxes']} mailbox(es); remove one first")
    n = existing["n"] if existing else max([s["n"] for s in current] or [0]) + 1
    lines = [l for l in _read_env() if not re.match(rf"\s*SENDER_{n}(_|=)", l)]
    lines += [f"SENDER_{n}={email}", f"SENDER_{n}_OAUTH={provider}", f"SENDER_{n}_OAUTH_TOKEN={secrets_box.seal(refresh_token)}"]
    _env_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_setting("sending.send_from", sorted({s["email"].lower() for s in current} | {email}))
    return {"mailboxes": mailbox_get()}


def mailbox_remove(email):
    """Disconnect one mailbox (its password or token is deleted from the account's .env)."""
    from . import mailer
    target = next((s for s in mailer.senders() if s["email"].lower() == (email or "").lower()), None)
    if not target:
        return None
    lines = [l for l in _read_env() if not re.match(rf"\s*SENDER_{target['n']}(_|=)", l)]
    _env_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_setting("sending.send_from", [s["email"].lower() for s in mailer.senders()])
    return {"mailboxes": mailbox_get()}


def mailbox_get_full():
    from . import mailer
    return mailer.senders()


def mailbox_test():
    from . import mailer
    out = []
    for s in mailer.senders():
        try:
            mailer.check_login(s)
            out.append({"email": s["email"], "ok": True})
        except Exception as e:
            out.append({"email": s["email"], "ok": False, "error": str(e)[:160]})
    return out


# --- bot on and off ---

def bot_state(account_id):
    rows = scheduler.schedules(account_id)
    running = ct._one("SELECT * FROM cycles WHERE account_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", account_id)
    last = ct._one("SELECT * FROM cycles WHERE account_id=? AND status NOT IN ('queued','running') ORDER BY id DESC LIMIT 1", account_id)
    return {"on": any(s["enabled"] for s in rows), "schedules": rows, "running": running, "last_run": last}


def bot_switch(who, on):
    if who["account_id"] == 1:
        raise ValueError("the owner's account is run by the Windows loop; use `jobbot loop start|stop` for it")
    acc = ct.account(who["account_id"])
    if on:
        reason = ct.ready_to_run(acc) or (None if mailbox_get() else "connect a mailbox first")
        if reason:
            raise ValueError(f"the bot cannot start yet: {reason}")
        rows = scheduler.schedules(who["account_id"])
        if rows:
            for s in rows:
                scheduler.schedule_save(who, {"enabled": True}, s["id"])
        else:
            scheduler.schedule_save(who, {"name": "Job bot", "kind": "interval", "every_minutes": 120, "enabled": True})
        scheduler.request(who["account_id"], "manual", who["user_id"])
    else:
        for s in scheduler.schedules(who["account_id"]):
            scheduler.schedule_save(who, {"enabled": False}, s["id"])
        for c in ct._all("SELECT id FROM cycles WHERE account_id=? AND status IN ('queued','running')", who["account_id"]):
            scheduler.stop(who["account_id"], c["id"])
    return bot_state(who["account_id"])


# --- overview, queue, applications ---

def overview(who):
    acc = ct.account(who["account_id"])
    con = db.connect()
    try:
        one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
        today = dt.date.today().isoformat()
        counts = {
            "jobs_found_today": one("SELECT COUNT(*) FROM jobs WHERE found_at >= ?", today),
            "matched_today": one("SELECT COUNT(*) FROM jobs WHERE matched_on = ?", today),
            "waiting_for_you": one("SELECT COUNT(*) FROM jobs WHERE status='matched' AND send_status IS NULL AND resume_path IS NOT NULL AND emails!='[]'"),
            "approved": one("SELECT COUNT(*) FROM jobs WHERE send_status='approved'"),
            "sent_today": one("SELECT COUNT(*) FROM jobs WHERE send_status='sent' AND sent_at >= ?", today),
            "sent_total": one("SELECT COUNT(*) FROM jobs WHERE send_status='sent'"),
            "replies": one("SELECT COUNT(*) FROM jobs WHERE outcome='replied'"),
            "bounced": one("SELECT COUNT(*) FROM jobs WHERE outcome='bounced'"),
        }
        recent = [dict(r) for r in con.execute(
            "SELECT id, title, company, location, sent_at, to_email, COALESCE(outcome, '') outcome FROM jobs "
            "WHERE send_status='sent' ORDER BY sent_at DESC LIMIT 8")]
        targets_saved = _pref(con, "targets") is not None
    finally:
        con.close()
    resume_ok = profilestore.get("resume").get("name") != ct.PLACEHOLDER_NAME
    boxes = mailbox_get()
    state = bot_state(who["account_id"])
    plan = plan_of(acc)
    return {
        "account": {k: acc.get(k) for k in ("id", "name", "plan", "trial_ends_at", "status", "timezone")},
        "plan": plan, "counts": counts, "recent": recent, "bot": state,
        "checklist": [{"step": "Upload your resume", "done": resume_ok, "tab": "resume"},
                      {"step": "Choose what to target", "done": targets_saved, "tab": "targets"},
                      {"step": "Connect your mailbox", "done": bool(boxes), "tab": "mailbox"},
                      {"step": "Turn the bot on", "done": state["on"], "tab": "home"}],
    }


def _job(r):
    return {k: r[k] for k in ("id", "title", "company", "location", "region", "remote", "job_type", "track", "category", "score",
                              "url", "posted_at", "found_at", "send_status", "sent_at", "to_email", "outcome", "email_subject")
            if k in r.keys()} | {"emails": json.loads(r["emails"] or "[]")}


STAGES = {
    "review": "status='matched' AND send_status IS NULL AND resume_path IS NOT NULL AND emails!='[]'",
    "approved": "send_status='approved'",
    "matched": "status='matched' AND send_status IS NULL",
    "found": "status='new' AND score > 0",
    "skipped": "status='skipped'",
    "sent": "send_status='sent'",
}
ACADEMIC = re.compile(r"(?i)universit|college|institute of technology|\biit\b|\biisc\b|academ|school|research cent|大學|學院|研究院|中央研究院")
SEMI_GOVERNMENT = re.compile(r"(?i)財團法人|行政法人|public sector|\bpsu\b|state .{0,30}corporation|corporation of|development corporation|"
                             r"electronics corporation|digital india corporation|c-dac|keltron|stpi|\b(bel|bhel|ongc|ntpc|gail|iocl|sail|hal|"
                             r"isro|drdo|nielit|railtel|bsnl|mtnl)\b|中華電信|台灣電力|中油|台灣自來水")
GOVERNMENT = re.compile(r"(?i)ministry|government|govt|\bgov\b|department of|national informatics|municipal|council|authority|commission|"
                        r"國立|政府|市政府|縣政府|部$|署$|\bnic\b|negd|meity|e-governance")
SOURCE_NAMES = {"104": "104 Job Bank", "yourator": "Yourator", "alert-linkedin": "LinkedIn (job alert)", "alert-indeed": "Indeed (job alert)",
                "alert-naukri": "Naukri (job alert)", "alert-1111": "1111 (job alert)", "alert-518": "518 (job alert)",
                "alert-jobsdb": "JobsDB (job alert)", "alert-ziprecruiter": "ZipRecruiter (job alert)", "alert-foundit": "Foundit (job alert)",
                "alert-cake": "Cake (job alert)", "leads": "Your lead sheet", "freelancer": "Freelancer.com"}


def source_name(source):
    if source in SOURCE_NAMES:
        return SOURCE_NAMES[source]
    return re.sub(r"[-_]", " ", source).strip().title()


def sector_of(r):
    who = f"{r['company']} {r['title']}"
    if r["source"] in ("ted", "ted_awards", "uk_contracts", "uk_find_a_tender") or r["track"] in ("lead", "subcontract"):
        return "B2B lead"
    if ACADEMIC.search(who):
        return "Academic"
    if SEMI_GOVERNMENT.search(who):
        return "Semi-government"
    if GOVERNMENT.search(who) or r["source"] in GOVERNMENT_SOURCES():
        return "Government"
    return "Private"


_gov_sources = None


def GOVERNMENT_SOURCES():
    global _gov_sources
    if _gov_sources is None:
        try:
            _gov_sources = {s["name"] for s in sourcestore.load() if s["adapter"] in GROUPS["government"]}
        except Exception:
            _gov_sources = set()
    return _gov_sources


def work_type(r):
    if r["track"] in ("lead", "subcontract"):
        return "B2B lead"
    if r["track"] == "freelance":
        return "Freelance"
    return "Part time" if r["job_type"] == "part" else "Full time"


def country_of(r, countries):
    if r["region"] == "taiwan":
        return "Taiwan"
    place = (r["location"] or "").lower()
    hit = next((c for c in countries if c.lower() in place), None)
    if hit:
        return hit
    return "Remote" if r["remote"] else "Not stated"


FACETS = ("country", "work_type", "field", "sector", "source", "reason")


def queue(q):
    """Jobs at one stage, before or after sending, with every filter a candidate may want and the counts for each."""
    one = lambda k: (q.get(k) or [""])[0].strip()
    con = db.connect()
    try:
        global _gov_sources
        _gov_sources = None
        stage = one("stage") or "review"
        where, args = STAGES.get(stage, STAGES["matched"]), []
        if one("q"):
            where += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
            args += [f"%{one('q')}%"] * 3
        if one("days").isdigit():
            since = (dt.date.today() - dt.timedelta(days=int(one("days")))).isoformat()
            where += " AND COALESCE(NULLIF(posted_at, ''), substr(found_at, 1, 10)) >= ?"
            args.append(since)
        order = "sent_at DESC" if stage == "sent" else "COALESCE(NULLIF(posted_at, ''), found_at) DESC, score DESC"
        rows = con.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY {order} LIMIT 5000", args).fetchall()
        countries = sorted(sourcestore.all_countries(con), key=len, reverse=True)
        items = []
        for r in rows:
            items.append((r, {"country": country_of(r, countries), "work_type": work_type(r), "field": r["category"] or "Other",
                              "sector": sector_of(r), "source": source_name(r["source"]), "reason": r["skip_reason"] or ""}))
        wanted = {f: one(f) for f in FACETS if one(f)}
        facets = {f: {} for f in FACETS}
        for _, tags in items:   # counts for each filter, among the jobs that pass all the other filters
            for f in FACETS:
                if all(tags[g] == v for g, v in wanted.items() if g != f):
                    facets[f][tags[f]] = facets[f].get(tags[f], 0) + 1
        kept = [(r, t) for r, t in items if all(t[f] == v for f, v in wanted.items())]
        limit = min(int(one("limit") or 100), 500)
        jobs = [_job(r) | t | {"source_name": t["source"], "email_body": r["email_body"]} for r, t in kept[:limit]]
        return {"stage": stage, "total": len(kept), "shown": len(jobs), "jobs": jobs,
                "facets": {f: sorted(c.items(), key=lambda x: -x[1]) for f, c in facets.items() if c and any(k for k in c)}}
    finally:
        con.close()


def applications(q):
    con = db.connect()
    try:
        limit = min(int((q.get("limit") or [200])[0]), 2000)
        return [_job(r) for r in con.execute("SELECT * FROM jobs WHERE send_status='sent' ORDER BY sent_at DESC LIMIT ?", (limit,))]
    finally:
        con.close()


def pick(job_id):
    """The customer chose a found job by hand: it is matched now, and the next run tailors and prepares it."""
    con = db.connect()
    try:
        n = con.execute("UPDATE jobs SET status='matched', matched_on=?, skip_reason=NULL WHERE id=? AND send_status IS NULL "
                        "AND status IN ('new', 'skipped')", (dt.date.today().isoformat(), job_id)).rowcount
        con.commit()
        return bool(n)
    finally:
        con.close()


def job_detail(job_id):
    """Everything about one job, before or after sending: the post, the tailored resume and email, where it went and what came back."""
    con = db.connect()
    try:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return None
        return _job(r) | {k: r[k] for k in ("description", "email_body", "salary", "score_notes", "skip_reason", "hr_name",
                                             "followup_at", "outcome_at", "deadline") if k in r.keys()} | {"tailored": _tailored_view(r)}
    finally:
        con.close()


def _tailored_view(r):
    """The resume as it will go to this job: the master resume with this job's tailoring applied."""
    from . import render
    t = json.loads(r["tailored_json"]) if r["tailored_json"] else None
    built = render.build(r["region"], master=render.load_master(), tailored=t)
    return {"by": r["tailored_by"] or "", "ready": bool(r["resume_path"]), "headline": built["headline"], "summary": built["summary"],
            "skills": built["skills"],
            "experience": [{"title": e["title"], "company": e["company"], "bullets": e["bullets"]} for e in built["experience"]],
            "email_subject": r["email_subject"] or "", "email_body": r["email_body"] or ""}


def tailored_put(job_id, body):
    """The candidate's own tailoring of the resume and email for one job. Claims not found in the master resume
    are reported back as warnings, so nothing invented slips through unnoticed."""
    from . import render, tailor
    con = db.connect()
    try:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return None
        if r["send_status"] in ("sent", "sending"):
            raise ValueError("this application has already been sent")
        master = render.load_master()
        exp = body.get("experience") or []
        if not isinstance(exp, list) or len(exp) != len(master["experience"]):
            raise ValueError(f"experience must list bullets for each of your {len(master['experience'])} jobs, in order")
        skills = [{"label": str(g.get("label", "")).strip(), "items": [str(i).strip() for i in g.get("items", []) if str(i).strip()]}
                  for g in body.get("skills") or [] if isinstance(g, dict)]
        t = {"headline": str(body.get("headline") or "").strip(), "summary": str(body.get("summary") or "").strip(),
             "skills": [g for g in skills if g["label"] and g["items"]],
             "experience": [{"bullets": [str(b).strip() for b in (e.get("bullets") or []) if str(b).strip()]} for e in exp]}
        subject, email_body = str(body.get("email_subject") or "").strip(), str(body.get("email_body") or "").strip()
        if not subject or len(email_body) < 40:
            raise ValueError("the email needs a subject and a body")
        render.check_region_rule(render.to_html(render.build(r["region"], master=master, tailored=t)), r["region"])
        warnings = tailor.violations({**t, "email_body": email_body}, master, r)
        path = r["resume_path"] or str(render.OUTPUT / dt.date.today().isoformat() / str(job_id) /
                                      (re.sub(r"\W+", "_", master["name"]).strip("_") + "_resume.pdf"))
        con.execute("UPDATE jobs SET tailored_json=?, email_subject=?, email_body=?, tailored_by='manual', resume_path=?, "
                    "tailor_note='edited by the candidate' WHERE id=?",
                    (json.dumps(t, ensure_ascii=False), subject, email_body, path, job_id))
        con.commit()
        return {"saved": True, "warnings": warnings}
    finally:
        con.close()


def retailor(job_id):
    """Throw away this job's tailoring; the next run writes it again from the job description."""
    con = db.connect()
    try:
        n = con.execute("UPDATE jobs SET resume_path=NULL, tailored_json=NULL, tailored_by=NULL, email_subject=NULL, email_body=NULL, "
                        "send_status=NULL, status='matched' WHERE id=? AND COALESCE(send_status, '') NOT IN ('sent', 'sending')",
                        (job_id,)).rowcount
        con.commit()
        return bool(n)
    finally:
        con.close()


def decide(job_id, approve):
    con = db.connect()
    try:
        if approve:
            n = con.execute("UPDATE jobs SET send_status='approved' WHERE id=? AND send_status IS NULL AND emails!='[]' "
                            "AND (resume_path IS NOT NULL OR job_type='lead')", (job_id,)).rowcount
        else:
            n = con.execute("UPDATE jobs SET send_status='rejected' WHERE id=? AND COALESCE(send_status,'') NOT IN ('sent','sending')",
                            (job_id,)).rowcount
        con.commit()
        return bool(n)
    finally:
        con.close()


# --- support and feedback ---

TICKET_KINDS = ("support", "feedback")
TICKET_STATUSES = ("open", "in progress", "answered", "closed")
SUPPORT_CATEGORIES = ("account and sign-in", "billing and plans", "resume", "mailbox and sending", "jobs and matching", "bug", "other")


def ticket_create(who, body):
    kind = body.get("kind")
    if kind not in TICKET_KINDS:
        raise ValueError("kind must be support or feedback")
    if ct._too_many(("ticket", who["account_id"])):
        raise ValueError("you have sent several messages in a short time; please wait a little")
    subject, message = (body.get("subject") or "").strip()[:150], (body.get("message") or "").strip()[:5000]
    if len(message) < 5:
        raise ValueError("please write a message")
    rating = body.get("rating")
    if kind == "feedback":
        if not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError("choose a rating from 1 to 5")
        subject = subject or f"Feedback ({rating}/5)"
    else:
        rating = None
        if not subject:
            raise ValueError("a subject is needed")
    category = body.get("category") if body.get("category") in SUPPORT_CATEGORIES else "other"
    _, ticket_id = ct._write("INSERT INTO tickets(account_id, user_id, kind, category, subject, message, rating, created_at) VALUES(?,?,?,?,?,?,?,?)",
                             who["account_id"], who["user_id"], kind, category, subject, message, rating, ct.now())
    with ct._failures_lock:
        ct._failures.setdefault(("ticket", who["account_id"]), []).append(dt.datetime.now().timestamp())
    return ct._one("SELECT * FROM tickets WHERE id=?", ticket_id)


def tickets_mine(who):
    return ct._all("SELECT * FROM tickets WHERE account_id=? ORDER BY id DESC LIMIT 200", who["account_id"])


def tickets_all(q):
    kind, status = (q.get("kind") or [None])[0], (q.get("status") or [None])[0]
    sql, args = ("SELECT t.*, a.name account_name, u.email user_email FROM tickets t JOIN accounts a ON a.id=t.account_id "
                 "LEFT JOIN users u ON u.id=t.user_id WHERE 1=1"), []
    if kind:
        sql += " AND t.kind=?"
        args.append(kind)
    if status:
        sql += " AND t.status=?"
        args.append(status)
    return ct._all(sql + " ORDER BY t.id DESC LIMIT 500", *args)


def ticket_update(ticket_id, body):
    if "status" in body and body["status"] not in TICKET_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(TICKET_STATUSES)}")
    reply = (body.get("reply") or "").strip()[:5000] or None
    n, _ = ct._write("UPDATE tickets SET status=COALESCE(?, status), reply=COALESCE(?, reply), "
                     "replied_at=CASE WHEN ? IS NOT NULL THEN ? ELSE replied_at END, updated_at=? WHERE id=?",
                     body.get("status") or ("answered" if reply else None), reply, reply, ct.now(), ct.now(), ticket_id)
    return ct._one("SELECT * FROM tickets WHERE id=?", ticket_id) if n else None


# --- billing ---

def billing(who):
    from . import payments
    acc = ct.account(who["account_id"])
    return {"plan": plan_of(acc), "market": acc["market"], "paid_until": acc.get("paid_until"), "trial_ends_at": acc.get("trial_ends_at"),
            "problem": ct.subscription_problem(acc), "plans": plans(market=acc["market"]),
            "gateways": [{k: g[k] for k in ("code", "name", "test_mode")} for g in payments.gateways(acc["market"])],
            "payments": payments.history(acc["id"], 50)}


# ---------- operator: the whole service ----------

def operator_overview():
    from . import use_home
    out = []
    for acc in ct.accounts():
        row = {k: acc.get(k) for k in ("id", "name", "slug", "plan", "market", "status", "created_at", "trial_ends_at", "paid_until", "contact_email", "users")}
        row["plan_title"], row["price_month"], row["currency"] = plan_of(acc)["title"], plan_of(acc)["price_month"], plan_of(acc)["currency"]
        try:
            with use_home(ct.home_of(acc)):
                con = db.connect()
                try:
                    today = dt.date.today().isoformat()
                    row["sent_today"] = con.execute("SELECT COUNT(*) FROM jobs WHERE send_status='sent' AND sent_at >= ?", (today,)).fetchone()[0]
                    row["sent_total"] = con.execute("SELECT COUNT(*) FROM jobs WHERE send_status='sent'").fetchone()[0]
                    row["replies"] = con.execute("SELECT COUNT(*) FROM jobs WHERE outcome='replied'").fetchone()[0]
                    row["jobs"] = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                finally:
                    con.close()
                row["resume_ready"] = profilestore.get("resume").get("name") != ct.PLACEHOLDER_NAME
                from . import access
                row["onboarded"], row["paid_active"] = bool(access._prefs("onboarded_at")["onboarded_at"]), access.paid_active(acc)
        except Exception as e:
            row["error"] = str(e)[:120]
        state = bot_state(acc["id"])
        row["bot_on"], row["last_run"] = state["on"], (state["last_run"] or {}).get("finished_at")
        out.append(row)
    enquiries = ct._one("SELECT COUNT(*) n, SUM(status='new') new FROM enquiries")
    paying = [a for a in out if a["status"] == "active" and a["price_month"] and a["plan"] != "internal"]
    revenue = {}
    for a in paying:   # monthly revenue per currency: rupees and NT dollars are never added together
        revenue[a["currency"]] = revenue.get(a["currency"], 0) + a["price_month"]
    return {"accounts": out, "markets": markets(), "totals": {
        "accounts": len(out), "active": sum(a["status"] == "active" for a in out), "paying": len(paying),
        "by_market": {m["code"]: sum(a["market"] == m["code"] for a in out) for m in markets()},
        "sent_today": sum(a.get("sent_today") or 0 for a in out), "sent_total": sum(a.get("sent_total") or 0 for a in out),
        "enquiries": enquiries["n"] or 0, "enquiries_new": enquiries["new"] or 0, "monthly_revenue": revenue,
        "tickets_open": ct._one("SELECT COUNT(*) n FROM tickets WHERE status IN ('open', 'in progress')")["n"]}}


def account_update(account_id, body):
    acc = ct.account(account_id)
    if not acc:
        return None
    if "status" in body and body["status"] not in ("active", "suspended"):
        raise ValueError("status must be active or suspended")
    plan = ct._one("SELECT * FROM plans WHERE name=?", body["plan"]) if "plan" in body else None
    if "plan" in body and not plan:
        raise ValueError("no such plan")
    ct._write("UPDATE accounts SET status=COALESCE(?, status), plan=COALESCE(?, plan), market=COALESCE(?, market), "
              "trial_ends_at=CASE WHEN ? > 0 THEN NULL ELSE trial_ends_at END WHERE id=?",
              body.get("status"), body.get("plan"), plan["market"] if plan else None, plan["price_month"] if plan else 0, account_id)
    return ct.account(account_id)


def plan_update(name, body):
    if not ct._one("SELECT 1 FROM plans WHERE name=?", name):
        return None
    fields = {k: body[k] for k in ("title", "daily_cap", "mailboxes", "price_month", "features", "active", "position") if k in body}
    for k in ("daily_cap", "mailboxes"):
        if k in fields and (not isinstance(fields[k], int) or fields[k] < 1):
            raise ValueError(f"{k} must be a whole number above 0")
    if fields:
        ct._write(f"UPDATE plans SET {', '.join(f'{k}=?' for k in fields)} WHERE name=?", *fields.values(), name)
    return ct._one("SELECT * FROM plans WHERE name=?", name)


def enquiries(q):
    status = (q.get("status") or [None])[0]
    text = (q.get("q") or [None])[0]
    sql, args = "SELECT * FROM enquiries WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if text:
        sql += " AND (name LIKE ? OR email LIKE ? OR message LIKE ? OR country LIKE ? OR company LIKE ?)"
        args += [f"%{text}%"] * 5
    return ct._all(sql + " ORDER BY id DESC LIMIT 500", *args)


def enquiry_update(enquiry_id, body, who):
    if "status" in body and body["status"] not in ENQUIRY_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(ENQUIRY_STATUSES)}")
    n, _ = ct._write("UPDATE enquiries SET status=COALESCE(?, status), note=COALESCE(?, note), handled_by=?, updated_at=? WHERE id=?",
                     body.get("status"), body.get("note"), who["user_id"], ct.now(), enquiry_id)
    return ct._one("SELECT * FROM enquiries WHERE id=?", enquiry_id) if n else None
