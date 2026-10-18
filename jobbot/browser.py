"""Open matched jobs in Chrome (Playwright) and check the saved data against the live page."""
import re

from . import HomePath

SHOTS = HomePath("output", "verify")


def _words(text):
    return [w for w in re.findall(r"[A-Za-z0-9#+.]{3,}|[一-鿿]{2,}", text or "")][:6]


def check_jobs(rows, channel="chrome", headless=False):
    """rows: sqlite rows with id, title, company, url. Returns list of result dicts."""
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=headless)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        for row in rows:
            page = ctx.new_page()
            res = {"id": row["id"], "source": row["source"], "title": row["title"], "http": None,
                   "title_found": False, "company_found": False, "note": ""}
            try:
                resp = page.goto(row["url"], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)  # let client-side pages fill in
                res["http"] = resp.status if resp else None
                body = (page.title() + "\n" + page.inner_text("body")).lower()
                hit = lambda text: (lambda ws: bool(ws) and sum(w.lower() in body for w in ws) >= max(1, len(ws) // 2))(_words(text))
                res["title_found"], res["company_found"] = hit(row["title"]), hit(row["company"])
                if re.search(r"captcha|verify you are human|access denied|just a moment", body[:600]):
                    res["note"] = "blocked by bot check"
                page.screenshot(path=str(SHOTS / f"job_{row['id']}.png"))
            except Exception as e:
                res["note"] = type(e).__name__ + ": " + str(e).splitlines()[0][:80]
            finally:
                page.close()
            results.append(res)
        browser.close()
    return results
