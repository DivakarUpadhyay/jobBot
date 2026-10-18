"""Build a resume PDF from the master resume, in the layout of the original PDF."""
import copy
import re

from jinja2 import Environment, select_autoescape

from . import HomePath, profilestore

OUTPUT = HomePath("output")
GOLD_CARD = re.compile(r"(?i)gold\s*card")


def load_master():
    """Every fact a resume may use (document `resume` in the database)."""
    return profilestore.get("resume")


def build(region, email=None, master=None, tailored=None):
    """Resume data for one job. region: 'taiwan' or 'abroad'.
    Gold Card lines appear only when the job is in Taiwan.
    tailored: optional job-specific headline / summary / skills / experience bullets."""
    r = copy.deepcopy(master or load_master())
    t = tailored or {}
    for key in ("headline", "summary", "skills"):
        if t.get(key):
            r[key] = t[key]
    for job, new in zip(r["experience"], t.get("experience") or []):
        if new.get("bullets"):
            job["bullets"] = new["bullets"]
    taiwan = region == "taiwan"
    if taiwan and r.get("summary_taiwan_only"):
        r["summary"] = f"{r['summary']} {r['summary_taiwan_only']}"
    r["additional"] = [a for a in r["additional"] if taiwan or not a.get("taiwan_only")]
    if email:
        r["contact"]["email"] = email
    return r


def to_html(resume, template=None):
    """template: the layout to use; default is the document `resume-template` in the database."""
    env = Environment(autoescape=select_autoescape(["html"], default_for_string=True))
    return env.from_string(template or profilestore.get("resume-template")).render(r=resume)


def check_region_rule(html, region):
    """Safety net: no Gold Card wording may reach a resume for a job outside Taiwan."""
    if region != "taiwan" and GOLD_CARD.search(html):
        raise ValueError("Gold Card text found in a non-Taiwan resume; not rendering.")


class PdfPrinter:
    """One Chrome instance for many PDFs. Use as: with PdfPrinter() as pdf: pdf.save(html, path)"""

    def __init__(self, channel="chrome"):
        self.channel = channel

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(channel=self.channel, headless=True)
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def save(self, html, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page = self._browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out_path), format="A4", print_background=True, prefer_css_page_size=True)
        page.close()
        _clean_metadata(out_path)
        return out_path


def to_pdf(html, out_path, channel="chrome"):
    with PdfPrinter(channel) as pdf:
        return pdf.save(html, out_path)


def _clean_metadata(path, author=None):
    """Chrome stamps 'HeadlessChrome' into the PDF properties; replace with plain author details."""
    import pymupdf
    author = author or load_master()["name"]
    doc = pymupdf.open(path)
    doc.set_metadata({"title": f"{author} - Resume", "author": author, "creator": "", "producer": "",
                      "subject": "", "keywords": ""})
    doc.del_xml_metadata()
    tmp = path.with_suffix(".tmp.pdf")
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    import time
    for attempt in range(6):   # a virus scanner or another step may hold the file for a moment
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    tmp.replace(path)


def render(region, out_path, email=None, channel="chrome"):
    html = to_html(build(region, email))
    check_region_rule(html, region)
    return to_pdf(html, out_path, channel)
