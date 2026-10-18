"""Read jobs from a site the bot has no dedicated reader for.

Tried in this order, first one that yields jobs wins:
  feed        RSS or Atom feed (the URL itself, or one the page links to)
  greenhouse / lever / ashby / workable
              hiring systems many companies use; read through their public job feeds
  jsonld      standard job data embedded in pages (schema.org JobPosting), on the page itself
              or on the job pages it links to
Sites that need a login, show a bot check or load everything by script give nothing here.
"""
import datetime as dt
import html as htmllib
import json
import re
from urllib.parse import urljoin, urlparse

import feedparser

from .base import Job, get, is_taiwan, strip_html

ATS_PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)",
    "lever": r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)",
    "workable": r"apply\.workable\.com/([A-Za-z0-9_-]+)",
}
JOB_LINK = re.compile(r"(?i)/(jobs?|careers?|positions?|vacanc(?:y|ies)|openings?|opportunit(?:y|ies)|stellen)(/|-|\?)")
MAX_PAGES = 25      # job pages opened per list page in a test read
JOB_PAGES = 60      # job pages opened per list page on a real run
MAX_LISTINGS = 8    # list pages followed per search address on each run
PAGE_NO = re.compile(r"(?i)(?:[?&](?:page|pg|p|pageno|page_no|pagenumber|offset|start)=|/page[/-])(\d+)")


def _fetch(url):
    try:
        return get(url)
    except Exception:
        return None


def _kind(text):
    t = (text or "").lower().replace("_", " ").replace("-", " ")
    if any(w in t for w in ("part time", "contract", "freelance", "temporary", "intern")):
        return "part"
    return "full" if "full" in t else ""


def _region(location, source_region):
    return "taiwan" if source_region.lower() == "taiwan" or is_taiwan(location) else "abroad"


# ---------- feeds ----------

