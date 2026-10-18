import html
import re
from dataclasses import dataclass, field

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
TIMEOUT = 40


@dataclass
class Job:
    source: str
    ext_id: str
    title: str
    company: str
    url: str
    description: str = ""
    location: str = ""
    region: str = "abroad"      # 'taiwan' or 'abroad'
    remote: bool = False
    job_type: str = ""          # 'full', 'part', or '' to let the filter decide from the text
    track: str = ""             # set to 'freelance' by bidding-platform sources; else the filter decides
    tags: list = field(default_factory=list)
    salary: str = ""
    posted_at: str = ""         # ISO date, or '' if the source does not say
    extra: dict = field(default_factory=dict)   # lead details: email, deadline, country, website


def get(url, params=None, headers=None):
    r = requests.get(url, params=params, headers={"User-Agent": UA, **(headers or {})}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def get_json_post(url, body):
    r = requests.post(url, json=body, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.json()


def strip_html(text):
    text = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d)\s*/?>", "\n", text or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def is_taiwan(text):
    return bool(re.search(r"(?i)taiwan|taipei|hsinchu|taichung|kaohsiung|台灣|臺灣|台北|新北", text or ""))
