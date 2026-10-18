"""One-time move to the single database (2026-10-18): control.db and every account's own jobbot.db go into
DATA/jobbot.db, in the shared and per-account tables described in db.py.

    python -m jobbot db-merge          (stop the loop and the sites first)

Before anything changes, every database file is copied to DATA/backup/db-<time>/ with SQLite's backup API.
The whole move is one transaction: it either completes or leaves the files as they were. Afterwards the old
files are renamed to *.merged so nothing can open them by mistake."""
import datetime as dt
import shutil
import sqlite3

from . import DATA
from .db import APP_COLS, POSTING_COLS, _schema_1

OLD_ACCOUNT_TABLES = {   # old table -> (new table, columns)
    "settings": ("acct_settings", ["key", "value", "changed_at", "default_value", "description"]),
    "documents": ("acct_documents", ["name", "format", "content", "changed_at"]),
    "portal_prefs": ("acct_prefs", ["key", "value", "changed_at"]),
    "meta": ("acct_meta", ["key", "value"]),
    "process_log": ("acct_process_log", ["iteration", "step", "started_at", "finished_at", "ok", "note"]),
}


def _tables(con, schema="main"):
    return {r[0] for r in con.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}


def _cols(con, table, schema="main"):
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def _backup(files, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for f in files:
        src = sqlite3.connect(f)
        dst = sqlite3.connect(folder / (f.parent.name + "__" + f.name if f.parent != DATA else f.name))
        with dst:
            src.backup(dst)
        src.close(); dst.close()


def _copy_account(con, schema, account_id, keep_ids):
    """One account's jobs and own tables from the attached database `schema` into the single database."""
    have = _tables(con, schema)
    n_jobs = 0
    if "jobs" in have:
        jcols = set(_cols(con, "jobs", schema))
        pick = lambda cols: ", ".join(f"j.{c}" if c in jcols else "NULL" for c in cols)
        con.execute(f"""INSERT OR IGNORE INTO postings({"id, " if keep_ids else ""}source, ext_id, kind, first_seen, {", ".join(POSTING_COLS)})
                        SELECT {"j.id, " if keep_ids else ""}j.source, j.ext_id, j.job_type, j.found_at, {pick(POSTING_COLS)} FROM {schema}.jobs j""")
        for col in (POSTING_COLS if not keep_ids else []):   # a post shared with another account: keep the fuller facts
            if col not in jcols:
                continue
            better = (f"length(COALESCE(j.{col}, '')) > length(COALESCE(postings.{col}, ''))" if col == "description"
                      else f"COALESCE(j.{col}, 0) > COALESCE(postings.{col}, 0)" if col in ("remote", "enriched", "contact_checked")
                      else f"COALESCE(postings.{col}, '') IN ('', '[]') AND COALESCE(j.{col}, '') NOT IN ('', '[]')")
            con.execute(f"""UPDATE postings SET {col} = (SELECT j.{col} FROM {schema}.jobs j WHERE j.source IS postings.source
                            AND j.ext_id IS postings.ext_id AND {better} LIMIT 1)
                            WHERE EXISTS (SELECT 1 FROM {schema}.jobs j WHERE j.source IS postings.source AND j.ext_id IS postings.ext_id AND {better})""")
        con.execute(f"""INSERT INTO applications({"id, " if keep_ids else ""}account_id, posting_id, {", ".join(APP_COLS)})
                        SELECT {"j.id, " if keep_ids else ""}?, (SELECT p.id FROM postings p WHERE p.source IS j.source AND p.ext_id IS j.ext_id
                                                                ORDER BY p.id LIMIT 1), {pick(APP_COLS)} FROM {schema}.jobs j""", (account_id,))
        n_jobs = con.execute(f"SELECT COUNT(*) FROM {schema}.jobs").fetchone()[0]
    for old, (new, cols) in OLD_ACCOUNT_TABLES.items():
        if old not in have:
            continue
        ocols = set(_cols(con, old, schema))
        use = [c for c in cols if c in ocols]
        con.execute(f"INSERT OR REPLACE INTO {new}(account_id, {', '.join(use)}) SELECT ?, {', '.join(use)} FROM {schema}.{old}", (account_id,))
    con.execute("INSERT OR REPLACE INTO acct_meta(account_id, key, value) VALUES(?, 'initialized', ?)", (account_id, dt.datetime.now().isoformat(timespec="seconds")))
    return n_jobs


def run():
    main_file, control_file = DATA / "jobbot.db", DATA / "control.db"
    con = sqlite3.connect(main_file, timeout=60)
    if "schema_versions" in _tables(con) and con.execute("SELECT 1 FROM schema_versions WHERE name='jobs'").fetchone():
        con.close()
        return "already a single database; nothing to do"
    if not control_file.exists():
        raise SystemExit(f"{control_file} not found")
    ctl = sqlite3.connect(control_file)
    accounts = ctl.execute("SELECT id, slug, home FROM accounts ORDER BY id").fetchall()
    control_version = ctl.execute("PRAGMA user_version").fetchone()[0]
    ctl.close()
    account_files = [(a, (DATA / a[2] / "jobbot.db").resolve()) for a in accounts if a[0] != 1]
    account_files = [(a, f) for a, f in account_files if f.exists()]

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA / "backup" / f"db-{stamp}"
    _backup([main_file, control_file] + [f for _, f in account_files], backup)

    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.isolation_level = None
    # every database is attached before the transaction: SQLite cannot detach one in the middle of it
    con.execute("ATTACH DATABASE ? AS c", (str(control_file),))
    for n, (_, f) in enumerate(account_files):
        con.execute(f"ATTACH DATABASE ? AS a{n}", (str(f),))
    con.execute("BEGIN IMMEDIATE")
    report = {}
    try:
        _schema_1(con)
        report["owner jobs"] = _copy_account(con, "main", 1, keep_ids=True)
        for old in ("jobs",) + tuple(OLD_ACCOUNT_TABLES):
            con.execute(f"DROP TABLE IF EXISTS main.{old}")
        # control tables, with their indexes and the same ids
        for name, sql in con.execute("SELECT name, sql FROM c.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            con.execute(sql)
            con.execute(f"INSERT INTO main.{name} SELECT * FROM c.{name}")
        for sql, in con.execute("SELECT sql FROM c.sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall():
            con.execute(sql)
        report["control tables"] = con.execute("SELECT COUNT(*) FROM c.sqlite_master WHERE type='table'").fetchone()[0]
        for n, ((acc_id, slug, _), f) in enumerate(account_files):
            report[f"account {slug} jobs"] = _copy_account(con, f"a{n}", acc_id, keep_ids=False)
        con.execute("CREATE TABLE IF NOT EXISTS schema_versions(name TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        con.execute("INSERT OR REPLACE INTO schema_versions VALUES('jobs', 1)")
        con.execute("INSERT OR REPLACE INTO schema_versions VALUES('control', ?)", (control_version,))
        con.execute("PRAGMA user_version = 0")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.close()
        raise
    for name in ["c"] + [f"a{n}" for n in range(len(account_files))]:
        con.execute(f"DETACH DATABASE {name}")
    con.execute("VACUUM")
    con.close()
    for f in [control_file] + [f for _, f in account_files]:
        for extra in ("", "-wal", "-shm"):
            p = f.with_name(f.name + extra)
            if p.exists():
                shutil.move(str(p), str(p) + ".merged")
    report["backup"] = str(backup)
    return report
