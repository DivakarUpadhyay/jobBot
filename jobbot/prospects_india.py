"""Speculative applications to Indian companies, by headquarters city and industry.

Companies come from Wikidata (headquarters city, official website, industry) and are kept in `companies`
with origin 'IN-Wikidata'. The hiring address is read from the company's own website, as for Taiwan
(prospects.find_on_site). The email is English only and does not mention the Taiwan Gold Card."""
import re
import time

import requests

from . import db
from .prospects import ANGLE, phone_parts
from .services_offer import bullets_en

INDIA_ORIGIN = "IN-Wikidata"
INDIA_CITIES = ["Mumbai", "Navi Mumbai", "Thane", "Pune", "Noida", "Greater Noida", "Gurgaon", "New Delhi", "Delhi", "Surat",
                "Vadodara", "Ahmedabad", "Gandhinagar", "Rajkot", "Bengaluru", "Hyderabad", "Chennai", "Kolkata", "Indore",
                "Nagpur", "Jaipur", "Chandigarh", "Mohali", "Kochi", "Thiruvananthapuram", "Coimbatore", "Bhubaneswar",
                "Lucknow", "Nashik", "Aurangabad", "Mangalore", "Mysore", "Visakhapatnam", "Faridabad", "Ghaziabad", "Bhopal"]
CITY_ALIASES = {"bombay": "Mumbai", "bangalore": "Bengaluru", "gurugram": "Gurgaon", "baroda": "Vadodara",
                "calcutta": "Kolkata", "madras": "Chennai", "cochin": "Kochi", "mysuru": "Mysore", "trivandrum": "Thiruvananthapuram"}
WD_QUERY = """SELECT ?c ?cLabel ?site (SAMPLE(?indL) AS ?industry) WHERE {{
  ?c wdt:P159 wd:{qid}; wdt:P856 ?site.
  FILTER NOT EXISTS {{ ?c wdt:P576 ?end }}
  FILTER NOT EXISTS {{ ?c wdt:P31/wdt:P279* wd:Q3918 }}
  FILTER NOT EXISTS {{ ?c wdt:P31/wdt:P279* wd:Q327333 }}
  OPTIONAL {{ ?c wdt:P452 ?ind. ?ind rdfs:label ?indL. FILTER(LANG(?indL)="en") }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} GROUP BY ?c ?cLabel ?site"""
WD_UA = {"User-Agent": "jobbot/1.0 (personal job search)"}

# industry words -> (fit tier, which paragraph of prospects.ANGLE the email uses)
TIERS = [(re.compile(r"(?i)software|information technology|\bit\b|internet|computer|digital|cloud|data|saas|"
                     r"technology|consult|outsourc"), 1, "it"),
         (re.compile(r"(?i)bank|financ|fintech|insur|payment|invest|broker|asset management|stock"), 1, "finance"),
         (re.compile(r"(?i)telecom|network|communication"), 1, "telecom"),
         (re.compile(r"(?i)e-?commerce|retail|online|marketplace"), 1, "commerce"),
         (re.compile(r"(?i)pharma|health|hospital|medic|biotech|diagnost"), 2, "health"),
         (re.compile(r"(?i)logistic|shipping|transport|courier|freight|airline|aviation|rail"), 2, "logistics"),
         (re.compile(r"(?i)electronic|semiconductor|manufactur|automotive|engineering|industrial|steel|chemical|"
                     r"textile|diamond|jewel|energy|power"), 2, "manufacturing")]
# One kind of work per email (user, 2026-10-16). Tier-1 firms (software, finance, telecom, e-commerce) are asked
# about full-time work (remote or relocation); the rest about freelance projects, since the candidate is in Taiwan.
OFFER = {
    "full": {"title": "Senior Software Engineer (.NET) – full-time",
             "ask": "any full-time openings in software development, system integration or IT",
             "mode": "I am looking for full-time work. I am currently based in Taiwan, can work remotely, and am open to "
                     "relocating to {city} for the right role."},
    "freelance": {"title": "Senior Software Engineer (.NET) – freelance projects",
                  "ask": "projects in software development, system integration or IT where outside help would be useful",
                  "mode": "I work on a freelance, project basis, remotely from Taiwan, and am happy to start with a small, "
                          "well-defined task."},
}
TITLE = OFFER["full"]["title"]


def offer_for(company):
    """'full' for tier 1, 'freelance' for tier 2 (healthcare, logistics, manufacturing: firms that buy software and
    IoT projects), None for the rest: their line of business is unknown, so a freelance offer would be a guess."""
    tier = fit(company["industry"])[0]
    return "full" if tier == 1 else "freelance" if tier == 2 else None


def fit(industry):
    """(tier, angle key) from the industry words Wikidata gives."""
    for pattern, tier, angle in TIERS:
        if pattern.search(industry or ""):
            return tier, angle
    return 3, "general"


def _city_qid(name):
    r = requests.get("https://www.wikidata.org/w/api.php", headers=WD_UA, timeout=30,
                     params={"action": "wbsearchentities", "search": name, "language": "en", "type": "item",
                             "limit": 10, "format": "json"}).json()
    for hit in r.get("search", []):
        d = (hit.get("description") or "").lower()
        if "india" in d and any(w in d for w in ("city", "metropol", "capital", "town", "municipal", "district", "satellite")):
            return hit["id"]
    return None


