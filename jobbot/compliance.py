"""Checks each job source against the site's robots.txt, so the service only reads pages the site allows.

`check_all()` reads robots.txt for every switched-on source and records the answer in the shared table
source_checks: allowed, disallowed, no robots.txt (allowed), or unreachable (not judged). The scrape step skips a
source marked disallowed while the setting sources.respect_robots is on. A site's terms of service can forbid
automated reading even where robots.txt allows it; those are noted by hand in the `terms` column
(see `note_terms`), and a source marked terms='forbids' is skipped too.
"""
import json
import urllib.parse
import urllib.robotparser

import requests

from . import db

USER_AGENT = "jobbot"
# Readers that call a site's public API: the address they read, when the source's own row does not say.
API_URLS = {
    "remotive": "https://remotive.com/api/remote-jobs", "remoteok": "https://remoteok.com/api",
    "arbeitnow": "https://www.arbeitnow.com/api/job-board-api", "hn": "https://hn.algolia.com/api/v1/search",
    "jobicy": "https://jobicy.com/api/v2/remote-jobs", "himalayas": "https://himalayas.app/jobs/api",
    "workingnomads": "https://www.workingnomads.com/api/exposed_jobs/", "themuse": "https://www.themuse.com/api/public/jobs",
    "tw104": "https://www.104.com.tw/jobs/search/list", "tw104_companies": "https://www.104.com.tw/jobs/search/list",
    "yourator": "https://www.yourator.co/api/v4/jobs", "freelancer": "https://www.freelancer.com/api/projects/0.1/projects/active",
    "ted": "https://api.ted.europa.eu/v3/notices/search", "ted_awards": "https://api.ted.europa.eu/v3/notices/search",
    "uk_contracts": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search",
    "uk_find_a_tender": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
    "jobrapido": "https://www.jobrapido.com/", "instahyre": "https://www.instahyre.com/api/v1/job_search",
}


def create(con):
    con.execute("""CREATE TABLE IF NOT EXISTS source_checks(
        name TEXT PRIMARY KEY, url TEXT, robots TEXT,  -- allowed | disallowed | no robots.txt | unreachable | no address
        detail TEXT, terms TEXT,                       -- ok | forbids | unknown, set by hand after reading the terms
        terms_note TEXT, checked_at TEXT)""")


def urls_of(source):
    params = source["params"] if isinstance(source["params"], dict) else json.loads(source["params"] or "{}")
    found = [params.get("url"), *(params.get("feeds") or []), *(params.get("urls") or []), source.get("home")]
    found = [u for u in found if isinstance(u, str) and u.startswith("http")]
    return found or ([API_URLS[source["adapter"]]] if source["adapter"] in API_URLS else [])


def robots_verdict(url, session=None):
    """(verdict, detail) for one address."""
    parts = urllib.parse.urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        r = (session or requests).get(robots_url, timeout=20, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        return "unreachable", f"{robots_url}: {type(e).__name__}"
    if r.status_code in (404, 410) or (r.status_code < 500 and r.status_code >= 400 and r.status_code not in (401, 403)):
        return "no robots.txt", f"{robots_url}: {r.status_code} (everything allowed)"
    if r.status_code in (401, 403):
        return "disallowed", f"{robots_url}: {r.status_code} (robots.txt refused: treated as disallowed)"
    if r.status_code >= 500:
        return "unreachable", f"{robots_url}: {r.status_code}"
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(r.text.splitlines())
    if parser.can_fetch(USER_AGENT, url):
        return "allowed", robots_url
    return "disallowed", f"{robots_url} disallows {parts.path or '/'}"


def check_all(con=None, only=None):
    """Check every switched-on source (or the names in `only`). Returns the rows written."""
    own = con is None
    con = con or db.connect()
    try:
        create(con)
        rows = [dict(r) for r in con.execute("SELECT name, adapter, params, home FROM sources WHERE enabled=1")]
        session, out = requests.Session(), []
        for s in rows:
            if only and s["name"] not in only:
                continue
            urls = urls_of(s)
            if not urls:
                verdict, detail, url = "no address", "the reader does not name a site to check", None
            else:
                results = [(u, *robots_verdict(u, session)) for u in urls[:5]]
                worst = next((x for x in results if x[1] == "disallowed"), None) or \
                    next((x for x in results if x[1] == "unreachable"), None) or results[0]
                url, verdict, detail = worst
            con.execute("""INSERT INTO source_checks(name, url, robots, detail, checked_at) VALUES(?,?,?,?,?)
                           ON CONFLICT(name) DO UPDATE SET url=excluded.url, robots=excluded.robots, detail=excluded.detail,
                           checked_at=excluded.checked_at""", (s["name"], url, verdict, detail, db.now()))
            out.append({"name": s["name"], "url": url, "robots": verdict, "detail": detail})
        con.commit()
        return out
    finally:
        if own:
            con.close()


def note_terms(name, terms, note=""):
    if terms not in ("ok", "forbids", "unknown"):
        raise ValueError("terms must be ok, forbids or unknown")
    con = db.connect()
    try:
        create(con)
        con.execute("INSERT INTO source_checks(name, terms, terms_note, checked_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET terms=excluded.terms, terms_note=excluded.terms_note", (name, terms, note, db.now()))
        con.commit()
    finally:
        con.close()


def blocked_sources(con):
    """Names the scrape step must skip: robots.txt disallows them, or their terms forbid automated reading."""
    create(con)
    return {r[0] for r in con.execute("SELECT name FROM source_checks WHERE robots='disallowed' OR terms='forbids'")}


def report(con=None):
    own = con is None
    con = con or db.connect()
    try:
        create(con)
        return [dict(r) for r in con.execute(
            "SELECT c.*, s.enabled FROM source_checks c LEFT JOIN sources s ON s.name=c.name ORDER BY c.robots, c.name")]
    finally:
        if own:
            con.close()
