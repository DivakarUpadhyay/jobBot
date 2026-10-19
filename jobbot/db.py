"""The one database of the whole service (DATA/jobbot.db).

Everything lives in this single SQLite file: the shared pool of job postings, every account's own data, and the
control tables (accounts, users, plans, payments, ...; see control.py).

    shared       postings (one row per job post, scraped once for everyone), runs, sources, candidates,
                 catalogue, countries, discovery, companies
    per account  applications (one account's verdict on a posting: score, status, tailored resume and email,
                 send state), acct_settings, acct_documents, acct_prefs, acct_process_log, acct_meta

The rest of the program keeps using the table names it always used (`jobs`, `settings`, `documents`,
`portal_prefs`, `process_log`, `meta`). On every connection these are views over the tables above, limited to
the account being served, with triggers that write back to the right table. So no query can see or change
another account's rows, and the code above this module did not have to change.

`rowcount` and `lastrowid` do not work through a view's triggers in SQLite, so the triggers count the rows they
change in temp.ctr and `Con.execute` reports those numbers. A new column belongs in POSTING_COLS (a fact about
the post, shared) or APP_COLS (one account's own), and in a new step in SCHEMA_STEPS.
"""
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path

from . import DATA, home

DB_FILE = DATA / "jobbot.db"

# Facts about a job post, the same for every account (shared through `postings`).
POSTING_COLS = ["title", "company", "location", "region", "remote", "url", "description", "tags", "salary", "posted_at",
                "emails", "hr_name", "enriched", "email_source", "contact_checked", "deadline", "company_url"]
# One account's own view of a post (kept in `applications`).
APP_COLS = ["job_type", "track", "category", "score", "score_notes", "status", "skip_reason", "dedupe_key", "found_at",
            "matched_on", "resume_path", "email_subject", "email_body", "tailored_by", "tailor_note", "tailored_json",
            "send_status", "sender", "to_email", "sent_at", "send_error", "outcome", "outcome_at", "cc_email", "auto_ack",
            "interview_prep", "interview_prep_at", "followup_at"]
DEFAULTS = {"enriched": "0", "contact_checked": "0", "score": "0", "status": "'new'", "auto_ack": "0",
            "found_at": "strftime('%Y-%m-%dT%H:%M:%S','now','localtime')"}

# Per-account tables: view name -> (table, key columns that identify a row within the account, other columns).
ACCOUNT_TABLES = {
    "settings": ("acct_settings", ["key"], ["value", "changed_at", "default_value", "description"]),
    "documents": ("acct_documents", ["name"], ["format", "content", "changed_at"]),
    "portal_prefs": ("acct_prefs", ["key"], ["value", "changed_at"]),
    "meta": ("acct_meta", ["key"], ["value"]),
    "process_log": ("acct_process_log", ["id"], ["iteration", "step", "started_at", "finished_at", "ok", "note"]),
}
VIEWS = ("jobs",) + tuple(ACCOUNT_TABLES)
_NAMES = "|".join(VIEWS)
_DML = re.compile(rf"^\s*(?:(?:INSERT|REPLACE)(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+(?:{_NAMES})(?!\w)", re.I)


