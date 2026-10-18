"""Job sources, candidate sites, the country list and the site catalogue, kept in the database
(tables `sources`, `candidates`, `countries`, `catalogue`).

sources.yaml and jobbot/candidates.yaml, countries.yaml, catalogue.yaml are read once, when the
tables are first created, and never again: after that the tables are the only place the bot and
the API look."""
import csv
import datetime as dt
import json
from contextlib import contextmanager

import yaml

from . import APP, db

SOURCES_FILE = APP / "sources.yaml"
CANDIDATES_FILE = APP / "jobbot" / "candidates.yaml"
COUNTRIES_FILE = APP / "jobbot" / "countries.yaml"
CATALOGUE_FILE = APP / "jobbot" / "catalogue.yaml"

TABLES = (
    """CREATE TABLE IF NOT EXISTS sources(
         name TEXT PRIMARY KEY,
         adapter TEXT NOT NULL,               -- which reader in jobbot/sources/ handles it
         region TEXT, enabled INTEGER NOT NULL DEFAULT 0, home TEXT,
         params TEXT NOT NULL DEFAULT '{}',   -- JSON, handed to the reader
         note TEXT,                           -- where the source came from
         position INTEGER, added_at TEXT, changed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS candidates(
         url TEXT PRIMARY KEY, region TEXT,
         state TEXT,                          -- readable | cannot read | too few jobs | not used | already a source; empty = not tested yet
         how TEXT,                            -- how it is read, or why it cannot be
         jobs INTEGER DEFAULT 0, source_name TEXT, added_at TEXT, checked_at TEXT)""",
)


def _exists(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates'").fetchone())


def create(con):
    """Create and fill `sources` and `candidates` (migration step; the caller commits)."""
    if _exists(con):
        return
    for sql in TABLES:
        con.execute(sql)
    _seed_sources(con)
    _seed_candidates(con)


def create_places(con):
    """Create and fill `countries` and `catalogue` from their starting lists (migration step)."""
    con.execute("""CREATE TABLE IF NOT EXISTS countries(
                     name TEXT PRIMARY KEY,
                     priority INTEGER NOT NULL DEFAULT 0,   -- 1 = checked before the others when looking for sources
                     position INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS catalogue(
                     url TEXT PRIMARY KEY, region TEXT, position INTEGER, added_at TEXT)""")
    if COUNTRIES_FILE.exists():
        data = yaml.safe_load(COUNTRIES_FILE.read_text(encoding="utf-8")) or {}
        first = data.get("first") or []
        for n, name in enumerate(first + [c for c in data.get("countries") or [] if c not in first], 1):
            con.execute("INSERT OR IGNORE INTO countries(name, priority, position) VALUES(?,?,?)", (name, int(name in first), n))
    if CATALOGUE_FILE.exists():
        n = 0
        for region, entries in (yaml.safe_load(CATALOGUE_FILE.read_text(encoding="utf-8")) or {}).items():
            for entry in entries or []:
                n += 1
                con.execute("INSERT OR IGNORE INTO catalogue(url, region, position, added_at) VALUES(?,?,?,?)",
                            (entry["url"], region, n, db.now()))


def _seed_sources(con):
    if not SOURCES_FILE.exists():
        return
    text = SOURCES_FILE.read_text(encoding="utf-8")
    notes, pending = {}, []
    for line in text.splitlines():   # the comment right above an entry says where it came from
        if line.startswith("#"):
            pending.append(line.lstrip("# ").strip())
        elif line.startswith("- name:"):
            notes[line.split(":", 1)[1].strip().strip("'\"")] = " ".join(pending)
            pending = []
        else:
            pending = []
    stamp = db.now()
    for n, s in enumerate(yaml.safe_load(text) or [], 1):
        name = str(s["name"])
        con.execute("INSERT OR IGNORE INTO sources(name, adapter, region, enabled, home, params, note, position, added_at, "
                    "changed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, s["adapter"], s.get("region"), int(bool(s.get("enabled"))), s.get("home"),
                     json.dumps(s.get("params") or {}, ensure_ascii=False), notes.get(name, ""), n, stamp, stamp))


