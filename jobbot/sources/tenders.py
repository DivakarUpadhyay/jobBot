"""Leads generated directly: open public tenders for websites and software, read from the
EU's official tender database (TED). The buyer's contact email is published in the notice
on purpose, for suppliers. Covers the EU countries plus Norway, Iceland and others that publish there.
"""
from .base import Job, get_json_post

API = "https://api.ted.europa.eu/v3/notices/search"
# CPV codes: website design, web page editing, internet / intranet development, custom software,
# software development, programming services
DEFAULT_CPV = ["72413000", "72212224", "72420000", "72421000", "72422000", "72230000", "72262000", "72211000"]
FIELDS = ["notice-type", "publication-number", "publication-date", "title-proc", "description-proc", "buyer-name",
          "organisation-email-buyer", "buyer-email", "touchpoint-email-buyer", "organisation-country-buyer",
          "organisation-city-buyer", "organisation-internet-address-buyer", "deadline-receipt-tender-date-lot",
          "classification-cpv"]
COUNTRIES = {"AUT": "Austria", "BEL": "Belgium", "BGR": "Bulgaria", "HRV": "Croatia", "CYP": "Cyprus", "CZE": "Czechia",
             "DNK": "Denmark", "EST": "Estonia", "FIN": "Finland", "FRA": "France", "DEU": "Germany", "GRC": "Greece",
             "HUN": "Hungary", "IRL": "Ireland", "ITA": "Italy", "LVA": "Latvia", "LTU": "Lithuania", "LUX": "Luxembourg",
             "MLT": "Malta", "NLD": "Netherlands", "POL": "Poland", "PRT": "Portugal", "ROU": "Romania", "SVK": "Slovakia",
             "SVN": "Slovenia", "ESP": "Spain", "SWE": "Sweden", "NOR": "Norway", "ISL": "Iceland", "CHE": "Switzerland",
             "LIE": "Liechtenstein", "GBR": "United Kingdom", "MKD": "North Macedonia", "SRB": "Serbia", "TUR": "Turkey"}


def _first(value):
    """TED returns text per language and often as lists; prefer English, else the first one."""
    if isinstance(value, dict):
        value = value.get("eng") or next(iter(value.values()), "")
    while isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def ted(cfg, params):
    import datetime as dt
    since = (dt.date.today() - dt.timedelta(days=cfg.get("max_age_days", 14))).strftime("%Y%m%d")
    cpv = " ".join(params.get("cpv") or DEFAULT_CPV)
    query = f"classification-cpv IN ({cpv}) AND publication-date>={since} SORT BY publication-date DESC"
    jobs = []
    for page in range(1, params.get("pages", 4) + 1):
        d = get_json_post(API, {"query": query, "fields": FIELDS, "limit": 100, "page": page, "scope": "ACTIVE",
                                "paginationMode": "PAGE_NUMBER"})
        notices = d.get("notices") or []
        for n in notices:
            if not str(n.get("notice-type", "")).startswith(("cn", "pin-cfc")):
                continue   # only open calls; award notices and plans are no use as leads
            number = n["publication-number"]
            email = _first(n.get("organisation-email-buyer")) or _first(n.get("buyer-email")) or _first(n.get("touchpoint-email-buyer"))
            country = COUNTRIES.get(_first(n.get("organisation-country-buyer")), _first(n.get("organisation-country-buyer")))
            title = _first(n.get("title-proc"))
            if not title:
                continue
            jobs.append(Job(
                source=params.get("name", "ted-tenders"), ext_id=number, title=title, company=_first(n.get("buyer-name")),
                url=f"https://ted.europa.eu/en/notice/-/detail/{number}", description=_first(n.get("description-proc"))[:3000],
                location=", ".join(x for x in (_first(n.get("organisation-city-buyer")), country) if x), remote=True,
                job_type="lead", track="lead", posted_at=str(n.get("publication-date") or "")[:10],
                extra={"email": email, "country": country, "city": _first(n.get("organisation-city-buyer")),
                       "deadline": _first(n.get("deadline-receipt-tender-date-lot"))[:10],
                       "website": _first(n.get("organisation-internet-address-buyer")),
                       "kind": "Website / software tender", "note": "EU TED (official EU tenders)"}))
        if len(notices) < 100:
            break
    return jobs


AWARD_FIELDS = ["notice-type", "publication-number", "publication-date", "title-proc", "description-proc", "buyer-name",
                "organisation-country-buyer", "winner-name", "winner-email", "winner-internet-address", "winner-country",
                "winner-city"]


