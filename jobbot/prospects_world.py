"""Freelance web and mobile offers to companies world-wide, addressed to the business inbox each company publishes
(user, 2026-10-19: "send emails so leads can be generated ... find all proper source and based on true facts and on
correct emails ids send").

Where the facts come from, and nothing else:
  - who runs the company: Wikidata's current CEO statement (P169 with no end date), which cites the company's own
    filings and press; for Taiwan the General Manager and Chairman the company filed with TWSE / TPEx
    (prospects.refresh)
  - the address: one published on the company's own website and domain (prospects.find_on_site, prefer='business':
    ceo@, partners@, sales@, hello@, info@ ...), never a guessed firstname@ pattern; the domain must accept mail (MX)

Country rules (see JOBBOT notes in service-offer-emails.md): Germany, Austria and Switzerland need prior consent for
cold email, so they are listed for LinkedIn only and never emailed. Singapore's Spam Control Act wants <ADV> in the
subject. Canada (CASL) and Australia (Spam Act) accept a conspicuously published business address when the message
says where it was found and relates to the business. Every email names the sender, carries the postal address and
an opt-out, and goes out only once per company."""
import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as Timeout
from urllib.parse import urlparse

import requests

from . import db, suppress
from .sendqueue import CJK
from .prospects import find_on_site, phone_parts
from .services_offer import bullets_en

ORIGIN = "WD"
UA = {"User-Agent": "jobbot/1.0 (personal job search)"}

# country -> (Wikidata id, may be emailed, footer kind)
COUNTRIES = {
    "USA": ("Q30", True, "us"), "Canada": ("Q16", True, "published"), "United Kingdom": ("Q145", True, "published"),
    "Ireland": ("Q27", True, "published"), "France": ("Q142", True, "published"),
    "Netherlands": ("Q55", True, "published"), "Belgium": ("Q31", True, "published"),
    "Sweden": ("Q34", True, "published"), "Norway": ("Q20", True, "published"), "Denmark": ("Q35", True, "published"),
    "Finland": ("Q33", True, "published"), "Spain": ("Q29", True, "published"), "Italy": ("Q38", True, "published"),
    "Australia": ("Q408", True, "published"), "New Zealand": ("Q664", True, "published"),
    "Singapore": ("Q334", True, "adv"), "Malaysia": ("Q833", True, "published"), "India": ("Q668", True, "published"),
    "China": ("Q148", True, "published"), "Taiwan": ("Q865", True, "published"),
    "Germany": ("Q183", False, None), "Austria": ("Q40", False, None), "Switzerland": ("Q39", False, None),
}
NO_EMAIL_WHY = "cold email needs prior consent in this country: LinkedIn message only"
MAX_EMPLOYEES = 5000   # a freelancer's offer to a 50,000-person group's info@ inbox is not read

QUERY = """SELECT ?c ?site ?ceo ?start WHERE {{ ?c wdt:P17 wd:{qid} . ?c p:P169 ?st . ?st ps:P169 ?ceo . ?c wdt:P856 ?site .
 OPTIONAL {{ ?st pq:P580 ?start }}
 FILTER NOT EXISTS {{ ?st pq:P582 ?e }} FILTER NOT EXISTS {{ ?c wdt:P576 ?d }} }}"""


def _sparql(query):
    for attempt in range(3):
        try:
            r = requests.get("https://query.wikidata.org/sparql", params={"query": query}, timeout=90,
                             headers={**UA, "Accept": "application/sparql-results+json"})
            if r.ok:
                return r.json()["results"]["bindings"]
        except Exception:
            pass
        time.sleep(5 * (attempt + 1))
    return []


def _entities(ids):
    """{id: entity} with English (else any) labels and claims, 50 per request."""
    out, ids = {}, list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        for attempt in range(6):
            try:
                r = requests.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=60,
                                 params={"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "format": "json",
                                         "props": "labels|claims", "languages": "en|zh-tw|zh|fr|nl|sv|es|it|de|ms",
                                         "maxlag": 5})
                if r.status_code == 200 and "entities" in r.json():
                    out.update(r.json()["entities"])
                    break
                wait = r.headers.get("Retry-After", "")   # rate limited or lagging: wait as asked
                time.sleep(int(wait) if wait.isdigit() else 10 * (attempt + 1))
            except Exception:
                time.sleep(10 * (attempt + 1))
    return out


