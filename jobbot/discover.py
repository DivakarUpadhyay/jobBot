"""Find new job sources for a region: known sites from the catalogue plus a web search,
each one test-read before it is proposed. Nothing is switched on without your approval
unless `sources.auto_approve` is true in config.yaml."""
import re
from urllib.parse import urlparse

from . import sourcestore
from .config import load_sources
from .contacts import _search
from .sources import generic

# Never proposed: they block automated reading or forbid it in their terms.
NEVER = re.compile(r"(?i)linkedin|indeed|glassdoor|facebook|instagram|twitter|x\.com|youtube|reddit|wikipedia|"
                   r"ziprecruiter|monster\.|upwork|fiverr|google\.|quora|medium\.com|tiktok|pinterest|trustpilot")
MIN_JOBS = 3


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30]


def known_urls():
    """Hosts already covered by a source, so discovery never proposes them twice."""
    hosts = set()
    for s in load_sources():
        p = s.get("params") or {}
        for value in [p.get("url"), s.get("home")] + list(p.get("feeds") or []):
            if value:
                hosts.add(urlparse(value).netloc.removeprefix("www."))
        hosts.add(str(s["name"]).lower())
    hosts |= {"104.com.tw", "remotive.com", "remoteok.com", "arbeitnow.com", "weworkremotely.com", "jobicy.com",
              "himalayas.app", "workingnomads.com", "themuse.com", "yourator.co", "freelancer.com",
              "news.ycombinator.com"}
    return hosts


def candidates(region, use_search=True, cities=()):
    """(url, where_from) pairs to try for one region, and for each of its listed cities."""
    out, seen = [], set()
    for entry in sourcestore.catalogue(region):
        if entry["url"] not in seen:
            seen.add(entry["url"]); out.append((entry["url"], "catalogue"))
    if use_search:
        queries = ([f"remote jobs board software developer RSS feed", "remote developer jobs worldwide job board"]
                   if region.lower() == "remote" else
                   [f"{region} job board software engineer jobs english", f"jobs in {region} for foreigners job site"])
        queries += [f"jobs in {city} {region} job site english" for city in cities]
        for q in queries:
            for url, _ in _search(q, n=10):
                host = urlparse(url).netloc
                if host and not NEVER.search(host) and url not in seen:
                    seen.add(url); out.append((url, "web search"))
    return out


def discover(region, cfg, use_search=True, limit=25, log=lambda *_: None):
    """Test-read candidates. Returns list of dict(name, url, region, params, jobs, origin)."""
    have, found = known_urls(), []
    cities = ((cfg.get("sources") or {}).get("cities") or {}).get(region, [])
    for url, origin in candidates(region, use_search, cities)[:limit + 3 * len(cities)]:
        host = urlparse(url).netloc.removeprefix("www.")
        if host in have:
            continue
        have.add(host)
        name = slug(host.rsplit(".", 1)[0]) + ("" if region.lower() == "remote" else "-" + slug(region)[:12])
        params, jobs = generic.detect(url, name, region, max_pages=6)
        log(host, len(jobs), params["kind"] if params else "cannot read")
        if params and len(jobs) >= MIN_JOBS:
            found.append({"name": name, "url": url, "region": region, "params": params, "jobs": jobs, "origin": origin})
    return found


def add_source(name, url, region, params, enabled, note=""):
    """Save one source read by the generic reader. Returns the name it was saved under."""
    return sourcestore.add(name, "generic", region, params, enabled=enabled, home=url, note=note)


def all_countries():
    return sourcestore.all_countries()


def ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS discovery(
                   region TEXT PRIMARY KEY, checked_at TEXT, tried INTEGER, found INTEGER, sources TEXT)""")


def next_countries(con, n, recheck_days=30):
    """Countries never checked first (in list order), then the ones checked longest ago."""
    import datetime as dt
    ensure_table(con)
    done = {r["region"]: r["checked_at"] for r in con.execute("SELECT region, checked_at FROM discovery")}
    never = [c for c in all_countries() if c not in done]
    if never:
        return never[:n]
    limit = (dt.date.today() - dt.timedelta(days=recheck_days)).isoformat()
    old = sorted((c for c in all_countries() if done[c][:10] < limit), key=lambda c: done[c])
    return old[:n]


def record(con, region, tried, names):
    from . import db
    ensure_table(con)
    import sqlite3, time
    for attempt in range(5):   # another command may be writing; wait and try again instead of failing
        try:
            con.execute("INSERT OR REPLACE INTO discovery(region, checked_at, tried, found, sources) VALUES(?,?,?,?,?)",
                        (region, db.now(), tried, len(names), ", ".join(names)))
            con.commit()
            return
        except sqlite3.OperationalError:
            con.rollback()
            time.sleep(15)
    raise


BLOCK_HINT = re.compile(r"(?i)captcha|just a moment|access denied|verify you are human|cf-chl|px-captcha|datadome|"
                        r"request blocked|unusual traffic|are you a robot")


def why_unreadable(url):
    """A plain reason a site could not be read, for the report."""
    import requests
    from .sources.base import UA
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    except requests.RequestException as e:
        return f"did not answer ({type(e).__name__})"
    if r.status_code in (401, 403, 429) or BLOCK_HINT.search(r.text[:8000]):
        return f"blocks automated reading (HTTP {r.status_code})"
    if r.status_code >= 400:
        return f"page not found or error (HTTP {r.status_code})"
    if len(re.sub(r"(?s)<script.*?</script>|<[^>]+>", "", r.text)) < 1500:
        return "content is loaded by script; needs a real browser"
    return "no feed, no known hiring system and no standard job data in the page"


def probe_all(cfg, log=lambda *_: None, workers=8, per_site_seconds=120, only_region=None, use_browser=False):
    """Test every site in the `candidates` table. Returns list of dict(region, url, state, how, jobs, name, params)."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as Timeout
    excluded = set(cfg.get("exclude_countries") or {})
    known_full = {(s.get("home") or "") for s in load_sources()}
    todo = [(region, url) for region, url in sourcestore.candidate_list()
            if region not in excluded and not (only_region and region.lower() != only_region.lower())]

    def one(item):
        region, url = item
        host = urlparse(url).netloc.removeprefix("www.")
        if url in known_full:
            return {"region": region, "url": url, "state": "already a source", "how": "", "jobs": 0}
        if NEVER.search(host):
            return {"region": region, "url": url, "state": "not used", "how": "forbids or blocks automated reading by its terms", "jobs": 0}
        name = slug(host.rsplit(".", 1)[0]) + ("" if region.lower() in ("remote", "international organisations") else "-" + slug(region)[:12])
        source_region = "Remote" if region == "International organisations" else region
        try:
            params, jobs = generic.detect(url, name, source_region, max_pages=6, use_browser=use_browser)
        except Exception as e:
            params, jobs = None, []
        if params and len(jobs) >= MIN_JOBS:
            return {"region": source_region, "url": url, "state": "readable", "how": params["kind"], "jobs": len(jobs),
                    "name": name, "params": params}
        if params:
            return {"region": region, "url": url, "state": "too few jobs", "how": f"{params['kind']}, only {len(jobs)}", "jobs": len(jobs)}
        return {"region": region, "url": url, "state": "cannot read", "how": why_unreadable(url), "jobs": 0}

    out = []
    pool = ThreadPoolExecutor(workers)
    futures = [(item, pool.submit(one, item)) for item in todo]
    for item, f in futures:
        try:
            r = f.result(timeout=per_site_seconds * (6 if use_browser else 1))
        except Timeout:
            r = {"region": item[0], "url": item[1], "state": "cannot read", "how": "took too long to answer", "jobs": 0}
        out.append(r)
        log(r)
    pool.shutdown(wait=False, cancel_futures=True)
    return out
