"""Companies whose openings the bot looks for by name (table `companies`).

The list is filled from the official lists of the Taiwan Stock Exchange and the Taipei Exchange
(company name in Chinese and English, website), plus any company you add yourself. The reader
`tw104.companies` takes the ones not looked at for the longest time, a few per run, your priority
companies first, so the whole list is covered in rotation without flooding 104 with requests."""
import re

import requests

from . import db
from .sourcestore import _db

LISTS = {   # exchange -> (address, field names: Chinese name, English name, website, industry code)
    "TWSE": ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", ("公司名稱", "英文簡稱", "網址", "產業別")),
    "TPEx": ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
             ("CompanyName", "Symbol", "WebAddress", "SecuritiesIndustryCode")),
}
LEGAL = re.compile(r"股份有限公司|有限公司|控股|\(股\)|-KY|＊|\*")
EN_NOISE = re.compile(r"(?i)\b(co|ltd|inc|corp|corporation|company|holdings?|group|limited|the|taiwan)\b\.?")


def create(con):
    """Create `companies` (migration step; the caller commits)."""
    con.execute("""CREATE TABLE IF NOT EXISTS companies(
                     id INTEGER PRIMARY KEY,
                     name_zh TEXT, name_en TEXT,
                     search TEXT NOT NULL UNIQUE,           -- what is typed into the job site's search box
                     website TEXT, careers_url TEXT,
                     origin TEXT,                           -- TWSE | TPEx | yours
                     industry TEXT,
                     priority INTEGER NOT NULL DEFAULT 0,   -- 1 = looked at before the others
                     enabled INTEGER NOT NULL DEFAULT 1,
                     checked_at TEXT, jobs_seen INTEGER DEFAULT 0, added_at TEXT)""")


def core(name_zh):
    """'研華股份有限公司' -> '研華': the part of a Chinese company name that job sites show."""
    return LEGAL.sub("", name_zh or "").replace("臺", "台").strip()


def key(name_en):
    """English name reduced to letters and digits, for comparing two spellings of one company."""
    return re.sub(r"[^a-z0-9]+", "", EN_NOISE.sub(" ", (name_en or "").lower()))


def add(name_en="", name_zh="", website="", origin="yours", industry="", priority=0, con=None):
    """Add one company, or fill in what is missing on one already there. Returns its search text."""
    search = core(name_zh) or name_en.strip()
    if not search:
        return None
    with _db(con) as c:
        c.execute("INSERT INTO companies(name_zh, name_en, search, website, origin, industry, priority, added_at) "
                  "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(search) DO UPDATE SET "
                  "name_en=COALESCE(NULLIF(companies.name_en, ''), excluded.name_en), "
                  "website=COALESCE(NULLIF(companies.website, ''), excluded.website), "
                  "priority=MAX(companies.priority, excluded.priority)",
                  (name_zh, name_en, search, website, origin, industry, int(priority), db.now()))
        c.commit()
    return search


def import_listed(con=None):
    """Read the two exchanges' company lists. Returns {exchange: number of companies read}."""
    counts = {}
    with _db(con) as c:
        for origin, (url, (zh, en, web, industry)) in LISTS.items():
            rows = requests.get(url, timeout=90, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}).json()
            for r in rows:
                site = (r.get(web) or "").strip()
                add(name_en=(r.get(en) or "").strip(), name_zh=(r.get(zh) or "").strip(),
                    website=site if "." in site else "", origin=origin, industry=str(r.get(industry) or ""), con=c)
            counts[origin] = len(rows)
    return counts


def find(name_en, con=None):
    """The listed company this English name means, or None. Exact match on the reduced name only."""
    want = key(name_en)
    if not want:
        return None
    with _db(con) as c:
        for r in c.execute("SELECT * FROM companies WHERE name_en != ''"):
            if key(r["name_en"]) == want:
                return dict(r)
    return None


def set_priority(search, priority=1, con=None):
    with _db(con) as c:
        n = c.execute("UPDATE companies SET priority=? WHERE search=?", (int(priority), search)).rowcount
        c.commit()
        return n


def due(limit, con=None):
    """The next companies to look at: two thirds from your priority companies, the rest from the
    others, each group oldest check first, so neither group ever waits on the other."""
    pick = lambda c, priority, n: [dict(r) for r in c.execute(
        "SELECT * FROM companies WHERE enabled=1 AND priority=? ORDER BY COALESCE(checked_at, ''), id LIMIT ?", (priority, n))]
    with _db(con) as c:
        first = pick(c, 1, limit - limit // 3)
        rest = pick(c, 0, limit - len(first))
        return first + rest if len(first) + len(rest) >= limit else pick(c, 1, limit - len(rest)) + rest


def checked(search, jobs, con=None):
    with _db(con) as c:
        c.execute("UPDATE companies SET checked_at=?, jobs_seen=? WHERE search=?", (db.now(), jobs, search))
        c.commit()


def summary(con=None):
    with _db(con) as c:
        r = c.execute("SELECT COUNT(*) n, SUM(priority) p, SUM(checked_at IS NOT NULL) done, SUM(jobs_seen > 0) hiring "
                      "FROM companies WHERE enabled=1").fetchone()
        return {"companies": r["n"], "priority": r["p"] or 0, "checked": r["done"] or 0, "with_jobs": r["hiring"] or 0}