def _schema_1(con):
    """The single-database structure (accounts' tables keyed by account_id)."""
    posting_defs = ", ".join(f"{c} {'INTEGER DEFAULT 0' if c in ('remote', 'enriched', 'contact_checked') else 'TEXT'}" for c in POSTING_COLS)
    app_defs = ", ".join(f"{c} {'INTEGER DEFAULT 0' if c in ('score', 'auto_ack') else 'TEXT'}" for c in APP_COLS)
    script = f"""
        CREATE TABLE IF NOT EXISTS postings(id INTEGER PRIMARY KEY, source TEXT, ext_id TEXT, kind TEXT, first_seen TEXT,
            {posting_defs}, UNIQUE(source, ext_id));
        CREATE INDEX IF NOT EXISTS ix_postings_seen ON postings(first_seen);
        CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, posting_id INTEGER NOT NULL,
            {app_defs}, UNIQUE(account_id, posting_id));
        CREATE INDEX IF NOT EXISTS ix_app_status ON applications(account_id, status);
        CREATE INDEX IF NOT EXISTS ix_app_dedupe ON applications(account_id, dedupe_key);
        CREATE INDEX IF NOT EXISTS ix_app_send ON applications(account_id, send_status);
        CREATE INDEX IF NOT EXISTS ix_app_posting ON applications(posting_id);
        -- last line of defence against duplicates: one account never emails the same address twice
        CREATE UNIQUE INDEX IF NOT EXISTS ux_app_one_email ON applications(account_id, lower(to_email))
            WHERE to_email IS NOT NULL AND send_status IN ('sending', 'sent');
        CREATE TABLE IF NOT EXISTS acct_settings(account_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, changed_at TEXT,
            default_value TEXT, description TEXT, PRIMARY KEY(account_id, key));
        CREATE TABLE IF NOT EXISTS acct_documents(account_id INTEGER NOT NULL, name TEXT NOT NULL, format TEXT NOT NULL,
            content TEXT NOT NULL, changed_at TEXT, PRIMARY KEY(account_id, name));
        CREATE TABLE IF NOT EXISTS acct_prefs(account_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, changed_at TEXT,
            PRIMARY KEY(account_id, key));
        CREATE TABLE IF NOT EXISTS acct_meta(account_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY(account_id, key));
        CREATE TABLE IF NOT EXISTS acct_process_log(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, iteration TEXT, step TEXT,
            started_at TEXT, finished_at TEXT, ok INTEGER, note TEXT);
        CREATE INDEX IF NOT EXISTS ix_process_log ON acct_process_log(account_id, id);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started_at TEXT, source TEXT, fetched INTEGER, added INTEGER, error TEXT);
    """
    for statement in script.split(";"):   # one by one: executescript would commit the caller's transaction
        if statement.strip():
            con.execute(statement)
    from . import companies, prospects, sourcestore
    sourcestore.create(con)          # shared: the job sources and candidate sites
    sourcestore.create_places(con)   # shared: countries and the catalogue of sites
    companies.create(con)            # shared: listed companies (the owner's prospect lists)
    prospects.create(con)


def _schema_2(con):
    """2026-10-19: companies gain country, CEO / chairman names and a business inbox (prospects_world)."""
    from . import prospects
    prospects.create(con)


SCHEMA_STEPS = [_schema_1, _schema_2]   # append new steps; never edit or reorder one that has run


def _version(con, name):
    con.execute("CREATE TABLE IF NOT EXISTS schema_versions(name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    r = con.execute("SELECT version FROM schema_versions WHERE name=?", (name,)).fetchone()
    return r[0] if r else 0


def _ensure_schema(con):
    if _version(con, "jobs") >= len(SCHEMA_STEPS):
        return
    for n, step in enumerate(SCHEMA_STEPS):
        con.execute("BEGIN IMMEDIATE")   # a second process waits here, then sees the step is done
        try:
            if _version(con, "jobs") == n:
                step(con)
                con.execute("INSERT INTO schema_versions(name, version) VALUES('jobs', ?) "
                            "ON CONFLICT(name) DO UPDATE SET version=excluded.version", (n + 1,))
            con.commit()
        except Exception:
            con.rollback()
            raise


# ---------- which account ----------

_accounts = {}


def account_id():
    """The account being served: the one whose home folder is in use (see jobbot.home)."""
    h = Path(home()).resolve()
    if h == DATA.resolve():
        return 1
    key = str(h)
    if key not in _accounts:
        rel = os.path.relpath(h, DATA).replace("\\", "/")
        raw = sqlite3.connect(DB_FILE, timeout=60)
        try:
            r = raw.execute("SELECT id FROM accounts WHERE home=? OR home=?", (rel, "./" + rel)).fetchone()
        finally:
            raw.close()
        if not r:
            raise LookupError(f"no account has the data folder {h}")
        _accounts[key] = r[0]
    return _accounts[key]


# ---------- the connection ----------

class _Counted:
    """A cursor whose rowcount and lastrowid are the ones the view's triggers counted."""

    def __init__(self, cur, rowcount, lastrowid):
        self._cur, self.rowcount, self.lastrowid = cur, rowcount, lastrowid

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)