LOCAL_LANGS = {"Taiwan": ("zh-tw", "zh"), "China": ("zh", "zh-tw"), "France": ("fr",), "Netherlands": ("nl",),
               "Belgium": ("nl", "fr"), "Sweden": ("sv",), "Spain": ("es",), "Italy": ("it",), "Germany": ("de",),
               "Austria": ("de",), "Switzerland": ("de", "fr", "it"), "Malaysia": ("ms",)}


def _label(e, country=None):
    """English name, else the country's own language; never a name in an unrelated language."""
    labels = (e or {}).get("labels", {})
    for lang in ("en",) + (LOCAL_LANGS.get(country, ()) if country else ("zh-tw", "zh", "fr", "nl", "sv", "es", "it", "de", "ms")):
        if lang in labels:
            return labels[lang]["value"]
    return ""


def resolve_labels(con):
    """Turn place and industry ids (Q123) left by an interrupted or rate-limited import into names."""
    rows = con.execute("SELECT DISTINCT city FROM companies WHERE origin=? AND city GLOB 'Q[0-9]*' UNION "
                       "SELECT DISTINCT industry FROM companies WHERE origin=? AND industry GLOB 'Q[0-9]*'",
                       (ORIGIN, ORIGIN)).fetchall()
    names = {k: _label(v) for k, v in _entities([r[0] for r in rows]).items()}
    for k, v in names.items():
        if v:
            con.execute("UPDATE companies SET city=? WHERE origin=? AND city=?", (v, ORIGIN, k))
            con.execute("UPDATE companies SET industry=? WHERE origin=? AND industry=?", (v, ORIGIN, k))
    con.commit()
    return len(names)


def _claim(e, prop):
    for c in (e or {}).get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if v is not None:
            return v
    return None


def import_country(con, country):
    """Store the companies of one country that have a current CEO and a website. Returns how many."""
    qid = COUNTRIES[country][0]
    rows = _sparql(QUERY.format(qid=qid))
    by_company = {}
    for b in rows:
        cid = b["c"]["value"].rsplit("/", 1)[1]
        start = b.get("start", {}).get("value", "")[:4]
        old = by_company.get(cid)
        if not old or start > old[2]:   # several open CEO statements: the most recent start wins
            by_company[cid] = (b["site"]["value"], b["ceo"]["value"].rsplit("/", 1)[1], start)
    ents = _entities(list(by_company))
    ceos = _entities([ceo for _, ceo, _ in by_company.values()])
    places = {}
    n = 0
    for cid, (site, ceo, start) in by_company.items():
        e = ents.get(cid)
        name, ceo_name = _label(e, country), _label(ceos.get(ceo), country)
        if not name or re.fullmatch(r"Q\d+", name) or not ceo_name or _claim(ceos.get(ceo), "P570"):
            continue   # no name, or the recorded CEO has died: the record is out of date
        title = f"CEO (since {start})" if start else "CEO (start date not recorded: check)"
        emp = _claim(e, "P1128")
        employees = int(float(emp["amount"])) if isinstance(emp, dict) and "amount" in emp else None
        if employees and employees > MAX_EMPLOYEES:
            continue
        hq = _claim(e, "P159")
        industry = _claim(e, "P452")
        places.setdefault(hq.get("id") if isinstance(hq, dict) else None, None)
        con.execute("INSERT INTO companies(name_zh, name_en, search, website, origin, industry, priority, added_at) "
                    "VALUES('', ?, ?, ?, ?, ?, 0, ?) ON CONFLICT(search) DO NOTHING",
                    (name, f"{name} ({country})", site, ORIGIN, "", db.now()))
        con.execute("UPDATE companies SET country=?, short_name=?, ceo_name=?, ceo_title=?, employees=?, "
                    "names_source=?, website=COALESCE(NULLIF(website, ''), ?), city=COALESCE(city, ?), "
                    "industry=COALESCE(NULLIF(industry, ''), ?) WHERE search=? AND origin=?",
                    (country, name, ceo_name, title, employees,f"Wikidata {cid} (current CEO statement)", site,
                     hq.get("id") if isinstance(hq, dict) else None,
                     industry.get("id") if isinstance(industry, dict) else "", f"{name} ({country})", ORIGIN))
        n += 1
    con.commit()
    # place and industry ids -> names, in one pass
    ids = [r[0] for r in con.execute("SELECT DISTINCT city FROM companies WHERE origin=? AND country=? AND city LIKE 'Q%'",
                                     (ORIGIN, country))]
    ids += [r[0] for r in con.execute("SELECT DISTINCT industry FROM companies WHERE origin=? AND country=? "
                                      "AND industry LIKE 'Q%'", (ORIGIN, country))]
    names = {k: _label(v) for k, v in _entities(ids).items()}
    for k, v in names.items():
        con.execute("UPDATE companies SET city=? WHERE origin=? AND country=? AND city=?", (v, ORIGIN, country, k))
        con.execute("UPDATE companies SET industry=? WHERE origin=? AND country=? AND industry=?", (v, ORIGIN, country, k))
    con.commit()
    return n