def read_feed(url, name, source_region):
    r = _fetch(url)
    if r is None:
        return []
    head = r.content[:600].lstrip().lower()
    if not (b"<rss" in head or b"<feed" in head or b"<?xml" in head or "xml" in r.headers.get("content-type", "")):
        return []
    jobs = []
    for e in feedparser.parse(r.content).entries:
        title = e.get("title", "")
        company, _, role = title.partition(": ")
        if not role or len(company) > 60:
            company, role = e.get("author", "") or name, title
        t = e.get("published_parsed") or e.get("updated_parsed")
        loc = e.get("region") or e.get("location") or ""
        if not e.get("link") or not role:
            continue
        jobs.append(Job(source=name, ext_id=e.get("id") or e.link, title=strip_html(role)[:200], company=strip_html(company)[:120],
                        url=e.link, description=strip_html(e.get("summary", "")), location=loc,
                        region=_region(loc, source_region), remote=source_region.lower() == "remote" or "remote" in (loc + title).lower(),
                        job_type=_kind(e.get("type", "")),
                        posted_at=f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}" if t else ""))
    return jobs


def feed_link_in(page_html, base):
    m = re.search(r'(?i)<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)', page_html)
    return urljoin(base, m.group(1)) if m else None


# ---------- hiring systems ----------

def read_ats(kind, slug, name, source_region, company=None):
    company = company or slug.replace("-", " ").replace("_", " ").title()
    jobs = []
    try:
        if kind == "greenhouse":
            data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"}).json()
            for j in data.get("jobs", []):
                loc = (j.get("location") or {}).get("name", "")
                jobs.append(Job(source=name, ext_id=j["id"], title=j["title"], company=company, url=j["absolute_url"],
                                description=strip_html(htmllib.unescape(j.get("content") or "")), location=loc,
                                region=_region(loc, source_region), remote="remote" in loc.lower(),
                                posted_at=(j.get("updated_at") or "")[:10]))
        elif kind == "lever":
            for j in get(f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"}).json():
                c = j.get("categories") or {}
                loc = c.get("location") or ""
                ms = j.get("createdAt")
                jobs.append(Job(source=name, ext_id=j["id"], title=j["text"], company=company, url=j["hostedUrl"],
                                description=j.get("descriptionPlain") or strip_html(j.get("description")), location=loc,
                                region=_region(loc, source_region),
                                remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower(),
                                job_type=_kind(c.get("commitment")),
                                posted_at=dt.date.fromtimestamp(ms / 1000).isoformat() if ms else ""))
        elif kind == "ashby":
            data = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
            for j in data.get("jobs", []):
                loc = j.get("location") or ""
                jobs.append(Job(source=name, ext_id=j["id"], title=j["title"], company=company, url=j["jobUrl"],
                                description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")), location=loc,
                                region=_region(loc, source_region), remote=bool(j.get("isRemote")) or "remote" in loc.lower(),
                                job_type=_kind(j.get("employmentType")), posted_at=(j.get("publishedAt") or "")[:10]))
        elif kind == "workable":
            data = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", {"details": "true"}).json()
            for j in data.get("jobs", []):
                loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
                jobs.append(Job(source=name, ext_id=j.get("shortcode") or j["url"], title=j["title"],
                                company=data.get("name") or company, url=j["url"], description=strip_html(j.get("description")),
                                location=loc, region=_region(loc, source_region), remote=bool(j.get("telecommuting")),
                                job_type=_kind(j.get("employment_type")), posted_at=(j.get("published_on") or "")[:10]))
    except Exception:
        return []
    return jobs


def find_ats(url, page_html=""):
    """(kind, slug) if the URL or the page points at a known hiring system."""
    for text in (url, page_html):
        for kind, pattern in ATS_PATTERNS.items():
            m = re.search(pattern, text or "")
            if m and m.group(1).lower() not in ("embed", "api", "v1", "js"):
                return kind, m.group(1)
    return None


# ---------- job data embedded in pages ----------

def _postings(node):
    if isinstance(node, list):
        for x in node:
            yield from _postings(x)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _postings(node[key])


def jsonld_jobs(page_html, page_url, name, source_region):
    jobs = []
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for p in _postings(data):
            org = p.get("hiringOrganization") or {}
            places = p.get("jobLocation") or []
            places = places if isinstance(places, list) else [places]
            locs = []
            for pl in places:
                addr = (pl or {}).get("address") or {}
                if isinstance(addr, dict):
                    country = addr.get("addressCountry")
                    country = country.get("name") if isinstance(country, dict) else country
                    locs.append(", ".join(str(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"), country) if x))
            loc = "; ".join(x for x in locs if x)
            ident = p.get("identifier")
            ident = ident.get("value") if isinstance(ident, dict) else ident
            etype = p.get("employmentType")
            etype = " ".join(etype) if isinstance(etype, list) else (etype or "")
            title = strip_html(str(p.get("title") or ""))
            if not title:
                continue
            link = p.get("url") or page_url
            jobs.append(Job(source=name, ext_id=str(ident or link), title=title[:200],
                            company=strip_html(str(org.get("name") if isinstance(org, dict) else org or ""))[:120],
                            url=link, description=strip_html(str(p.get("description") or "")), location=loc,
                            region=_region(loc, source_region),
                            remote=p.get("jobLocationType") == "TELECOMMUTE" or source_region.lower() == "remote",
                            job_type=_kind(etype), posted_at=str(p.get("datePosted") or "")[:10]))
    return jobs


def _next_page(page_html, page_url, seen):
    """The next page of a job list: the link marked rel="next", else the nearest higher page number."""
    m = re.search(r'(?is)<(?:a|link)\b[^>]*\brel=["\']next["\'][^>]*\bhref=["\']([^"\'#]+)'
                  r'|<(?:a|link)\b[^>]*\bhref=["\']([^"\'#]+)["\'][^>]*\brel=["\']next["\']', page_html)
    if m:
        url = urljoin(page_url, htmllib.unescape(m.group(1) or m.group(2)))
        return url if url not in seen else None
    host, here = urlparse(page_url).netloc, PAGE_NO.search(page_url)
    here = int(here.group(1)) if here else 1
    later = []
    for href in re.findall(r'(?i)<a[^>]+href=["\']([^"\'#]+)', page_html):
        full = urljoin(page_url, htmllib.unescape(href))
        n = PAGE_NO.search(full)
        if n and int(n.group(1)) > here and urlparse(full).netloc == host and full not in seen:
            later.append((int(n.group(1)), full))
    return min(later)[1] if later else None


def read_pages(url, name, source_region, max_pages=MAX_PAGES, listings=1):
    """Jobs on a list page and on the job pages it links to. With `listings` above 1 the list is
    followed page by page, until a page brings nothing new or that many pages have been read."""
    jobs, links, seen = {}, [], set()
    while url and len(seen) < listings:
        r = _fetch(url)
        if r is None:
            break
        seen.add(url)
        before = len(jobs) + len(links)
        for j in jsonld_jobs(r.text, r.url, name, source_region):
            jobs.setdefault(j.ext_id, j)
        host = urlparse(r.url).netloc
        for href in re.findall(r'(?i)<a[^>]+href=["\']([^"\'#]+)', r.text):
            full = urljoin(r.url, href)
            if urlparse(full).netloc == host and JOB_LINK.search(urlparse(full).path + "?") and full != r.url \
                    and full not in links and not PAGE_NO.search(full):
                links.append(full)
        if len(jobs) + len(links) == before:
            break   # a page with nothing new: the end of the list
        url = _next_page(r.text, r.url, seen | {r.url})
    for link in links[:max_pages * max(len(seen), 1)]:
        page = _fetch(link)
        if page is not None:
            for j in jsonld_jobs(page.text, page.url, name, source_region):
                jobs.setdefault(j.ext_id, j)
    return list(jobs.values())


# ---------- pages that only fill in inside a real browser ----------

import threading

_browser_lock = threading.Lock()   # one Chrome at a time
BOT_CHECK = re.compile(r"(?i)captcha|just a moment|verify you are human|access denied|unusual traffic|are you a robot")


def read_pages_browser(url, name, source_region, max_pages=8, channel="chrome"):
    """Open the listing in Chrome (Playwright), let its scripts run, then read the job data the
    same way as for ordinary pages. A site that answers with a bot check is left alone."""
    from playwright.sync_api import sync_playwright
    jobs = {}
    with _browser_lock, sync_playwright() as pw:
        browser = pw.chromium.launch(channel=channel, headless=True)
        try:
            ctx = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()

            def rendered(link):
                page.goto(link, wait_until="domcontentloaded", timeout=40000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass   # some pages never go quiet; what has loaded by now is used
                return page.content(), page.url

            html, final = rendered(url)
            if BOT_CHECK.search(page.inner_text("body")[:1500]):
                return []
            for j in jsonld_jobs(html, final, name, source_region):
                jobs[j.ext_id] = j
            host = urlparse(final).netloc
            links = []
            for href in page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
                if urlparse(href).netloc == host and JOB_LINK.search(urlparse(href).path + "?") \
                        and href.split("#")[0] != final.split("#")[0] and href not in links:
                    links.append(href)
            for link in links[:max_pages]:
                try:
                    html, landed = rendered(link)
                except Exception:
                    continue
                for j in jsonld_jobs(html, landed, name, source_region):
                    jobs.setdefault(j.ext_id, j)
        finally:
            browser.close()
    return list(jobs.values())


# ---------- detection and the adapter ----------

def detect(url, name="new-source", source_region="Remote", max_pages=MAX_PAGES, use_browser=False):
    """Work out how to read a site. Returns (params, jobs); params is None when nothing worked."""
    ats = find_ats(url)
    if ats:
        jobs = read_ats(*ats, name, source_region)
        if jobs:
            return {"kind": ats[0], "slug": ats[1]}, jobs
    jobs = read_feed(url, name, source_region)
    if jobs:
        return {"kind": "feed", "url": url}, jobs
    r = _fetch(url)
    if r is None:
        return None, []
    # A hiring-system link inside a page counts only when it is the site's own board (a company
    # careers page). On a job board such a link belongs to one of the companies it lists.
    ats = find_ats("", r.text)
    host = re.sub(r"[^a-z0-9]", "", urlparse(r.url).netloc.lower().removeprefix("www.").rsplit(".", 1)[0])
    own = ats and (lambda slug: slug in host or host in slug)(re.sub(r"[^a-z0-9]", "", ats[1].lower()))
    if own:
        jobs = read_ats(*ats, name, source_region)
        if jobs:
            return {"kind": ats[0], "slug": ats[1]}, jobs
    feed = feed_link_in(r.text, r.url)
    if feed:
        jobs = read_feed(feed, name, source_region)
        if len(jobs) >= 3 and sum(bool(JOB_LINK.search(j.url + "?")) for j in jobs) >= len(jobs) / 2:
            return {"kind": "feed", "url": feed}, jobs      # a jobs feed, not the site's blog
    jobs = read_pages(url, name, source_region, max_pages)
    if jobs:
        return {"kind": "jsonld", "url": url}, jobs
    if use_browser:
        try:
            jobs = read_pages_browser(url, name, source_region, min(max_pages, 8))
        except Exception:
            jobs = []
        if jobs:
            return {"kind": "browser", "url": url}, jobs
    return None, []


def fetch(cfg, params):
    """Adapter used for every source added with `jobbot source add` or found by discovery."""
    name, region, kind = params["name"], params.get("region", "Remote"), params["kind"]
    if kind == "feed":
        return read_feed(params["url"], name, region)
    if kind == "jsonld":
        found = {}   # one site can be searched with several words: `urls` lists one search page per word
        for url in params.get("urls") or [params["url"]]:
            for j in read_pages(url, name, region, params.get("job_pages", JOB_PAGES), params.get("pages", MAX_LISTINGS)):
                found.setdefault(j.ext_id, j)
        return list(found.values())
    if kind == "browser":
        return read_pages_browser(params["url"], name, region, params.get("max_pages", 12),
                                  (cfg.get("browser") or {}).get("channel", "chrome"))
    return read_ats(kind, params["slug"], name, region, params.get("company"))
