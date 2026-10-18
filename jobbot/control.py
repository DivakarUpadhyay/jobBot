"""The control tables: who the customers are and what is due to run.

They hold accounts, users, sign-in tokens and API keys, schedules, the queue of runs, the audit log, plans,
payments, enquiries and tickets. They live in the service's one database (DATA/jobbot.db) beside the shared
job postings and every account's own rows (see db.py); this module opens that file without the per-account views.

Times in this database are UTC, written as 2026-10-12T19:30:00+00:00."""
import datetime as dt
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from . import DATA, use_home

CONTROL_FILE = DATA / "jobbot.db"   # the one database of the service (db.py); these tables sit beside the jobs
ROLES = ("viewer", "member", "owner")          # each may do everything the one before it may
SESSION_DAYS = 14
MIN_PASSWORD = 10
PLACEHOLDER_NAME = "Your Name"                 # the name in the empty resume a new account starts with

# One string per step, statements separated by ";" (so no ";" inside a comment).
STEPS = [
    """CREATE TABLE accounts(
         id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
         home TEXT NOT NULL,                    -- folder with the account's data, relative to the data folder
         timezone TEXT NOT NULL DEFAULT 'UTC',  -- schedules are set in the customer's own time
         status TEXT NOT NULL DEFAULT 'active', -- active | suspended
         created_at TEXT);
       CREATE TABLE users(
         id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
         email TEXT UNIQUE NOT NULL, name TEXT, password_hash TEXT NOT NULL,
         role TEXT NOT NULL DEFAULT 'owner',    -- viewer | member | owner, within the account
         operator INTEGER NOT NULL DEFAULT 0,   -- 1 = runs the service: may create and list accounts
         active INTEGER NOT NULL DEFAULT 1, created_at TEXT, last_login_at TEXT);
       CREATE TABLE tokens(
         id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
         kind TEXT NOT NULL,                    -- session (from signing in) | key (an API key)
         name TEXT, token_hash TEXT UNIQUE NOT NULL,
         hint TEXT,                             -- first characters, to tell keys apart (the key itself is never stored)
         created_at TEXT, expires_at TEXT, last_used_at TEXT, revoked_at TEXT);
       CREATE TABLE schedules(
         id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), name TEXT,
         kind TEXT NOT NULL,                    -- daily (at a time, on chosen weekdays) | interval (every N minutes)
         at TEXT, days TEXT, every_minutes INTEGER,
         enabled INTEGER NOT NULL DEFAULT 1, next_run_at TEXT, last_run_at TEXT, created_at TEXT, created_by INTEGER);
       CREATE TABLE cycles(
         id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), schedule_id INTEGER,
         trigger TEXT NOT NULL,                 -- schedule | manual
         status TEXT NOT NULL,                  -- queued | running | done | failed | stopped | skipped
         requested_at TEXT, requested_by INTEGER, started_at TEXT, finished_at TEXT,
         pid INTEGER, exit_code INTEGER, note TEXT, log_file TEXT);
       CREATE INDEX ix_cycles_open ON cycles(status, account_id);
       CREATE TABLE audit(
         id INTEGER PRIMARY KEY, at TEXT, account_id INTEGER, user_id INTEGER, action TEXT, result INTEGER, detail TEXT);""",
    # Selling the service: plans with their limits, the plan of each account, and enquiries from anyone.
    """CREATE TABLE plans(
         name TEXT PRIMARY KEY, title TEXT NOT NULL,
         daily_cap INTEGER NOT NULL,            -- most emails a day the bot may send for the account
         mailboxes INTEGER NOT NULL,            -- how many sending mailboxes the customer may connect
         price_month REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
         features TEXT, position INTEGER, active INTEGER NOT NULL DEFAULT 1);
       INSERT INTO plans VALUES('trial', 'Free trial', 10, 1, 0, 'USD', '14 days. Every email approved by you before it goes.', 1, 1);
       INSERT INTO plans VALUES('starter', 'Starter', 30, 1, 19, 'USD', 'Jobs worldwide, private and government, full and part time.', 2, 1);
       INSERT INTO plans VALUES('pro', 'Pro', 80, 2, 49, 'USD', 'Adds freelance projects, B2B leads and automatic sending.', 3, 1);
       INSERT INTO plans VALUES('agency', 'Agency', 150, 5, 149, 'USD', 'Several mailboxes and priority support.', 4, 1);
       INSERT INTO plans VALUES('internal', 'Internal', 450, 10, 0, 'USD', 'The service owner''s own account.', 9, 0);
       ALTER TABLE accounts ADD COLUMN plan TEXT NOT NULL DEFAULT 'trial';
       ALTER TABLE accounts ADD COLUMN trial_ends_at TEXT;
       ALTER TABLE accounts ADD COLUMN contact_email TEXT;
       UPDATE accounts SET plan = 'internal' WHERE id = 1;
       CREATE TABLE enquiries(
         id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
         name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT, country TEXT, company TEXT,
         topic TEXT NOT NULL,                   -- job seeker | business | partnership | support | other
         message TEXT NOT NULL,
         status TEXT NOT NULL DEFAULT 'new',    -- new | in progress | answered | closed | spam
         note TEXT, handled_by INTEGER, updated_at TEXT, client TEXT);
       CREATE INDEX ix_enquiries_status ON enquiries(status, created_at)""",
    # Plans per market, priced in local money: India first (active), Taiwan prepared (switched on later in the admin panel).
    """ALTER TABLE plans ADD COLUMN market TEXT NOT NULL DEFAULT 'IN';
       ALTER TABLE accounts ADD COLUMN market TEXT NOT NULL DEFAULT 'IN';
       DELETE FROM plans WHERE name IN ('starter', 'pro', 'agency');
       UPDATE plans SET title = 'Free trial', currency = 'INR', market = 'IN', features = '14 days. 10 applications a day. You approve every email.' WHERE name = 'trial';
       INSERT INTO plans(name, title, daily_cap, mailboxes, price_month, currency, features, position, active, market) VALUES
         ('in-starter', 'Starter', 30, 1, 499, 'INR', 'Private and government jobs in India and abroad. Full and part time.', 2, 1, 'IN'),
         ('in-pro', 'Pro', 80, 2, 1499, 'INR', 'Adds freelance projects, B2B leads and automatic sending.', 3, 1, 'IN'),
         ('in-agency', 'Agency', 150, 5, 4999, 'INR', 'For placement agencies and career coaches: 5 mailboxes, priority support.', 4, 1, 'IN'),
         ('tw-trial', 'Free trial', 10, 1, 0, 'TWD', '14 days. 10 applications a day. You approve every email.', 11, 0, 'TW'),
         ('tw-starter', 'Starter', 30, 1, 490, 'TWD', 'Jobs in Taiwan and abroad, including foreigner-friendly roles.', 12, 0, 'TW'),
         ('tw-pro', 'Pro', 80, 2, 1290, 'TWD', 'Adds freelance projects, B2B leads and automatic sending.', 13, 0, 'TW'),
         ('tw-agency', 'Agency', 150, 5, 3990, 'TWD', 'For agencies and coaches: 5 mailboxes, priority support.', 14, 0, 'TW');
       UPDATE accounts SET market = 'TW' WHERE id = 1""",
    # Markets as data: the operator adds a country and its plans in the admin panel; no code change per country.
    """CREATE TABLE markets(code TEXT PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL, position INTEGER, note TEXT);
       INSERT INTO markets VALUES('IN', 'India', 'INR', 1, 'launch market');
       INSERT INTO markets VALUES('TW', 'Taiwan', 'TWD', 2, 'second market');
       INSERT INTO markets VALUES('AE', 'United Arab Emirates (Dubai)', 'AED', 3, 'planned');
       INSERT INTO markets VALUES('NP', 'Nepal', 'NPR', 4, 'planned');
       INSERT INTO markets VALUES('CN', 'China', 'CNY', 5, 'planned');
       INSERT INTO markets VALUES('JP', 'Japan', 'JPY', 6, 'planned');
       INSERT INTO markets VALUES('KR', 'South Korea', 'KRW', 7, 'planned')""",
    # Payments: gateways are rows (which markets each serves, on or off), so more can be added per country later.
    """CREATE TABLE payment_gateways(
         code TEXT PRIMARY KEY, name TEXT NOT NULL,
         kind TEXT NOT NULL,                    -- which adapter in payments.py handles it: dummy | razorpay | ecpay | stripe ...
         markets TEXT NOT NULL,                 -- market codes it serves, comma separated
         active INTEGER NOT NULL DEFAULT 1, test_mode INTEGER NOT NULL DEFAULT 1, note TEXT, position INTEGER);
       INSERT INTO payment_gateways VALUES('dummy-in', 'Test payments (India)', 'dummy', 'IN', 1, 1, 'No real money moves. For trying the full purchase flow.', 1);
       INSERT INTO payment_gateways VALUES('dummy-tw', 'Test payments (Taiwan)', 'dummy', 'TW', 1, 1, 'No real money moves. For trying the full purchase flow.', 2);
       INSERT INTO payment_gateways VALUES('razorpay', 'Razorpay (UPI, cards, net banking)', 'razorpay', 'IN', 0, 1, 'Planned. Needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env.', 3);
       INSERT INTO payment_gateways VALUES('ecpay', 'ECPay (credit card, ATM, convenience store)', 'ecpay', 'TW', 0, 1, 'Planned. Needs the ECPay merchant id and keys in .env.', 4);
       INSERT INTO payment_gateways VALUES('stripe', 'Stripe (cards)', 'stripe', 'AE,JP,KR', 0, 1, 'Planned for later markets.', 5);
       CREATE TABLE payments(
         id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
         plan TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, gateway TEXT NOT NULL,
         status TEXT NOT NULL DEFAULT 'created',   -- created | paid | failed | refunded
         reference TEXT, created_at TEXT, paid_at TEXT, period_end TEXT, note TEXT);
       CREATE INDEX ix_payments_account ON payments(account_id, created_at);
       ALTER TABLE accounts ADD COLUMN paid_until TEXT""",
    # Customer support and feedback from signed-in customers, answered in the admin panel.
    """CREATE TABLE tickets(
         id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), user_id INTEGER NOT NULL,
         kind TEXT NOT NULL,                    -- support | feedback
         category TEXT, subject TEXT NOT NULL, message TEXT NOT NULL,
         rating INTEGER,                        -- feedback: 1 to 5
         status TEXT NOT NULL DEFAULT 'open',   -- open | in progress | answered | closed
         reply TEXT, replied_at TEXT, created_at TEXT NOT NULL, updated_at TEXT);
       CREATE INDEX ix_tickets_status ON tickets(status, created_at)""",
    # Operations: alerts for the operator (ops.py), and the shared do-not-contact list every account obeys.
    """CREATE TABLE alerts(
         id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, account_id INTEGER,
         message TEXT NOT NULL, key TEXT UNIQUE,  -- one alert per key, so a problem is reported once
         emailed_at TEXT, resolved_at TEXT);
       CREATE INDEX ix_alerts_open ON alerts(resolved_at, at);
       CREATE TABLE suppressions(
         address TEXT PRIMARY KEY,              -- an email address, or @domain for a whole domain, lower case
         reason TEXT NOT NULL,                  -- unsubscribed | bounced | complaint | manual
         account_id INTEGER, added_at TEXT NOT NULL, note TEXT)""",
]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


