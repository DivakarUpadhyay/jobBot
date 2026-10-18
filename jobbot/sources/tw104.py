"""104.com.tw public search API, newest first.

jobRo: 1 = full-time, 2 = part-time, 3 = senior, 4 = dispatch (leaving `ro` out returns every kind).
remoteWork: 1 = fully remote, 2 = partly remote.  isnew: only posts updated in the last N days (0, 3, 7, 14, 30)."""
import time

from .base import Job, get

API = "https://www.104.com.tw/jobs/search/api/jobs"
HEAD = {"Referer": "https://www.104.com.tw/jobs/search/"}


def _salary(j):
    lo, hi = j.get("salaryLow") or 0, j.get("salaryHigh") or 0
    if not lo and not hi:
        return ""
    unit = {30: "/hour", 50: "/month", 60: "/year"}.get(j.get("s10"), "")
    return f"TWD {lo:,}-{hi:,}{unit}" if hi and hi < 9999999 else f"TWD {lo:,}+{unit}"


def _job(j):
    d = j.get("appearDate") or ""
    link = (j.get("link") or {}).get("job") or ""
    return Job(
        source="104", ext_id=j["jobNo"], title=j.get("jobName") or "", company=j.get("custName") or "",
        url=("https:" + link if link.startswith("//") else link).split("?")[0],
        description=j.get("description") or "", location=j.get("jobAddrNoDesc") or "",
        region="taiwan", remote=bool(j.get("remoteWorkType")),
        job_type="part" if j.get("jobRo") == 2 else "full",
        tags=[t for t in (j.get("pcSkills") or []) if isinstance(t, str)], salary=_salary(j),
        posted_at=f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else "")


def fetch(cfg, params):
    areas = ",".join(params.get("areas", []))
    local = {"area": areas} if areas else {}
    search = cfg["search"]
    # Your keywords in every kind of job (full-time, senior, dispatch), then the part-time keywords.
    queries = [({"keyword": k, **local}, params.get("pages", 2)) for k in search["keywords"]]
    queries += [({"keyword": k, "ro": 2, **local}, params.get("pages", 2)) for k in search["part_time_keywords"]]
    # Remote work is looked for in all of Taiwan: the newest posts in any field, then your keywords.
    remote = ",".join(str(x) for x in params.get("remote", []))
    if remote:
        queries.append(({"remoteWork": remote}, params.get("remote_pages", 3)))
        queries += [({"keyword": k, "remoteWork": remote}, 1) for k in search["keywords"]]
    jobs = {}
    for extra, pages in queries:
        for page in range(1, pages + 1):
            q = {"mode": "s", "order": 16, "page": page, "pagesize": 30, "searchJobs": 1, **extra}
            if params.get("isnew") is not None:
                q["isnew"] = params["isnew"]
            data = get(API, q, HEAD).json().get("data") or []
            for j in data:
                jobs[j["jobNo"]] = _job(j)
            if len(data) < 30:
                break
            time.sleep(0.4)
    return list(jobs.values())


def companies(cfg, params):
    """Openings at the companies in the `companies` table, a few companies per run in rotation.
    104's company search also returns firms that only mention the name, so a job is kept only
    when the company shown on it carries the name searched for."""
    from .. import companies as store
    jobs = {}
    for c in store.due(params.get("per_run", 20)):
        q = {"mode": "s", "order": 16, "page": 1, "pagesize": 30, "searchJobs": 1, "keyword": c["search"], "kwop": 2}
        if params.get("isnew") is not None:
            q["isnew"] = params["isnew"]
        names = [n.lower() for n in (c["search"], c["name_en"] or "") if len(n) >= 2]
        found = 0
        for j in get(API, q, HEAD).json().get("data") or []:
            shown = (j.get("custName") or "").replace("臺", "台").lower()
            if any(n in shown for n in names):
                jobs[j["jobNo"]] = _job(j)
                found += 1
        store.checked(c["search"], found)
        time.sleep(params.get("wait", 0.8))
    return list(jobs.values())


def detail(url):
    """Full description and contact notes for one job, from 104's job detail feed."""
    code = url.rstrip("/").rsplit("/", 1)[-1]
    d = get(f"https://www.104.com.tw/job/ajax/content/{code}", headers={"Referer": url}).json()["data"]
    c = d.get("contact") or {}
    return {"title": d["header"]["jobName"], "company": d["header"]["custName"],
            "description": d["jobDetail"].get("jobDescription") or "",
            "hr_name": c.get("hrName") or "",
            "contact_text": "\n".join(x for x in (c.get("other"), c.get("reply"), c.get("email") or "") if x)}