class Con(sqlite3.Connection):
    def execute(self, sql, params=()):
        if not _DML.match(sql):
            return super().execute(sql, params)
        super().execute("UPDATE temp.ctr SET n=0, last=NULL")
        cur = super().execute(sql, params)
        n, last = super().execute("SELECT n, last FROM temp.ctr").fetchone()
        return _Counted(cur, n, last)


def _q(s):
    return s.replace("'", "''")


def _views(con, account):
    """The account's own tables under the names the program uses (temporary: this connection only)."""
    con.executescript(f"""
        CREATE TEMP TABLE ctx(account_id INTEGER NOT NULL); INSERT INTO temp.ctx VALUES({int(account)});
        CREATE TEMP TABLE ctr(n INTEGER, last INTEGER); INSERT INTO temp.ctr VALUES(0, NULL);""")
    acct = "(SELECT account_id FROM temp.ctx)"
    # unqualified inside triggers (SQLite's rule); ctr is a temp table, found before any table in main
    count = "UPDATE ctr SET n = n + changes(), last = CASE WHEN changes() > 0 THEN last_insert_rowid() ELSE last END;"
    count_upd = "UPDATE ctr SET n = n + changes();"
    p_sel = ", ".join(f"p.{c}" for c in POSTING_COLS)
    a_sel = ", ".join(f"a.{c}" for c in APP_COLS)
    val = lambda c: f"COALESCE(NEW.{c}, {DEFAULTS[c]})" if c in DEFAULTS else f"NEW.{c}"
    changed = " OR ".join(f"{c} IS NOT NEW.{c}" for c in POSTING_COLS)
    fill, gaps = merge_facts("NEW")
    script = [f"""
        CREATE TEMP VIEW jobs AS SELECT a.id, p.source, p.ext_id, {p_sel}, {a_sel}, a.posting_id
            FROM main.applications a JOIN main.postings p ON p.id = a.posting_id WHERE a.account_id = {acct};
        CREATE TEMP TRIGGER jobs_insert INSTEAD OF INSERT ON jobs BEGIN
            INSERT OR IGNORE INTO postings(source, ext_id, kind, first_seen, {", ".join(POSTING_COLS)})
                VALUES(NEW.source, NEW.ext_id, NEW.job_type, {DEFAULTS['found_at']}, {", ".join(val(c) for c in POSTING_COLS)});
            UPDATE postings SET {fill} WHERE source IS NEW.source AND ext_id IS NEW.ext_id AND ({gaps});
            INSERT INTO applications(account_id, posting_id, {", ".join(APP_COLS)})
                VALUES({acct}, (SELECT id FROM main.postings WHERE source IS NEW.source AND ext_id IS NEW.ext_id ORDER BY id DESC LIMIT 1),
                       {", ".join(val(c) for c in APP_COLS)});
            {count}
        END;
        CREATE TEMP TRIGGER jobs_update INSTEAD OF UPDATE ON jobs BEGIN
            SELECT RAISE(ABORT, 'source and ext_id identify a shared posting and cannot be changed')
                WHERE NEW.source IS NOT OLD.source OR NEW.ext_id IS NOT OLD.ext_id;
            UPDATE postings SET {", ".join(f"{c} = NEW.{c}" for c in POSTING_COLS)}
                WHERE id = OLD.posting_id AND ({changed});
            UPDATE applications SET {", ".join(f"{c} = NEW.{c}" for c in APP_COLS)}
                WHERE id = OLD.id AND account_id = {acct};
            {count_upd}
        END;
        CREATE TEMP TRIGGER jobs_delete INSTEAD OF DELETE ON jobs BEGIN
            DELETE FROM applications WHERE id = OLD.id AND account_id = {acct};
            {count_upd}
        END;"""]
    for view, (table, keys, cols) in ACCOUNT_TABLES.items():
        allc = keys + cols
        where = " AND ".join(f"{k} = OLD.{k}" for k in keys)
        ins_cols = [c for c in allc if c != "id"]
        script.append(f"""
        CREATE TEMP VIEW {view} AS SELECT {", ".join(allc)} FROM main.{table} WHERE account_id = {acct};
        CREATE TEMP TRIGGER {view}_insert INSTEAD OF INSERT ON {view} BEGIN
            INSERT INTO {table}(account_id, {", ".join(ins_cols)}) VALUES({acct}, {", ".join(f"NEW.{c}" for c in ins_cols)});
            {count}
        END;
        CREATE TEMP TRIGGER {view}_update INSTEAD OF UPDATE ON {view} BEGIN
            UPDATE {table} SET {", ".join(f"{c} = NEW.{c}" for c in cols)} WHERE account_id = {acct} AND {where};
            {count_upd}
        END;
        CREATE TEMP TRIGGER {view}_delete INSTEAD OF DELETE ON {view} BEGIN
            DELETE FROM {table} WHERE account_id = {acct} AND {where};
            {count_upd}
        END;""")
    con.executescript("\n".join(script))


