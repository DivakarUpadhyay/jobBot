"""More free feeds: Jobicy, Himalayas, Working Nomads, The Muse (remote), Yourator (Taiwan),
Freelancer.com (bidding projects)."""
import datetime as dt
import time

from .base import Job, get, is_taiwan, strip_html


def _region(text):
    return "taiwan" if is_taiwan(text) else "abroad"


def _day(ts):
    return dt.date.fromtimestamp(int(ts)).isoformat() if ts else ""


def _kind(text):
    text = (text or "").lower()
    return "part" if any(w in text for w in ("part", "contract", "freelance", "temporary")) else "full" if text else ""


def jobicy(cfg, params):
    jobs = {}
    for tag in [None] + list(params.get("tags", [])):
        q = {"count": 50, **({"tag": tag} if tag else {})}
        try:
            found = get("https://jobicy.com/api/v2/remote-jobs", q).json().get("jobs") or []
        except Exception:
            if not tag:
                raise
            continue  # Jobicy rejects some tags (e.g. 'c#'); the other tags still count
        for j in found:
            geo = j.get("jobGeo") or ""
            lo, hi = j.get("salaryMin"), j.get("salaryMax")
            jobs[j["id"]] = Job(
                source="jobicy", ext_id=j["id"], title=strip_html(j["jobTitle"]), company=strip_html(j["companyName"]),
                url=j["url"], description=strip_html(j.get("jobDescription") or j.get("jobExcerpt")),
                location=geo, region=_region(geo), remote=True, job_type=_kind(" ".join(j.get("jobType") or [])),
                salary=f"{j.get('salaryCurrency', '')} {lo}-{hi}/{j.get('salaryPeriod', '')}" if hi else "",
                posted_at=(j.get("pubDate") or "")[:10])
        time.sleep(0.5)
    return list(jobs.values())


def himalayas(cfg, params):
    jobs, cursor = [], None
    for _ in range(params.get("pages", 10)):
        d = get("https://himalayas.app/jobs/api", {"limit": 20, **({"cursor": cursor} if cursor else {})}).json()
        for j in d.get("jobs") or []:
            loc = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
            jobs.append(Job(
                source="himalayas", ext_id=j["guid"].rstrip("/").rsplit("/", 1)[-1] + "@" + (j.get("companySlug") or ""),
                title=j["title"], company=j.get("companyName") or "", url=j.get("applicationLink") or j["guid"],
                description=strip_html(j.get("description") or j.get("excerpt")), location=loc, region=_region(loc),
                remote=True, job_type=_kind(j.get("employmentType")), tags=j.get("categories") or [],
                salary=f"{j.get('currency')} {j.get('minSalary')}-{j.get('maxSalary')}" if j.get("maxSalary") else "",
                posted_at=_day(j.get("pubDate"))))
        cursor = d.get("nextCursor")
        if not cursor:
            break
        time.sleep(0.5)
    return jobs


def workingnomads(cfg, params):
    jobs = []
    for j in get("https://www.workingnomads.com/api/exposed_jobs/").json():
        loc = j.get("location") or ""
        jobs.append(Job(
            source="workingnomads", ext_id=j["url"].rstrip("/").rsplit("/", 1)[-1], title=j["title"],
            company=j.get("company_name") or "", url=j["url"], description=strip_html(j.get("description")),
            location=loc, region=_region(loc), remote=True, tags=(j.get("tags") or "").split(","),
            posted_at=(j.get("pub_date") or "")[:10]))
    return jobs


def themuse(cfg, params):
    jobs = []
    for cat in params.get("categories", ["Software Engineering"]):
        for page in range(1, params.get("pages", 3) + 1):
            d = get("https://www.themuse.com/api/public/jobs",
                    {"page": page, "category": cat, "location": "Flexible / Remote"}).json()
            for j in d.get("results") or []:
                loc = ", ".join(l["name"] for l in j.get("locations") or [])
                jobs.append(Job(
                    source="themuse", ext_id=j["id"], title=j["name"], company=(j.get("company") or {}).get("name") or "",
                    url=j["refs"]["landing_page"], description=strip_html(j.get("contents")),
                    location=loc.replace("Flexible / Remote", "Remote"), region=_region(loc), remote=True,
                    job_type="full", posted_at=(j.get("publication_date") or "")[:10]))
            if page >= d.get("page_count", 1):
                break
            time.sleep(0.4)
    return jobs