# ---------------------------------------------------------------- the address

def mail_domain_ok(email, _cache={}):
    """True when the address's domain has a mail server (MX, else an A record as the standard fallback)."""
    import dns.resolver
    dom = email.rsplit("@", 1)[1].lower()
    if dom not in _cache:
        ok = False
        for kind in ("MX", "A"):
            try:
                dns.resolver.resolve(dom, kind, lifetime=8)
                ok = True
                break
            except Exception:
                continue
        _cache[dom] = ok
    return _cache[dom]


def find_business(con, rows, workers=16, per_company_seconds=75, on_result=None):
    """Look up each company's published business inbox and store it. Returns how many were found."""
    pool = ThreadPoolExecutor(workers)
    futures = [(r, pool.submit(find_on_site, r["website"], False, "business")) for r in rows]
    found = 0
    try:
        for r, future in futures:
            try:
                email, where = future.result(timeout=per_company_seconds)
            except (Timeout, Exception):
                email, where = "", ""
            if email and not mail_domain_ok(email):
                email, where = "", f"no mail server for {email}"
            con.execute("UPDATE companies SET biz_email=?, biz_email_where=?, biz_checked_at=? WHERE id=?",
                        (email or None, where or None, db.now(), r["id"]))
            con.commit()
            found += bool(email)
            if on_result:
                on_result(r, email, where)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return found


# ---------------------------------------------------------------- the list (CSV)

CSV_COLUMNS = ["country", "company", "ceo_name", "ceo_title", "chairman", "email", "email_type", "email_found_on",
               "website", "city", "industry", "employees", "names_source", "may_email", "already_contacted"]


def _contacted(con):
    out = {}
    for r in con.execute("SELECT ext_id, source, COALESCE(send_status, 'waiting') s FROM jobs "
                         "WHERE source IN ('prospect', 'services')"):
        out.setdefault(r["ext_id"], f"{r['source']}: {r['s']}")
    return out


def _country(r):
    if r["country"]:
        return r["country"]
    if r["origin"] in ("TWSE", "TPEx"):
        return "Taiwan"
    if r["origin"] == "IN-Wikidata":
        return "India"
    if r["origin"] == "YC":
        return "Canada" if "Canada" in (r["city"] or "") else "USA"
    return ""


def export_csv(con, path):
    """Every company with a name to address: who runs it (where known), the best published address, and whether it
    may be emailed. Returns rows written."""
    contacted = _contacted(con)
    blocked = suppress.blocked(con=con)
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: Excel shows Chinese names correctly
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in con.execute("SELECT * FROM companies WHERE origin IN ('TWSE', 'TPEx', 'IN-Wikidata', 'YC', ?) "
                             "ORDER BY country, origin, name_en", (ORIGIN,)):
            country = _country(r)
            if r["biz_email"]:
                email, kind, where = r["biz_email"], "business inbox on the company's website", r["biz_email_where"]
            elif r["origin"] in ("TWSE", "TPEx") and r["published_email"]:
                email, kind, where = r["published_email"], "filed with the stock exchange (often IR / spokesperson)", "TWSE / TPEx open data"
            elif r["hr_email"]:
                kind = "general inbox" if "general inbox" in (r["hr_email_where"] or "") else "hiring inbox"
                email, where = r["hr_email"], r["hr_email_where"]
            else:
                email = kind = where = ""
            if not (email or r["ceo_name"]):
                continue
            rule = COUNTRIES.get(country)
            may = ("no: " + NO_EMAIL_WHY) if rule and not rule[1] else \
                  ("no: on the do-not-contact list" if email and email in blocked else
                   ("no: no published address" if not email else "yes"))
            w.writerow([country, r["short_name"] or r["name_en"] or r["name_zh"], r["ceo_name"] or "", r["ceo_title"] or "",
                        r["chairman"] or "", email, kind, where or "", r["website"] or "", r["city"] or "",
                        r["industry"] or "", r["employees"] or "",
                        r["names_source"] or ("YC directory (founders not named)" if r["origin"] == "YC" else ""),
                        may, contacted.get(str(r["id"]), "")])
            n += 1
    return n