def ted_awards(cfg, params):
    """Subcontracting leads: companies that have just WON a website / software contract (EU award
    notices). The winner's contact email is published in the notice. One lead per winning company."""
    import datetime as dt
    since = (dt.date.today() - dt.timedelta(days=cfg.get("max_age_days", 14))).strftime("%Y%m%d")
    cpv = " ".join(params.get("cpv") or DEFAULT_CPV)
    query = (f"classification-cpv IN ({cpv}) AND notice-type IN (can-standard can-social can-modif) "
             f"AND publication-date>={since} SORT BY publication-date DESC")
    leads = {}
    for page in range(1, params.get("pages", 3) + 1):
        d = get_json_post(API, {"query": query, "fields": AWARD_FIELDS, "limit": 100, "page": page, "scope": "ACTIVE",
                                "paginationMode": "PAGE_NUMBER"})
        notices = d.get("notices") or []
        for n in notices:
            winner, email, title = _first(n.get("winner-name")), _first(n.get("winner-email")), _first(n.get("title-proc"))
            if not winner or not email or not title or email.lower() in leads:
                continue
            number = n["publication-number"]
            country = COUNTRIES.get(_first(n.get("winner-country")), _first(n.get("winner-country")))
            buyer = _first(n.get("buyer-name"))
            leads[email.lower()] = Job(
                source=params.get("name", "ted-awards"), ext_id="award-" + number, title=title, company=winner,
                url=f"https://ted.europa.eu/en/notice/-/detail/{number}", description=_first(n.get("description-proc"))[:3000],
                location=", ".join(x for x in (_first(n.get("winner-city")), country) if x), remote=True,
                job_type="lead", track="subcontract", posted_at=str(n.get("publication-date") or "")[:10],
                extra={"email": email, "country": country, "city": _first(n.get("winner-city")),
                       "website": _first(n.get("winner-internet-address")), "kind": "Contract winner (subcontracting)",
                       "note": f"won this contract from {buyer}" if buyer else "won this public contract", "buyer": buyer})
        if len(notices) < 100:
            break
    return list(leads.values())


def uk_contracts(cfg, params):
    """Open UK public contracts for IT and software (Contracts Finder), with the buyer's published contact."""
    import datetime as dt
    import html as htmllib
    from .base import get
    since = (dt.date.today() - dt.timedelta(days=cfg.get("max_age_days", 14))).isoformat()
    api = "https://www.contractsfinder.service.gov.uk/api/rest/2"
    seen, jobs = set(), []
    for word in params.get("keywords", ["software development", "website development", "application development",
                                        "systems integration", "database", "API integration"]):
        d = get_json_post(f"{api}/search_notices/json",
                          {"searchCriteria": {"keyword": word, "statuses": ["Open"], "publishedFrom": since}, "size": 60})
        for hit in d.get("noticeList") or []:
            it = hit["item"]
            codes = str(it.get("cpvCodes") or "").split()
            if it["id"] in seen or not any(c.startswith(("72", "48")) for c in codes):
                continue   # 72 = IT services, 48 = software packages; the keyword search alone is too loose
            seen.add(it["id"])
            try:
                c = (get(f"{api}/get_published_notice/json/{it['id']}").json().get("notice") or {}).get("contactDetails") or {}
            except Exception:
                c = {}
            jobs.append(Job(
                source=params.get("name", "uk-contracts"), ext_id=it["id"], title=htmllib.unescape(it.get("title") or ""),
                company=it.get("organisationName") or "", url=f"https://www.contractsfinder.service.gov.uk/notice/{it['id']}",
                description=htmllib.unescape(it.get("description") or "")[:3000],
                location=", ".join(x for x in (c.get("town"), "United Kingdom") if x), remote=True, job_type="lead",
                track="lead", posted_at=str(it.get("publishedDate") or "")[:10],
                extra={"email": c.get("email") or "", "country": "United Kingdom", "city": c.get("town") or "",
                       "deadline": str(it.get("deadlineDate") or "")[:10], "website": c.get("webAddress") or "",
                       "person": c.get("name") or "", "kind": "Website / software tender",
                       "note": "UK Contracts Finder (official)"}))
    return jobs


def uk_find_a_tender(cfg, params):
    """Open UK tenders above the threshold (Find a Tender), IT and software only."""
    import datetime as dt
    from .base import get
    since = (dt.datetime.now() - dt.timedelta(days=cfg.get("max_age_days", 14))).strftime("%Y-%m-%dT00:00:00")
    url, jobs, pages = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages", [], 0
    query = {"limit": 100, "stages": "tender", "updatedFrom": since}
    while url and pages < params.get("pages", 6):
        d = get(url, query).json()
        pages += 1
        for rel in d.get("releases") or []:
            t = rel.get("tender") or {}
            codes = [str((t.get("classification") or {}).get("id") or "")] + \
                    [str(((i.get("classification") or {}).get("id")) or "") for i in t.get("items") or []] + \
                    [str(a.get("id") or "") for i in t.get("items") or [] for a in i.get("additionalClassifications") or []]
            if not t.get("title") or not any(c.startswith(("72", "48")) for c in codes):
                continue
            buyer = next((p for p in rel.get("parties") or [] if "buyer" in (p.get("roles") or [])), {})
            c = buyer.get("contactPoint") or {}
            addr = buyer.get("address") or {}
            jobs.append(Job(
                source=params.get("name", "uk-find-a-tender"), ext_id=rel.get("ocid") or rel.get("id"), title=t["title"],
                company=buyer.get("name") or "", url=f"https://www.find-tender.service.gov.uk/Notice/{rel.get('id', '')}",
                description=str(t.get("description") or "")[:3000],
                location=", ".join(x for x in (addr.get("locality"), "United Kingdom") if x), remote=True, job_type="lead",
                track="lead", posted_at=str(rel.get("date") or "")[:10],
                extra={"email": c.get("email") or "", "country": "United Kingdom", "city": addr.get("locality") or "",
                       "deadline": str((t.get("tenderPeriod") or {}).get("endDate") or "")[:10],
                       "website": c.get("url") or "", "person": c.get("name") or "", "kind": "Website / software tender",
                       "note": "UK Find a Tender (official)"}))
        url, query = (d.get("links") or {}).get("next"), None
    return jobs
