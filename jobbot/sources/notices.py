"""Recruitment notice boards: the "Careers / Recruitment / Vacancies" pages of government bodies, institutes
and companies that publish each opening as a link, usually to a PDF, instead of as job data.

Every notice whose title looks like a vacancy is opened (PDFs are read as text). Its title, closing date
and the email address it asks applications to go to become a job. Addresses written as "name[at]nic[dot]in"
are turned back into real addresses, because that is how government notices publish them. Notices whose
closing date has passed are dropped.
"""
import datetime as dt
import hashlib
import html
import re
from urllib.parse import urljoin, urlparse

from .base import Job, get

VACANCY = re.compile(r"(?i)recruit|vacanc|advertis|advt|engagement|walk[- ]?in|hiring|position|post of|posts? |"
                     r"consultant|developer|engineer|programmer|analyst|architect|manager|officer|expert|specialist|"
                     r"contract|deputation|fellow|project (?:staff|associate|assistant)|career|job")
NOT_A_NOTICE = re.compile(r"(?i)result|answer key|admit card|interview schedule|corrigendum|cancell?ation|shortlist|"
                          r"selected|select list|waitlist|application form|past vacanc|tender|rti\b|archive|login|register|"
                          r"privacy|disclaimer|sitemap|feedback|contact us|home$")
# Only bracketed forms: a plain " at " in a sentence ("available at nic.in") must not become an address.
OBFUSCATED_AT = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\[@\])\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*", re.I)
DATE = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](20\d\d)|(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+(20\d\d)", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
LAST_DATE = re.compile(r"(?i)(last date|closing date|on or before|latest by|deadline|due date)[^\n]{0,80}")


def readable_emails(text):
    """'recruit[at]nic[dot]in' -> 'recruit@nic.in', so the normal email finder sees it."""
    text = OBFUSCATED_AT.sub("@", text)
    return OBFUSCATED_DOT.sub(".", text)


def _dates(text):
    out = []
    for m in DATE.finditer(text):
        try:
            if m.group(1):
                out.append(dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
            else:
                out.append(dt.date(int(m.group(6)), MONTHS[m.group(5)[:3].lower()], int(m.group(4))))
        except ValueError:
            continue
    return out


def _text_of(url):
    """Text of a notice: a PDF read page by page, or an HTML page without its markup."""
    r = get(url)
    if url.lower().split("?")[0].endswith(".pdf") or "pdf" in (r.headers.get("content-type") or "").lower():
        import pymupdf
        with pymupdf.open(stream=r.content, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in list(doc)[:12])
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", r.text)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def read_board(url, name, organisation, place="India", max_notices=25, max_age_days=120):
    page = get(url)
    seen, jobs, today = set(), [], dt.date.today()
    for href, label in re.findall(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', page.text):
        title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", label)).split())
        link = urljoin(page.url, html.unescape(href.strip()))
        if (len(title) < 12 or link in seen or not VACANCY.search(title) or NOT_A_NOTICE.search(title)
                or urlparse(link).scheme not in ("http", "https")):
            continue
        seen.add(link)
        if len(seen) > max_notices:
            break
        try:
            text = readable_emails(_text_of(link))
        except Exception:
            text = title
        dates = _dates(title + "\n" + text)
        closing = [d for m in LAST_DATE.finditer(text) for d in _dates(m.group(0))]
        if closing and max(closing) < today:
            continue                                   # applications already closed
        published = max((d for d in dates if d <= today), default=None)
        if published and (today - published).days > max_age_days and not closing:
            continue                                   # old notice with no closing date
        jobs.append(Job(
            source=name, ext_id=hashlib.sha1(link.encode()).hexdigest()[:16], title=title[:200], company=organisation,
            url=link, description=f"{title}\n\n{text[:6000]}", location=place, region="abroad",
            posted_at=(published or today).isoformat(), job_type="full",
            extra={"deadline": max(closing).isoformat()} if closing else {}))
    return jobs


def fetch(cfg, params):
    """Adapter for sources with adapter 'notices'."""
    return read_board(params["url"], params["name"], params.get("organisation", params["name"]),
                      params.get("place", "India"), params.get("max_notices", 25))