def import_companies(con, cities=None, on_city=None):
    """Indian companies with a website, by headquarters city. Returns {city: companies stored}."""
    from . import companies
    counts = {}
    for name in cities or INDIA_CITIES:
        qid = _city_qid(name)
        res = None
        for _ in range(3 if qid else 0):
            try:
                res = requests.get("https://query.wikidata.org/sparql", timeout=70, params={"query": WD_QUERY.format(qid=qid)},
                                   headers={**WD_UA, "Accept": "application/sparql-results+json"}).json()
                break
            except Exception:
                time.sleep(5)
        n = 0
        for b in (res or {}).get("results", {}).get("bindings", []):
            label = b["cLabel"]["value"]
            if re.fullmatch(r"Q\d+", label):
                continue   # no English name
            search = companies.add(name_en=label, website=b["site"]["value"], origin=INDIA_ORIGIN,
                                   industry=b.get("industry", {}).get("value", ""), con=con)
            if search:
                con.execute("UPDATE companies SET city=COALESCE(NULLIF(city, ''), ?), short_name=? WHERE search=?",
                            (name, label, search))
                n += 1
        con.commit()
        counts[name] = n
        if on_city:
            on_city(name, n)
        time.sleep(1)
    return counts


def select(con, city=None, industry=None, tier=None, need_email=False, not_checked=False, limit=None):
    """Indian companies matching the filters, best fit first."""
    rows = con.execute("SELECT * FROM companies WHERE enabled=1 AND origin=?", (INDIA_ORIGIN,)).fetchall()
    wanted = {CITY_ALIASES.get(c.strip().lower(), c.strip()).lower() for c in city.split(",")} if city else None
    words = [w.strip().lower() for w in industry.split(",")] if industry else None
    out = []
    for r in rows:
        t, angle = fit(r["industry"])
        if wanted and (r["city"] or "").lower() not in wanted:
            continue
        if words and not any(w in (r["industry"] or "").lower() or w == angle for w in words):
            continue
        if tier and t > int(tier):
            continue
        if need_email and not r["hr_email"]:
            continue
        if not_checked and r["email_checked_at"]:
            continue
        out.append((t, r["id"], r))
    out.sort(key=lambda x: x[:2])
    rows = [x[2] for x in out]
    return rows[:limit] if limit else rows


def write(company, master, sender_email="{sender}", offer=None):
    """(subject, body) in English, about one kind of work. Facts only from the resume; no Gold Card (Taiwan only)."""
    offer_kind = offer or offer_for(company)
    o = OFFER[offer_kind]
    name = master["name"].replace(" R.", "")
    links = master["contact"].get("links") or []
    portfolio = next((l for l in links if "portfolio" in l or "vercel" in l), "")
    linkedin = next((l for l in links if "linkedin" in l), "")
    _, intl_phone = phone_parts(master["contact"].get("phone", ""))
    company_name = company["short_name"] or company["name_en"] or company["search"]
    city = company["city"] or "India"
    angle_en = ANGLE[fit(company["industry"])[1]][1]
    subject = f"{o['title']} – {name} | {company_name}"
    folio = f"You can see my portfolio and project examples at {portfolio}\n\n" if portfolio else ""
    body = (f"Dear Hiring Team,\n\n"
            f"I am writing to ask whether {company_name} has {o['ask']}.\n\n"
            f"I am a Senior Software Engineer with 10+ years of experience building enterprise applications on Microsoft .NET "
            f"(C#, ASP.NET Core, .NET 8) and Microsoft SQL Server, covering backend architecture, REST and SOAP API design, "
            f"third-party integrations, and front ends in Angular and Next.js, across FinTech, HealthTech and logistics. "
            f"I worked in Mumbai from 2015 to 2021, on projects for clients such as Bajaj Finserv, Bajaj Electricals and "
            f"Tata DoCoMo, and have since worked in Taiwan, including on-site at Acer.\n\n"
            f"{angle_en}\n\n"
            f"{o['mode'].format(city=city)}\n\n"
            + (f"Services I offer:\n{bullets_en()}\n\n" if offer_kind == "freelance" else "")
            + f""
            f"{folio}"
            f"My resume is attached. If this message has reached the wrong department, I would be grateful if you could "
            f"forward it to {'the person who handles IT projects' if offer_kind == 'freelance' else 'HR or the hiring team'}.\n\n"
            f"Best regards,\n{name}\n{intl_phone}\n{sender_email}"
            + "".join(f"\n{l}" for l in (linkedin, portfolio) if l))
    return subject, body


def queue_one(con, c, master, resume_path, to, how):
    """One approved prospect row in `jobs` (region 'abroad': the resume carries no Gold Card)."""
    kind = offer_for(c)
    if kind is None:
        return 0
    subject, body = write(c, master, offer=kind)
    tier, angle = fit(c["industry"])
    name = c["short_name"] or c["name_en"] or c["search"]
    norm = re.sub(r"[\W_]+", "", name.lower())
    con.execute("""INSERT OR IGNORE INTO jobs(source, ext_id, title, company, location, region, remote, job_type, track,
                   category, url, description, tags, emails, score, score_notes, status, dedupe_key, found_at,
                   matched_on, resume_path, email_subject, email_body, tailored_by, email_source, contact_checked,
                   send_status, company_url)
                   VALUES('prospect',?,?,?,?,'abroad',0,'prospect',?,'Prospect',?,?,'[]',?,?,?,'matched',?,?,?,?,?,?,
                   'template',?,1,'approved',?)""",
                (str(c["id"]), OFFER[kind]["title"], name, f"{c['city'] or ''}, India".strip(", "), kind, c["website"] or "",
                 c["industry"] or "", f'["{to}"]', 60 - 10 * tier, f"India {angle}", f"{norm}|prospect", db.now(),
                 db.today(), str(resume_path), subject, body, how, c["website"] or None))
