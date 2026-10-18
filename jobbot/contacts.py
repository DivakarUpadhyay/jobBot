"""Find a real HR / hiring email for a job. Addresses are only ever read from a page, never guessed.

How the company's own website is found, in order (the label is stored with the email):
  post-link   a link to the company in the job post
  search      a web search for the company, accepted only if the site's address or title carries
              the company name (and, for Taiwan jobs, the site mentions Taiwan)
  guessed     <company>.com/.io/.co/.ai with the same checks; always needs your review

On that site the home page, its careers / jobs / contact / about / team links and a few standard
paths are read. A second web search looks for a hiring address on the company's domain.
Every address must be on the company's domain and that domain must accept mail (MX record).
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import dns.resolver
import requests

from .filter import EMAIL_RE, NOT_EMAIL
from .sources.base import UA

NOT_COMPANY = re.compile(
    r"(?i)(remoteok|remotive|weworkremotely|himalayas|jobicy|workingnomads|yourator|104\.com|1111\.com|518\.com|"
    r"freelancer|ycombinator|themuse|arbeitnow|linkedin|greenhouse|lever\.co|ashbyhq|workable|google|github|twitter|"
    r"x\.com|facebook|youtube|notion|medium|apple\.com|bit\.ly|calendly|typeform|airtable|wellfound|angel\.co|indeed|"
    r"glassdoor|instagram|t\.co|goo\.gl|docs\.|forms\.|w3\.org|schema\.org|crunchbase|wikipedia|cake\.me|cakeresume|"
    r"zoominfo|rocketreach|pitchbook|bloomberg|tracxn|builtin|levels\.fyi|reddit|tiktok|threads\.net|g2\.com|"
    r"trustpilot|yelp|apollo\.io|signalhire|owler|dnb\.com|meet\.jobs|smartrecruiters|bamboohr|recruitee|teamtailor)")
PATHS = ["careers", "jobs", "contact", "about", "contact-us", "join-us", "company/careers"]
LINK_HINT = re.compile(r"(?i)career|job|join|hiring|recruit|contact|about|team|work-with|人才|徵才|招募|聯絡|加入|關於")
HIRING = ["career", "job", "recruit", "talent", "hiring", "hr", "people", "work", "join"]
GENERIC = ["hello", "hi", "contact", "info", "team", "founders", "service"]
SKIP_LOCAL = re.compile(r"(?i)^(support|sales|press|media|abuse|privacy|legal|security|billing|noreply|no-reply|"
                        r"donotreply|webmaster|postmaster|admin|help|feedback|marketing|partners?|investors?|dpo|bd|"
                        r"ir|pr|newsletter|orders?|returns?|complaints?|accessibility|community|service|"
                        r".*accomm?odations?.*|.*disabilit.*|.*accessib.*|n?support|bonjour|hola|web[._-]?admin|web[._-]?master|hr[._-]?support|"
                        r"pr[._-].*|press.*|media[._-].*|comms?|communications?|investor.*|"
                        r"e?mail|name|your[._-]?name|first[._-]?name.*|user(name)?|example|test|you|someone|"
                        r"customer.*|support.*|.*privacy.*|.*abuse.*|.*billing.*|compliance|events?|"
                        r"u00[0-9a-f]{2}.*)$")
TAIWAN = re.compile(r"(?i)taiwan|taipei|台灣|臺灣|台北|臺北|新北|\.tw\b")
SUFFIXES = re.compile(r"(?i)\b(inc|llc|ltd|gmbh|co|corp|corporation|limited|labs?|group|technologies|technology)\b\.?"
                      r"|股份有限公司|有限公司|台灣分公司|集團")
TIMEOUT = 10
_search_lock = threading.Lock()


def _get(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", ""):
            return r
    except requests.RequestException:
        pass
    return None


def _search(query, n=6):
    """Web search results as (url, snippet). One at a time, with a pause, to stay polite."""
    try:
        from ddgs import DDGS
        with _search_lock:
            time.sleep(1.2)
            return [(r.get("href", ""), r.get("body", "")) for r in DDGS(timeout=15).text(query, max_results=n)]
    except Exception:
        return []


def _base_domain(host):
    parts = host.lower().split(".")
    return ".".join(parts[-3:] if len(parts) > 2 and len(parts[-1]) == 2 and len(parts[-2]) <= 3 else parts[-2:])


def has_mx(domain, _cache={}):
    if domain not in _cache:
        try:
            _cache[domain] = bool(dns.resolver.resolve(domain, "MX", lifetime=6))
        except Exception:
            _cache[domain] = False
    return _cache[domain]


def rank(email):
    """Lower is better: hiring addresses, then named people, then generic inboxes."""
    local = email.split("@")[0]
    if any(w in local for w in HIRING):
        return 0
    if local in GENERIC:
        return 2
    return 1


def clean(emails, domain=None):
    out = []
    for e in emails:
        e = e.strip(".").lower()
        local, _, dom = e.partition("@")
        if not local[:1].isalnum() or not local[-1:].isalnum():
            continue   # "-bonjour@..." and the like are cut-off fragments, and they bounce
        if e in out or NOT_EMAIL.search(e) or SKIP_LOCAL.match(local) or re.search(r"\d{5,}|^[0-9a-f]{16,}$", local):
            continue
        if domain and _base_domain(dom) != domain:
            continue
        if has_mx(dom):
            out.append(e)
    return sorted(out, key=rank)


def _names(company):
    """(ascii slug, non-ascii core name) used to recognise the company's own site."""
    core = SUFFIXES.sub("", re.split(r"[_|｜(（]", company)[0]).strip()
    slug = re.sub(r"[^a-z0-9]", "", core.lower())
    cjk = "".join(re.findall(r"[一-鿿]+", SUFFIXES.sub("", company)))
    return slug, cjk