def yourator(cfg, params):
    """Taiwan startups. The list gives title and tags only, no description."""
    jobs = {}
    for term in params.get("terms") or cfg["search"]["keywords"]:
        for page in range(1, params.get("pages", 2) + 1):
            p = get("https://www.yourator.co/api/v4/jobs", {"term[]": term, "page": page}).json()["payload"]
            for j in p.get("jobs") or []:
                words = (j["name"] + " " + " ".join(j.get("tags") or [])).lower()
                jobs[j["id"]] = Job(
                    source="yourator", ext_id=j["id"], title=j["name"], company=(j.get("company") or {}).get("brand") or "",
                    url="https://www.yourator.co" + j["path"], description=", ".join(j.get("tags") or []),
                    location=j.get("location") or "", region="taiwan", tags=j.get("tags") or [],
                    remote="remote" in words or "遠端" in words, salary=j.get("salary") or "")
            if not p.get("hasMore"):
                break
            time.sleep(0.4)
    return list(jobs.values())


def freelancer(cfg, params):
    """Open projects on Freelancer.com. These are bid on through the site, never emailed."""
    jobs = {}
    # Freelancer's text search is loose, so projects are picked by its own skill IDs instead.
    for skill in params.get("skill_ids", []):
        d = get("https://www.freelancer.com/api/projects/0.1/projects/active/",
                {"jobs[]": skill, "limit": params.get("limit", 30), "full_description": "true",
                 "sort_field": "time_updated", "languages[]": "en"}).json()
        for j in (d.get("result") or {}).get("projects") or []:
            b, cur = j.get("budget") or {}, (j.get("currency") or {}).get("code", "")
            unit = "/hour" if j.get("type") == "hourly" else " fixed"
            jobs[j["id"]] = Job(
                source="freelancer", ext_id=j["id"], title=j["title"], company="Freelancer.com client",
                url="https://www.freelancer.com/projects/" + j["seo_url"],
                description=j.get("description") or j.get("preview_description") or "", location="Worldwide",
                remote=True, job_type="part", track="freelance",
                salary=f"{cur} {b.get('minimum', 0):,.0f}-{b.get('maximum') or 0:,.0f}{unit}",
                posted_at=_day(j.get("time_submitted")))
        time.sleep(0.5)
    return list(jobs.values())


def jobrapido(cfg, params):
    """Jobrapido lists jobs from many other sites. Only some country editions allow reading."""
    import datetime as dt
    import html as htmllib
    import json
    import re
    jobs = {}
    year = dt.date.today().year
    for cc in params.get("editions", ["tw"]):
        for word in params.get("keywords") or cfg["search"]["keywords"]:
            try:
                page = get(f"https://{cc}.jobrapido.com/", {"w": word}).text
            except Exception:
                continue
            for raw in re.findall(r"data-advert=(?:'([^']+)'|\"([^\"]+)\")", page):
                try:
                    a = json.loads(htmllib.unescape(raw[0] or raw[1]))
                except ValueError:
                    continue
                posted = ""
                try:
                    d = dt.datetime.strptime(f"{a.get('date', '')} {year}", "%d %b %Y").date()
                    posted = (d if d <= dt.date.today() else d.replace(year=year - 1)).isoformat()
                except ValueError:
                    pass
                loc = a.get("location") or ""
                jobs[a["advertId"]] = Job(
                    source=params.get("name", "jobrapido"), ext_id=a["advertId"], title=strip_html(a.get("title") or ""),
                    company=a.get("company") or "", url=a.get("openAdvertUrl") or "",
                    description=strip_html(a.get("bodyHighlighted") or ""), location=loc,
                    region="taiwan" if cc == "tw" or is_taiwan(loc) else "abroad", remote="remote" in loc.lower(),
                    salary=str(a.get("salary") or "").replace("None", ""), posted_at=posted)
            time.sleep(0.5)
    return list(jobs.values())


def instahyre(cfg, params):
    """Instahyre (India): its public job-search API, one search per skill. No email in the post; the bot looks
    the company up afterwards like any other job."""
    jobs = {}
    for skill in params.get("skills", [".net", "c#", "sql server", "angular"]):
        for page in range(params.get("pages", 4)):
            d = get("https://www.instahyre.com/api/v1/job_search",
                    {"skills": skill, "limit": 35, "offset": 35 * page}).json()
            for j in d.get("objects") or []:
                emp = j.get("employer") or {}
                loc = j.get("locations") or "India"
                note = emp.get("instahyre_note") or emp.get("company_tagline") or ""
                jobs[j["id"]] = Job(
                    source="instahyre", ext_id=j["id"], title=j.get("title") or j.get("candidate_title") or "",
                    company=emp.get("company_name") or "", url=j.get("public_url") or "",
                    description=f"{note}\nSkills: {', '.join(j.get('keywords') or [])}\nLocation: {loc}, India",
                    location=f"{loc}, India", region="abroad", remote="home" in loc.lower(), job_type="full",
                    tags=j.get("keywords") or [])
            if not d.get("meta", {}).get("next"):
                break
            time.sleep(0.5)
    return list(jobs.values())