def _seed_candidates(con):
    stamp = db.now()
    if CANDIDATES_FILE.exists():
        for region, urls in (yaml.safe_load(CANDIDATES_FILE.read_text(encoding="utf-8")) or {}).items():
            for url in urls or []:
                con.execute("INSERT OR IGNORE INTO candidates(url, region, added_at) VALUES(?,?,?)", (url, region, stamp))
    # Results of the tests already run, oldest report first so a later test of the same site wins.
    for path in sorted((APP / "reports").glob("source-probe*.csv"), key=lambda p: p.stat().st_mtime):
        when = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                con.execute("INSERT OR IGNORE INTO candidates(url, region, added_at) VALUES(?,?,?)",
                            (row["Site"], row["Country / region"], stamp))
                _record(con, row["Site"], row["Result"], row["How it is read / why not"],
                        int(row["Jobs in the test read"] or 0), row["Source name"], when)


def _record(con, url, state, how, jobs, source_name, when):
    if state == "already a source":   # nothing was tested, so what the last real test found is kept
        con.execute("UPDATE candidates SET state=COALESCE(NULLIF(state, ''), ?), checked_at=? WHERE url=?", (state, when, url))
    else:
        con.execute("UPDATE candidates SET state=?, how=?, jobs=?, source_name=COALESCE(NULLIF(?, ''), source_name), "
                    "checked_at=? WHERE url=?", (state, how, jobs, source_name or "", when, url))


@contextmanager
def _db(con):
    """Use the caller's connection, or open one for this call and close it afterwards."""
    own = con is None
    con = con or db.connect()
    try:
        yield con
    finally:
        if own:
            con.close()


def _source(r):
    return {"name": r["name"], "adapter": r["adapter"], "region": r["region"] or "", "enabled": bool(r["enabled"]),
            "home": r["home"], "params": json.loads(r["params"] or "{}"), "note": r["note"] or "",
            "added_at": r["added_at"], "changed_at": r["changed_at"]}


def _generic_params(name, region, params):
    """A generic source labels its jobs with params.name, so that always follows the source's own name."""
    return {"name": name, "region": region, **{k: v for k, v in (params or {}).items() if k not in ("name", "region")}}


# ---------- sources ----------

def load(con=None):
    with _db(con) as c:
        return [_source(r) for r in c.execute("SELECT * FROM sources ORDER BY position, name")]


def get(name, con=None):
    with _db(con) as c:
        r = c.execute("SELECT * FROM sources WHERE name=?", (str(name),)).fetchone()
        return _source(r) if r else None


def add(name, adapter, region, params=None, enabled=False, home=None, note="", con=None):
    """Insert one source. A name already taken gets -2, -3 and so on. Returns the name used."""
    with _db(con) as c:
        taken = {r["name"] for r in c.execute("SELECT name FROM sources")}
        base, n = str(name), 2
        name = base
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        if adapter == "generic":
            params = _generic_params(name, region, params)
        stamp = db.now()
        c.execute("INSERT INTO sources(name, adapter, region, enabled, home, params, note, position, added_at, changed_at) "
                  "VALUES(?,?,?,?,?,?,?,(SELECT COALESCE(MAX(position), 0) + 1 FROM sources),?,?)",
                  (name, adapter, region, int(bool(enabled)), home, json.dumps(params or {}, ensure_ascii=False), note,
                   stamp, stamp))
        c.commit()
        return name


def update(name, changes, con=None):
    """Change region, enabled, home, params, note or adapter of one source. Returns False if there is no such source."""
    with _db(con) as c:
        old = get(name, c)
        if not old:
            return False
        new = {**old, **{k: v for k, v in changes.items() if k in ("region", "enabled", "home", "params", "note", "adapter")}}
        if new["adapter"] == "generic":
            new["params"] = _generic_params(old["name"], new["region"], new["params"])
        c.execute("UPDATE sources SET adapter=?, region=?, enabled=?, home=?, params=?, note=?, changed_at=? WHERE name=?",
                  (new["adapter"], new["region"], int(bool(new["enabled"])), new["home"],
                   json.dumps(new["params"] or {}, ensure_ascii=False), new["note"], db.now(), old["name"]))
        c.commit()
        return True


def set_enabled(name, enabled, con=None):
    with _db(con) as c:
        n = c.execute("UPDATE sources SET enabled=?, changed_at=? WHERE name=?", (int(bool(enabled)), db.now(), str(name))).rowcount
        c.commit()
        return bool(n)


