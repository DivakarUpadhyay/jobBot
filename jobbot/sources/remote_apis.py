"""Free remote-job feeds: Remotive, RemoteOK, Arbeitnow, We Work Remotely, Hacker News 'Who is hiring'."""
import datetime as dt
import re
import time

import feedparser

from .base import Job, get, is_taiwan, strip_html


def _region(text):
    return "taiwan" if is_taiwan(text) else "abroad"


def remotive(cfg, params):
    jobs = []
    for cat in params.get("categories", ["software-dev"]):
        for j in get("https://remotive.com/api/remote-jobs", {"category": cat}).json().get("jobs", []):
            loc = j.get("candidate_required_location") or ""
            jobs.append(Job(
                source="remotive", ext_id=j["id"], title=j["title"], company=j["company_name"], url=j["url"],
                description=strip_html(j.get("description")), location=loc, region=_region(loc), remote=True,
                job_type={"full_time": "full", "part_time": "part", "contract": "part", "freelance": "part"}.get(
                    j.get("job_type"), ""),
                tags=j.get("tags") or [], salary=j.get("salary") or "", posted_at=(j.get("publication_date") or "")[:10]))
    return jobs


# RemoteOK appends "Please mention the word X and tag Y when applying..." to every post.
# The tag encodes the reader's IP address; it is not part of the job, so it is removed.
REMOTEOK_TAG = re.compile(r"(?is)please mention the word.*?(?:they're human\.?|$)")


def remoteok(cfg, params):
    jobs = []
    for j in get("https://remoteok.com/api").json():
        if not j.get("position"):
            continue  # first element is the API's legal notice
        loc = j.get("location") or ""
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        jobs.append(Job(
            source="remoteok", ext_id=j["id"], title=j["position"], company=j.get("company") or "", url=j["url"],
            description=REMOTEOK_TAG.sub("", strip_html(j.get("description"))).strip(), location=loc, region=_region(loc), remote=True,
            tags=j.get("tags") or [], salary=f"USD {lo:,}-{hi:,}/year" if hi else "",
            posted_at=(j.get("date") or "")[:10]))
    return jobs


def arbeitnow(cfg, params):
    jobs = []
    for page in range(1, params.get("pages", 2) + 1):
        for j in get("https://www.arbeitnow.com/api/job-board-api", {"page": page}).json().get("data", []):
            types = " ".join(j.get("job_types") or []).lower()
            ts = j.get("created_at")
            jobs.append(Job(
                source="arbeitnow", ext_id=j["slug"], title=j["title"], company=j["company_name"], url=j["url"],
                description=strip_html(j.get("description")), location=j.get("location") or "",
                region=_region(j.get("location")), remote=bool(j.get("remote")),
                job_type="part" if re.search(r"part|contract|freelance|working student", types) else "full",
                tags=j.get("tags") or [],
                posted_at=dt.date.fromtimestamp(ts).isoformat() if ts else ""))
        time.sleep(0.4)
    return jobs


def wwr(cfg, params):
    jobs = []
    for feed in params.get("feeds", []):
        for e in feedparser.parse(get(feed).content).entries:
            company, _, title = e.title.partition(": ")
            loc = e.get("region") or ""
            t = e.get("published_parsed")
            jobs.append(Job(
                source="weworkremotely", ext_id=e.link.rstrip("/").rsplit("/", 1)[-1], title=title or e.title,
                company=company if title else "", url=e.link, description=strip_html(e.get("summary")),
                location=loc, region=_region(loc), remote=True,
                job_type="full" if "full" in (e.get("type") or "").lower() else "part" if e.get("type") else "",
                posted_at=f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}" if t else ""))
    return jobs


def hn(cfg, params):
    """Top-level comments of the newest 'Ask HN: Who is hiring?' thread. First line is
    usually 'Company | Role | Location | REMOTE ...'."""
    api = "https://hn.algolia.com/api/v1/search_by_date"
    stories = get(api, {"tags": "story,author_whoishiring", "hitsPerPage": 6}).json()["hits"]
    story = next((s for s in stories if "who is hiring" in s["title"].lower()), None)
    if not story:
        return []
    jobs = []
    for page in range(params.get("pages", 4)):
        hits = get(api, {"tags": f"comment,story_{story['objectID']}", "hitsPerPage": 100, "page": page}).json()["hits"]
        for h in hits:
            if str(h.get("parent_id")) != str(story["objectID"]):
                continue  # replies, not job posts
            text = strip_html(h.get("comment_text"))
            head = text.split("\n", 1)[0]
            parts = [p.strip() for p in head.split("|")]
            if len(parts) < 2:
                continue  # not in the usual job-post format
            jobs.append(Job(
                source="hn-whoishiring", ext_id=h["objectID"], title=" | ".join(parts[1:])[:150],
                company=re.sub(r"\s*\(?https?://\S+\)?|\s*\([^)]*\)", "", parts[0]).strip()[:80] or parts[0][:80],
                url=f"https://news.ycombinator.com/item?id={h['objectID']}", description=text,
                location=head[:150], region=_region(head), remote=bool(re.search(r"(?i)\bremote\b", head)),
                posted_at=(h.get("created_at") or "")[:10]))
        if len(hits) < 100:
            break
    return jobs
