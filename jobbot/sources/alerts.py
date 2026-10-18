"""Jobs from the job-alert emails that job sites send to your own inboxes.

LinkedIn, Indeed, Naukri, 1111, JobsDB, ZipRecruiter and others cannot be read directly (they block
automated reading or forbid it), but when you set up an alert they email you their new jobs. Only
those emails are read, never the sites, and nothing in the inbox is changed (not even marked read).
Title, company, place and link come from the email; the HR address is looked up afterwards by the
contacts step, from the company's own website.
"""
import datetime as dt
import email
import hashlib
import html
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from .base import Job, is_taiwan

# site: (sender addresses or domains, pattern of a job link with its id as group 1 or None, country of its jobs)
SITES = {
    "linkedin": (("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"), r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", ""),
    "indeed": (("jobalert.indeed.com", "alert@indeed.com"), r"indeed\.[a-z.]+/(?:rc/clk|viewjob|m/viewjob|pagead/clk)[^\"'\s]*?[?&]jk=([0-9a-f]{16})", ""),
    "naukri": (("naukrialerts@naukri.com", "jobalert@naukri.com"), r"naukri\.com/(?:jd/)?job-listings-[^?\"'\s]*?-(\d{9,})", "India"),
    "1111": (("ms.1111.com.tw",), r"1111\.com\.tw/job/(\d+)", "Taiwan"),
    "518": (("service@518.com.tw",), r"518\.com\.tw/job-([A-Za-z0-9]+)\.html", "Taiwan"),
    "jobsdb": (("e.jobsdb.com", "jobsdb.com"), None, ""),
    "jobstreet": (("e.jobstreet.com", "s.seek.com"), None, ""),
    "ziprecruiter": (("alerts@ziprecruiter.com",), None, "USA"),
}
INDEED_COUNTRY = {"tw": "Taiwan", "in": "India", "sg": "Singapore", "hk": "Hong Kong", "my": "Malaysia", "jp": "Japan",
                  "kr": "South Korea", "uk": "United Kingdom", "ca": "Canada", "au": "Australia", "de": "Germany"}
NOT_A_TITLE = re.compile(r"(?i)^(apply|1-click apply|apply now|view|see|learn more|more|save|easy apply|unsubscribe|"
                         r"查看|詳細內容|應徵|立即應徵)\b|job alert for|view all|see all|manage|privacy|help|terms|settings")
SEP = re.compile(r"\s+[·•|]\s+")
LABEL = re.compile(r"^(工作地點|學歷要求|職務類別|工作經驗|工作待遇|工作內容|查看更多.*|下載.*APP|Location:?|Company:?)$", re.I)


def _decode(value):
    return str(make_header(decode_header(value or "")))


def _html(msg):
    for kind in ("text/html", "text/plain"):
        for part in msg.walk():
            if part.get_content_type() == kind:
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _chunks(page):
    """The email as a list of (text, link): every piece of visible text, with the link it sits in."""
    page = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", page)
    out, href = [], None
    for tag, text in re.findall(r"(<[^>]+>)|([^<]+)", page):
        if tag:
            m = re.match(r'(?is)<a\b[^>]*href=["\']([^"\']+)', tag)
            href = html.unescape(m.group(1)) if m else (None if re.match(r"(?i)</a", tag) else href)
            continue
        text = " ".join(html.unescape(text).replace("͏", " ").replace("​", " ").split())
        if text:
            if out and out[-1][1] == href and href:
                out[-1] = (out[-1][0] + " " + text, href)   # one link split over several tags
            else:
                out.append((text, href))
    return out


def _job_id(site, pattern, href):
    for candidate in (href, unquote(href), unquote(unquote(href))):
        m = re.search(pattern, candidate) if pattern else None
        if m:
            return m.group(1)
    return None


def _clean_url(site, job_id, href):
    return {"linkedin": f"https://www.linkedin.com/jobs/view/{job_id}",
            "1111": f"https://www.1111.com.tw/job/{job_id}",
            "518": f"https://www.518.com.tw/job-{job_id}.html"}.get(site) or href.split("&from=")[0]


def parse(site, page, subject="", when=""):
    """Jobs in one alert email."""
    domains, pattern, country = SITES[site]
    chunks, jobs = _chunks(page), {}
    for i, (text, href) in enumerate(chunks):
        if not href or len(text) < 4 or len(text) > 140 or NOT_A_TITLE.search(text) or LABEL.match(text):
            continue
        job_id = _job_id(site, pattern, href)
        if pattern and not job_id:
            continue
        if not pattern and not re.search(r"(?i)jobsdb|jobstreet|seek|ziprecruiter", href):
            continue
        after = [t for t, h in chunks[i + 1:i + 8] if t != text and not NOT_A_TITLE.search(t) and not LABEL.match(t)
                 and not re.fullmatch(r"[\d.]+", t)]
        if not after:
            continue
        parts = SEP.split(after[0])
        company = parts[0][:120]
        place = parts[1] if len(parts) > 1 else (after[1] if len(after) > 1 and len(after[1]) < 80 else "")
        if site == "indeed":
            host = re.search(r"//([a-z]{2})\.indeed\.", href)
            country = INDEED_COUNTRY.get(host.group(1), "") if host else ""
        if country and country.lower() not in place.lower():
            place = f"{place}, {country}" if place else country
        key = job_id or hashlib.sha1(f"{text}|{company}".lower().encode()).hexdigest()[:16]
        extra = " ".join(after[1:4])
        jobs.setdefault(key, Job(
            source=f"alert-{site}", ext_id=key, title=text, company=company,
            url=_clean_url(site, job_id, href) if job_id else href, description=f"{text}\n{company}\n{place}\n{extra}",
            location=place[:120], region="taiwan" if country == "Taiwan" or is_taiwan(place) else "abroad",
            remote=bool(re.search(r"(?i)\bremote\b|work from home|遠端", f"{text} {place} {extra}")), posted_at=when))
    return list(jobs.values())


def _single_job_from_subject(site, subject, when):
    """Alerts that carry one job named in the subject line (Foundit, Cake)."""
    if site == "foundit":
        parts = [p.strip() for p in subject.split("|")]
        if len(parts) >= 3 and parts[0].lower() == "job":
            company, title = parts[1], re.sub(r"\s*\([^)]*yrs\)", "", parts[2])
            place = parts[3] if len(parts) > 3 else ""
            key = hashlib.sha1(f"{title}|{company}".lower().encode()).hexdigest()[:16]
            return [Job(source="alert-foundit", ext_id=key, title=title, company=company, url="https://www.foundit.in",
                        description=subject, location=place, region="abroad", posted_at=when,
                        remote=bool(re.search(r"(?i)remote|work from home", subject)))]
    if site == "cake":
        m = re.match(r"(.+?), the company you followed, posted a new job - (.+)", subject)
        if m:
            key = hashlib.sha1(subject.lower().encode()).hexdigest()[:16]
            return [Job(source="alert-cake", ext_id=key, title=m.group(2).strip(), company=m.group(1).strip(),
                        url="https://www.cake.me", description=subject, location="Taiwan", region="taiwan", posted_at=when)]
    return []


SUBJECT_SITES = {"foundit": ("opportunities@foundit.in",), "cake": ("no-reply.cake.me",)}


def fetch(cfg, params):
    """Adapter: read the alert emails of the last `days` days in every sender account's inbox."""
    from .. import mailer
    days, per_account = params.get("days", 3), params.get("emails_per_account", 80)
    every = {**{k: v[0] for k, v in SITES.items()}, **SUBJECT_SITES}
    query = "(" + " OR ".join(f"from:{d}" for ds in every.values() for d in ds) + f") newer_than:{days}d"
    jobs = {}
    for account in mailer.senders():
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
            m.login(account["email"], account["password"])
            m.select("INBOX", readonly=True)
            _, data = m.uid("SEARCH", None, "X-GM-RAW", f'"{query}"')
            for uid in data[0].split()[-per_account:]:
                _, parts = m.uid("FETCH", uid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(parts[0][1])
                sender = (msg.get("From") or "").lower()
                site = next((s for s, ds in every.items() if any(d in sender for d in ds)), None)
                if not site:
                    continue
                subject = _decode(msg.get("Subject"))
                try:
                    when = parsedate_to_datetime(msg.get("Date")).date().isoformat()
                except Exception:
                    when = dt.date.today().isoformat()
                found = _single_job_from_subject(site, subject, when) if site in SUBJECT_SITES else parse(site, _html(msg), subject, when)
                for j in found:
                    jobs.setdefault((j.source, j.ext_id), j)
            m.logout()
        except Exception:
            continue   # one account that cannot be read does not stop the others
    return list(jobs.values())