# ---------------------------------------------------------------- the email

def footer(country, where, postal_address):
    kind = COUNTRIES[country][2]
    src = f"I found your address on your website ({where})." if where else ""
    lines = [postal_address]
    if kind in ("published", "adv"):
        lines.insert(0, src)
    lines.append('If you would rather not hear from me, reply "unsubscribe" and I will not write again.')
    return "\n".join(l for l in lines if l)


def write(company, master, postal_address, sender_email="{sender}"):
    """(subject, body): a plain freelance offer for web and mobile work, with the facts above only."""
    country = company["country"]
    name = master["name"].replace(" R.", "")
    links = master["contact"].get("links") or []
    portfolio = next((l for l in links if "portfolio" in l or "vercel" in l), "")
    _, phone = phone_parts(master["contact"].get("phone", ""))
    firm = company["short_name"] or company["name_en"]
    # Wikidata's CEO can be out of date, so the email greets the company, not a person who may have left
    greeting = f"Dear {firm} team,"
    subject = f"Freelance web and mobile developer available to support {firm}"
    if COUNTRIES[country][2] == "adv":
        subject = "<ADV> " + subject
    where = re.sub(r"^business inbox: ", "", company["biz_email_where"] or "")
    body = (f"{greeting}\n\n"
            f"I am a freelance web and mobile developer with 11 years of experience (C# / .NET, SQL Server, React, "
            f"Next.js, Angular), working remotely from Taiwan. I help companies build and maintain websites and apps "
            f"without hiring: fixed price per project, hourly, or part-time monthly.\n\n"
            f"What I can do for {firm}:\n{bullets_en()}\n\n"
            + (f"Examples of my work: {portfolio}\n\n" if portfolio else "")
            + f"If you have a website or app in mind, or an existing system that needs fixes or new features, I would "
              f"be glad to look at it and send a free estimate. Would a 15-minute call be useful?\n\n"
              f"Best regards,\n{name}\nFreelance Web & Mobile Developer\n{phone}\n{sender_email}\n\n"
            + footer(country, where, postal_address))
    return subject, body


def queue(con, master, postal_address, countries=None, limit=None):
    """One offer per company that may be emailed and was never contacted. Waits for approval (send_status NULL)."""
    contacted = _contacted(con)
    blocked = suppress.blocked(con=con)
    rows = con.execute("SELECT * FROM companies WHERE origin=? AND biz_email IS NOT NULL ORDER BY employees IS NULL, employees",
                       (ORIGIN,)).fetchall()
    added = skipped = 0
    for c in rows:
        if countries and c["country"] not in countries:
            continue
        if not COUNTRIES.get(c["country"], (0, False))[1] or str(c["id"]) in contacted or c["biz_email"] in blocked:
            skipped += 1
            continue
        if con.execute("SELECT 1 FROM jobs WHERE to_email=? OR emails=?", (c["biz_email"], f'["{c["biz_email"]}"]')).fetchone():
            skipped += 1   # this address already had an email from another list
            continue
        subject, body = write(c, master, postal_address)
        firm = c["short_name"] or c["name_en"]
        if c["country"] != "Taiwan" and CJK.search(subject + body):
            skipped += 1   # user, 2026-10-19: no Chinese in emails outside Taiwan (no English company name known)
            continue
        con.execute("""INSERT OR IGNORE INTO jobs(source, ext_id, title, company, location, region, remote, job_type, track,
                       category, url, description, tags, emails, score, score_notes, status, dedupe_key, found_at,
                       matched_on, resume_path, email_subject, email_body, tailored_by, email_source, contact_checked,
                       company_url)
                       VALUES('services',?,?,?,?,?,1,'prospect','freelance','Services',?,?,'[]',?,50,?,
                       'matched',?,?,?,'',?,?,'template',?,1,?)""",
                    (str(c["id"]), "Freelance web and mobile offer", firm, f"{c['city'] or ''}, {c['country']}",
                     "taiwan" if c["country"] == "Taiwan" else "abroad",
                     c["website"] or "", f"CEO: {c['ceo_name']} ({c['names_source']})", f'["{c["biz_email"]}"]',
                     f"{c['country']} company, CEO from Wikidata",
                     re.sub(r"[\W_]+", "", firm.lower()) + "|services", db.now(), db.today(), subject, body,
                     f"published on the company's own website: {c['biz_email_where']}", c["website"] or None))
        added += 1
        if limit and added >= limit:
            break
    con.commit()
    return added, skipped