_READY = {}   # database file -> its structure was checked by this process


def connect():
    con = sqlite3.connect(CONTROL_FILE, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    ensure(con)
    return con


def ensure(con):
    """Bring the control tables to the latest step, once per process (the check writes, so it must not wait on
    other writers every time a connection is opened). db.connect() calls this too."""
    if not _READY.get(str(CONTROL_FILE)):
        con.execute("PRAGMA journal_mode=WAL")
        if _version(con) < len(STEPS):
            _migrate(con)
        _READY[str(CONTROL_FILE)] = True


def _version(con):
    """How many of STEPS this file has (kept in schema_versions, shared with db.py's own steps)."""
    con.execute("CREATE TABLE IF NOT EXISTS schema_versions(name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    r = con.execute("SELECT version FROM schema_versions WHERE name='control'").fetchone()
    return r[0] if r else 0


def _migrate(con):
    for n, script in enumerate(STEPS):
        con.execute("BEGIN IMMEDIATE")
        try:
            if _version(con) == n:
                for statement in script.split(";"):
                    if statement.strip():
                        con.execute(statement)
                if n == 0:   # the account that existed before there were accounts: its data is the program folder itself
                    con.execute("INSERT INTO accounts(slug, name, home, timezone, created_at) VALUES('owner', 'Owner', '.', ?, ?)",
                                (_local_timezone(), now()))
                con.execute("INSERT INTO schema_versions(name, version) VALUES('control', ?) "
                            "ON CONFLICT(name) DO UPDATE SET version=excluded.version", (n + 1,))
            con.commit()
        except Exception:
            con.rollback()
            raise


def _local_timezone():
    """This computer's time zone by name (the owner's schedules were set on its clock); UTC if it cannot be told."""
    try:
        import tzlocal
        return str(tzlocal.get_localzone())
    except Exception:
        offset = -time.timezone // 3600
        return {8: "Asia/Taipei"}.get(offset, "UTC")


def _one(sql, *args):
    con = connect()
    try:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _all(sql, *args):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _write(sql, *args):
    """Run one change. Returns (rows changed, id of the new row)."""
    con = connect()
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.rowcount, cur.lastrowid
    finally:
        con.close()


# ---------- accounts ----------

def home_of(account):
    return (DATA / account["home"]).resolve()


def account(account_id):
    return _one("SELECT * FROM accounts WHERE id=?", account_id)


def accounts():
    return _all("SELECT a.*, (SELECT COUNT(*) FROM users u WHERE u.account_id=a.id) users FROM accounts a ORDER BY id")


def account_create(name, timezone="UTC"):
    """A new customer: a row here, a folder of its own, and a fresh database with the neutral starting settings."""
    from zoneinfo import ZoneInfo
    ZoneInfo(timezone)   # raises on a name that is not a time zone
    name = name.strip()
    if not name:
        raise ValueError("the account needs a name")
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "account"
    taken = {a["slug"] for a in accounts()}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    _, account_id = _write("INSERT INTO accounts(slug, name, home, timezone, created_at) VALUES(?,?,?,?,?)",
                           slug, name, f"accounts/{slug}", timezone, now())
    new = account(account_id)
    home_of(new).mkdir(parents=True, exist_ok=True)
    from . import db
    with use_home(home_of(new)):
        db.connect().close()   # creates the account's database and fills it from the starting lists
    return new


def subscription_problem(acc):
    """Why this account's plan does not allow running now (trial over, payment period over), or None."""
    if acc["id"] == 1 or acc.get("plan") == "internal":
        return None
    plan = _one("SELECT * FROM plans WHERE name=?", acc.get("plan") or "")
    stamp = now()
    if not plan:
        return "the account has no plan"
    if plan["price_month"] <= 0:   # a free account sets up its profile; the bot runs only on a paid plan
        return "choose a plan to start the bot"
    if plan["price_month"] > 0 and (not acc.get("paid_until") or acc["paid_until"] < stamp):
        return "the paid period has ended; renew the plan to continue"
    return None


def ready_to_run(acc):
    """Why the bot may not run for this account yet, or None when it may."""
    if acc["status"] != "active":
        return "the account is suspended"
    if subscription_problem(acc):
        return subscription_problem(acc)
    from . import profilestore
    with use_home(home_of(acc)):
        if profilestore.get("resume").get("name") == PLACEHOLDER_NAME:
            return "the resume has not been filled in yet"
    return None


# ---------- users and passwords ----------

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$14$8$1${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, _, _, _, salt, digest = stored.split("$")
        again = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(again.hex(), digest)
    except (ValueError, AttributeError):
        return False


def password_problem(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"the password needs at least {MIN_PASSWORD} characters"
    return None


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def user_create(account_id, email, password, role="owner", name="", operator=False):
    """Returns the new user. Raises ValueError with the reason when it cannot be created."""
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise ValueError("that is not an email address")
    if role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    if password_problem(password):
        raise ValueError(password_problem(password))
    if not account(account_id):
        raise ValueError("no such account")
    try:
        _, user_id = _write("INSERT INTO users(account_id, email, name, password_hash, role, operator, created_at) "
                            "VALUES(?,?,?,?,?,?,?)", account_id, email, name, hash_password(password), role, int(operator), now())
    except sqlite3.IntegrityError:
        raise ValueError("a user with that email already exists") from None
    return user(user_id)


def user(user_id):
    return _public(_one("SELECT * FROM users WHERE id=?", user_id))


def users(account_id):
    return [_public(u) for u in _all("SELECT * FROM users WHERE account_id=? ORDER BY id", account_id)]


def user_count():
    return _one("SELECT COUNT(*) n FROM users")["n"]


def _public(u):
    """A user without the password hash."""
    return {k: v for k, v in u.items() if k != "password_hash"} if u else None


def user_update(account_id, user_id, role=None, active=None):
    """Change a user of this account. Returns False if there is no such user in it."""
    if role is not None and role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    n, _ = _write("UPDATE users SET role=COALESCE(?, role), active=COALESCE(?, active) WHERE id=? AND account_id=?",
                  role, None if active is None else int(bool(active)), user_id, account_id)
    if n and active is not None and not active:
        _write("UPDATE tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", now(), user_id)
    return bool(n)


# ---------- signing in, sessions and API keys ----------

_failures, _failures_lock = {}, threading.Lock()
MAX_FAILURES, FAILURE_WINDOW = 5, 15 * 60


def _too_many(key):
    with _failures_lock:
        recent = [t for t in _failures.get(key, []) if time.time() - t < FAILURE_WINDOW]
        _failures[key] = recent
        return len(recent) >= MAX_FAILURES


def login(email, password, where=""):
    """Returns (token, expires_at, user), or raises PermissionError: 'wrong' or 'wait' (too many tries)."""
    email = (email or "").strip().lower()
    key = (email, where)
    if _too_many(key):
        raise PermissionError("wait")
    row = _one("SELECT * FROM users WHERE email=? AND active=1", email)
    # The check runs even for an unknown email, so the answer takes the same time either way.
    ok = check_password(password or "", row["password_hash"] if row else hash_password("-"))
    if not (row and ok):
        with _failures_lock:
            _failures.setdefault(key, []).append(time.time())
        raise PermissionError("wrong")
    with _failures_lock:
        _failures.pop(key, None)
    _write("UPDATE users SET last_login_at=? WHERE id=?", now(), row["id"])
    token, expires = token_create(row["id"], "session", "signed in", SESSION_DAYS)
    return token, expires, _public(row)


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_create(user_id, kind, name, days=None):
    """Returns (the token, when it expires). Only its hash is stored, so this is the one time it can be shown."""
    token = ("jbs_" if kind == "session" else "jbk_") + secrets.token_urlsafe(32)
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(timespec="seconds") if days else None
    _write("INSERT INTO tokens(user_id, kind, name, token_hash, hint, created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
           user_id, kind, name, _hash(token), token[:8], now(), expires)
    return token, expires


def authenticate(token):
    """Who this token belongs to: dict(user_id, account_id, email, role, operator, home, token_id), or None."""
    if not token or not token.startswith(("jbs_", "jbk_")):
        return None
    row = _one("""SELECT t.id token_id, t.expires_at, t.last_used_at, u.id user_id, u.email, u.role, u.operator,
                         a.id account_id, a.slug, a.name account_name, a.home, a.status, a.timezone
                  FROM tokens t JOIN users u ON u.id=t.user_id JOIN accounts a ON a.id=u.account_id
                  WHERE t.token_hash=? AND t.revoked_at IS NULL AND u.active=1""", _hash(token))
    if not row or row["status"] != "active" or (row["expires_at"] and row["expires_at"] < now()):
        return None
    if (row["last_used_at"] or "")[:16] != now()[:16]:   # noted once a minute at most, not on every request
        _write("UPDATE tokens SET last_used_at=? WHERE id=?", now(), row["token_id"])
    row["home"] = home_of(row)
    row["operator"] = bool(row["operator"])
    return row


def tokens(user_id, kind="key"):
    return _all("SELECT id, name, hint, created_at, expires_at, last_used_at FROM tokens "
                "WHERE user_id=? AND kind=? AND revoked_at IS NULL ORDER BY id", user_id, kind)


def token_revoke(user_id, token_id):
    n, _ = _write("UPDATE tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL", now(), token_id, user_id)
    return bool(n)


def allowed(who, method, path):
    """Why this user may not make this request, or None when they may."""
    if path.startswith(("/accounts", "/operator")):
        return None if who["operator"] else "only the service operator may do this"
    if path == "/portal/mailbox" and method == "PUT" and who["role"] != "owner":
        return "only the account owner may connect mailboxes"
    if not who["operator"] and who["account_id"] != 1:
        # Customers work only through the portal: they cannot change raw settings (limits), add sources the
        # bot would fetch, edit templates or start steps directly. Everything they need has a /portal route.
        allowed_paths = ("/me", "/logout", "/keys", "/portal/", "/schedules", "/users", "/audit")
        reads = ("/status", "/stats", "/jobs", "/sent")
        if not (path.startswith(allowed_paths) or (method == "GET" and path.startswith(reads))):
            return "not available to customer accounts; use the portal"
    if method == "GET" or path == "/logout":
        return None if not path.startswith(("/users", "/audit")) or who["role"] == "owner" else "only the account owner may see this"
    need = "owner" if path.startswith(("/config", "/profile", "/schedules", "/users", "/processes", "/audit")) else "member"
    if ROLES.index(who["role"]) < ROLES.index(need):
        return f"your role ({who['role']}) may not do this; it needs {need}"
    return None


def audit(who, action, result, detail=""):
    _write("INSERT INTO audit(at, account_id, user_id, action, result, detail) VALUES(?,?,?,?,?,?)",
           now(), who["account_id"] if who else None, who["user_id"] if who else None, action, result, detail[:300])


def audit_log(account_id, limit=100):
    return _all("SELECT a.id, a.at, u.email, a.action, a.result, a.detail FROM audit a LEFT JOIN users u ON u.id=a.user_id "
                "WHERE a.account_id=? ORDER BY a.id DESC LIMIT ?", account_id, limit)