def set_all_enabled(enabled, con=None):
    """Switch every source on or off. Returns the names whose state changed."""
    with _db(con) as c:
        names = [r["name"] for r in c.execute("SELECT name FROM sources WHERE enabled!=?", (int(bool(enabled)),))]
        c.execute("UPDATE sources SET enabled=?, changed_at=? WHERE enabled!=?", (int(bool(enabled)), db.now(), int(bool(enabled))))
        c.commit()
        return names


def remove(name, con=None):
    """Delete an added source. Returns 'removed', 'built-in' (those can only be switched off) or 'missing'."""
    with _db(con) as c:
        src = get(name, c)
        if not src:
            return "missing"
        if src["adapter"] != "generic":
            return "built-in"
        c.execute("DELETE FROM sources WHERE name=?", (src["name"],))
        c.commit()
        return "removed"


# ---------- candidate sites ----------

def candidates(con=None, state=None, region=None, q=None):
    """Every site on the list to try, with its last test result and the source it became, if any."""
    with _db(con) as c:
        srcs = load(c)
        by_name, by_home = {s["name"]: s for s in srcs}, {s["home"]: s for s in srcs if s["home"]}
        where, args = ["1=1"], []
        if state == "untested":
            where.append("COALESCE(state, '')=''")
        elif state:
            where.append("state=?"); args.append(state)
        if region:
            where.append("lower(region)=lower(?)"); args.append(region)
        if q:
            where.append("url LIKE ?"); args.append(f"%{q}%")
        out = []
        for r in c.execute("SELECT * FROM candidates WHERE " + " AND ".join(where) + " ORDER BY region, rowid", args):
            src = by_name.get(r["source_name"] or "") or by_home.get(r["url"])
            out.append({"url": r["url"], "region": r["region"], "state": r["state"] or "not tested yet", "how": r["how"] or "",
                        "jobs_in_test": r["jobs"] or 0, "checked_at": r["checked_at"], "added_at": r["added_at"],
                        "source": src["name"] if src else None, "source_enabled": src["enabled"] if src else None})
        return out


def candidate_list(con=None):
    """(region, url) pairs in the order they were added."""
    with _db(con) as c:
        return [(r["region"], r["url"]) for r in c.execute("SELECT region, url FROM candidates ORDER BY rowid")]


def add_candidate(url, region, con=None):
    """Put one more site on the list to try. Returns False when it is already there."""
    with _db(con) as c:
        n = c.execute("INSERT OR IGNORE INTO candidates(url, region, added_at) VALUES(?,?,?)", (url, region, db.now())).rowcount
        c.commit()
        return bool(n)


def record_probe(results, con=None):
    """Save what a test run found for each candidate site."""
    with _db(con) as c:
        stamp = db.now()
        for r in results:
            _record(c, r["url"], r["state"], r["how"], r["jobs"], r.get("name", ""), stamp)
        c.commit()


# ---------- countries and the catalogue of known sites ----------

def all_countries(con=None):
    """Every country and region, the priority ones first, each group in its listed order."""
    with _db(con) as c:
        return [r["name"] for r in c.execute("SELECT name FROM countries ORDER BY priority DESC, position")]


def catalogue(region=None, con=None):
    """Known job sites, tried first when looking for sources in a region."""
    with _db(con) as c:
        where, args = ("WHERE lower(region)=lower(?)", (region,)) if region else ("", ())
        return [{"url": r["url"], "region": r["region"], "added_at": r["added_at"]}
                for r in c.execute(f"SELECT * FROM catalogue {where} ORDER BY position, rowid", args)]


def add_catalogue(url, region, con=None):
    """Returns False when the site is already in the catalogue."""
    with _db(con) as c:
        n = c.execute("INSERT OR IGNORE INTO catalogue(url, region, position, added_at) VALUES(?,?,"
                      "(SELECT COALESCE(MAX(position), 0) + 1 FROM catalogue),?)", (url, region, db.now())).rowcount
        c.commit()
        return bool(n)


def remove_catalogue(url, con=None):
    with _db(con) as c:
        n = c.execute("DELETE FROM catalogue WHERE url=?", (url,)).rowcount
        c.commit()
        return bool(n)