def connect(account=None):
    """A connection that sees the shared tables and one account's own rows (by default the account being served)."""
    con = sqlite3.connect(DB_FILE, timeout=60, factory=Con)   # wait, do not fail, while another process is writing
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")                     # reports can read while a long step is running
    con.execute("PRAGMA busy_timeout=60000")
    _ensure_schema(con)
    from . import control
    control.ensure(con)   # the control tables live in this same file; the job code reads some of them (suppressions)
    account = account or account_id()
    _views(con, account)
    _init_account(con)
    return con


def _init_account(con):
    """An account's first connection: its settings and documents from the starting lists."""
    if con.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
        return
    from . import config, profilestore
    config.sync_defaults(con)
    profilestore.create(con)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('initialized', ?)", (now(),))
    con.commit()


def new_postings(con, since):
    """Postings found since `since` that this account has not judged yet (the shared pool)."""
    return con.execute("SELECT * FROM main.postings p WHERE p.first_seen >= ? AND NOT EXISTS "
                       "(SELECT 1 FROM main.applications a WHERE a.posting_id = p.id AND a.account_id = (SELECT account_id FROM temp.ctx)) "
                       "ORDER BY p.id", (since,)).fetchall()


def merge_facts(new):
    """SQL that improves a posting from another copy of the same post (`new`): empty facts are filled in and the
    longer description wins. Returns (SET list, WHERE condition that something would change)."""
    sets, conds = [], []
    for c in POSTING_COLS:
        if c == "description":
            sets.append(f"description = CASE WHEN length(COALESCE({new}.description, '')) > length(COALESCE(description, '')) "
                        f"THEN {new}.description ELSE description END")
            conds.append(f"length(COALESCE({new}.description, '')) > length(COALESCE(description, ''))")
        elif c in ("remote", "enriched", "contact_checked"):
            sets.append(f"{c} = MAX(COALESCE({c}, 0), COALESCE({new}.{c}, 0))")
            conds.append(f"COALESCE({new}.{c}, 0) > COALESCE({c}, 0)")
        else:
            empty = f"COALESCE({c}, '') IN ('', '[]')"
            sets.append(f"{c} = CASE WHEN {empty} THEN {new}.{c} ELSE {c} END")
            conds.append(f"({empty} AND COALESCE({new}.{c}, '') NOT IN ('', '[]'))")
    return ", ".join(sets), " OR ".join(conds)


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


def insert_job(con, job, verdict):
    """Insert a scraped job with its filter verdict. Returns True if it was new."""
    cur = con.execute(
        """INSERT OR IGNORE INTO jobs(source, ext_id, title, company, location, region, remote, job_type, track, category,
           url, description, tags, salary, posted_at, emails, score, score_notes, status, skip_reason,
           dedupe_key, found_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job.source, str(job.ext_id), job.title, job.company, job.location, job.region, int(job.remote),
         job.job_type, verdict["track"], verdict["category"], job.url, job.description, json.dumps(job.tags, ensure_ascii=False), job.salary,
         job.posted_at, json.dumps(verdict["emails"]), verdict["score"], verdict["notes"],
         "skipped" if verdict["skip"] else "new", verdict["skip"], verdict["dedupe_key"], now()))
    return cur.rowcount == 1


def log_run(con, source, fetched, added, error=None):
    con.execute("INSERT INTO runs(started_at, source, fetched, added, error) VALUES(?,?,?,?,?)",
                (now(), source, fetched, added, error))