def _is_company_site(resp, slug, cjk, taiwan_job):
    """The page must carry the company's name, and for Taiwan jobs must mention Taiwan."""
    host = re.sub(r"[^a-z0-9]", "", urlparse(resp.url).netloc.lower().removeprefix("www."))
    title = (re.search(r"(?is)<title[^>]*>(.*?)</title>", resp.text) or [None, ""])[1]
    title_slug = re.sub(r"[^a-z0-9]", "", title.lower())
    named = (len(slug) >= 3 and (slug in host or slug in title_slug)) or (len(cjk) >= 2 and cjk[:4] in resp.text)
    return named and (not taiwan_job or bool(TAIWAN.search(resp.url + resp.text[:200000])))


def company_site(company, text, taiwan_job=False):
    """Returns (home response, how_found) or (None, '')."""
    slug, cjk = _names(company)
    for url in re.findall(r"https?://[^\s<>\"')\]]+", text or ""):
        host = urlparse(url).netloc
        if host and not NOT_COMPANY.search(host):
            r = _get(f"https://{host}")
            if r is not None and _is_company_site(r, slug, cjk, taiwan_job):   # skip links to other organisations
                return r, "post-link"
    if len(slug) < 3 and len(cjk) < 2:
        return None, ""
    query = f'"{company.split("_")[0]}" official website' + (" 台灣" if taiwan_job else "")
    for url, _ in _search(query):
        host = urlparse(url).netloc
        if not host or NOT_COMPANY.search(host):
            continue
        r = _get(f"https://{host}")
        if r is not None and _is_company_site(r, slug, cjk, taiwan_job):
            return r, "search"
    if 3 <= len(slug) <= 25:
        for tld in (("com.tw", "tw") if taiwan_job else ()) + ("com", "io", "co", "ai"):
            r = _get(f"https://{slug}.{tld}")
            if r is not None and _is_company_site(r, slug, cjk, taiwan_job):
                return r, "guessed"
    return None, ""


def _pages_to_read(home):
    """Careers / contact / about links found on the home page, then a few standard paths."""
    base = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}/"
    domain = _base_domain(urlparse(home.url).netloc)
    urls = []
    for href, label in re.findall(r'(?is)<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', home.text):
        if LINK_HINT.search(href) or LINK_HINT.search(re.sub(r"<[^>]+>", "", label)[:40]):
            full = urljoin(home.url, href)
            if full.startswith("http") and _base_domain(urlparse(full).netloc) == domain and full not in urls:
                urls.append(full)
    return urls[:6] + [u for u in (urljoin(base, p) for p in PATHS) if u not in urls][:4]


def _emails_in(html):
    page = re.sub(r"\\u00[0-9a-fA-F]{2}", " ", html)
    page = re.sub(r"\\[nrt]", " ", page)   # a literal \n glued to an address ("\nsupport@...") is not part of it   # JSON escapes such as \u003e glued to an address
    page = page.replace("%40", "@").replace("&#64;", "@").replace("&#x40;", "@")
    page = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", page, flags=re.I)
    return EMAIL_RE.findall(page)


def find(company, text, taiwan_job=False):
    """Returns (emails, where_found, company_site). Empty list when nothing trustworthy turns up.
    The site is returned even without an email, unless it was only guessed from the name."""
    home, how = company_site(company, text, taiwan_job)
    if home is None:
        return [], "", ""
    site = "" if how == "guessed" else f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"
    domain = _base_domain(urlparse(home.url).netloc)
    found = _emails_in(home.text)
    best = clean(found, domain)
    where = home.url
    if not (best and rank(best[0]) == 0):
        for url in _pages_to_read(home):
            r = _get(url)
            if r is None:
                continue
            found += _emails_in(r.text)
            best = clean(found, domain)
            if best and rank(best[0]) == 0:
                where = r.url
                break
    if not (best and rank(best[0]) == 0):
        # hiring address quoted on another page (job portal, directory) but on the company's own domain
        for url, snippet in _search(f'"{domain}" careers OR jobs OR hr email "@{domain}"', n=8):
            hits = clean(_emails_in(snippet), domain)
            if hits and rank(hits[0]) < (rank(best[0]) if best else 9):
                found += hits
                best, where = clean(found, domain), f"quoted on {urlparse(url).netloc}"
    if not best:
        return [], "", site
    # anything short of a hiring address on a confirmed site is left for you to check
    doubt = "CHECK " if how == "guessed" or rank(best[0]) == 2 else ""
    kind = {0: "hiring address", 1: "named person", 2: "general inbox"}[rank(best[0])]
    return best[:3], f"{doubt}{kind}, site found by {how}: {where}", site or f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"


def find_many(jobs, workers=6, per_job_seconds=150):
    """jobs: rows with id, company, description, region. Yields (id, emails, where, company_site).
    A lookup that does not finish in time is given up (nothing found) so one stuck website can
    never hold up the run."""
    from concurrent.futures import TimeoutError as Timeout
    pool = ThreadPoolExecutor(workers)
    futures = [(j["id"], pool.submit(find, j["company"], j["description"], j["region"] == "taiwan")) for j in jobs]
    try:
        for job_id, future in futures:
            try:
                yield (job_id, *future.result(timeout=per_job_seconds))
            except Timeout:
                yield job_id, [], "", ""
            except Exception:
                yield job_id, [], "", ""
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
