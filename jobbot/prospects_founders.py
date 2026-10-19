"""Service offers to startup founders in the USA and Canada (development work on contract / part-time).

Companies: the public Y Combinator directory (active, USA / Canada, 50 people or fewer, with a website),
read from the community mirror yc-oss.github.io. The address is one the startup publishes on its own
website (prospects.find_on_site: hiring address, else its general inbox such as hello@ / founders@),
which is what Canada's anti-spam law (CASL, "conspicuous publication") requires for a first message.

The email is a commercial message, so (US CAN-SPAM) it identifies the sender, has an honest subject,
an opt-out line, and the sender's postal address. Emails wait (send_status NULL) until the postal
address is set in the settings (`services.postal_address`); then `jobbot prospects approve --services`."""
import re

import requests

from .services_offer import bullets_en
from . import db
from .prospects import phone_parts

ORIGIN = "YC"
DATA = "https://yc-oss.github.io/api/companies/all.json"
MAX_TEAM = 50

# What the email offers, by the startup's industry words: (one sentence of relevant experience, services line)
OFFERS = [
    (re.compile(r"(?i)fintech|finance|payment|banking|insurance|lending|crypto|web3|blockchain"),
     "I have built payment integrations (Stripe, PayU), KYC flows (Sumsub) and audit trails, and wrote the Solidity "
     "contracts and .NET 8 backend of a tokenized-asset investment platform."),
    (re.compile(r"(?i)health|medical|bio|clinic|pharma"),
     "I worked as a senior engineer on a corporate and consumer health platform, integrating pharmacy and "
     "telemedicine services."),
    (re.compile(r"(?i)logistic|supply chain|shipping|delivery|transport|freight"),
     "I have integrated FedEx and TNT EDI and DHL services for Acer's repair-centre system."),
    (re.compile(r"(?i)e-?commerce|retail|marketplace|consumer"),
     "I have built Next.js storefronts and portals on .NET backends with payment-gateway integrations."),
    (re.compile(r"(?i)b2b|saas|enterprise|analytics|data|developer tools|infrastructure"),
     "I have built B2B portals, admin tools, SQL Server reporting (SSRS) and data pipelines (SSIS) for enterprise clients."),
]
DEFAULT_OFFER = ("I have delivered enterprise applications end to end, from requirements and database design to "
                 "APIs, front ends and release.")


def offer(industry):
    return next((sentence for pattern, sentence in OFFERS if pattern.search(industry or "")), DEFAULT_OFFER)


def import_companies(con):
    """Store the YC startups that fit. Returns how many were stored."""
    from . import companies
    rows = requests.get(DATA, timeout=120, headers={"User-Agent": "jobbot/1.0"}).json()
    n = 0
    for x in rows:
        loc = x.get("all_locations") or ""
        if x.get("status") != "Active" or not x.get("website") or not (0 < (x.get("team_size") or 0) <= MAX_TEAM):
            continue
        if not re.search(r"USA|United States|Canada", loc):
            continue
        industry = ", ".join(x.get("industries") or []) or x.get("industry") or ""
        search = companies.add(name_en=x["name"], website=x["website"], origin=ORIGIN, industry=industry, con=con)
        if search:
            city = loc.split(";")[0].strip()
            con.execute("UPDATE companies SET city=?, short_name=?, address=? WHERE search=? AND origin=?",
                        (city, x["name"], (x.get("one_liner") or "")[:300], search, ORIGIN))
            n += 1
    con.commit()
    return n


def select(con, need_email=False, not_checked=False, limit=None, country=None):
    rows = con.execute("SELECT * FROM companies WHERE enabled=1 AND origin=?", (ORIGIN,)).fetchall()
    out = [r for r in rows if (not need_email or r["hr_email"]) and (not not_checked or not r["email_checked_at"])
           and (not country or (country.lower() == "canada") == ("Canada" in (r["city"] or "")))]
    return out[:limit] if limit else out


def founder_subject(startup):
    """A plain, professional subject: who is writing and for whom. No sales words, no list of technologies
    (user, 2026-10-18: "contract development ... should not go in the subject; subject needs to be proper")."""
    return f"Freelance web and mobile developer available to support {startup}"


def write(company, master, postal_address, sender_email="{sender}"):
    """(subject, body). Plain, specific, with opt-out and postal address."""
    name = master["name"].replace(" R.", "")
    links = master["contact"].get("links") or []
    portfolio = next((l for l in links if "portfolio" in l or "vercel" in l), "")
    linkedin = next((l for l in links if "linkedin" in l), "")
    _, phone = phone_parts(master["contact"].get("phone", ""))
    startup = company["short_name"] or company["name_en"] or company["search"]
    subject = founder_subject(startup)
    body = (f"Hi {startup} team,\n\n"
            f"I am a freelance web and mobile developer (11 years) offering development help to startups on a "
            f"project basis, working remotely from Taiwan with good overlap with North American mornings "
            f"and evenings.\n\n"
            f"{offer(company['industry'])}\n\n"
            f"Services I offer:\n{bullets_en()}\n\n"
            + (f"Examples of my work: {portfolio}\n\n" if portfolio else "")
            + f"If any of this would help {startup}, I would be glad to have a short call or start with a small, "
              f"well-defined task.\n\n"
              f"Best regards,\n{name}\n{phone}\n{sender_email}"
            + "".join(f"\n{l}" for l in (linkedin,) if l)
            + f"\n{postal_address}\n\n"
              f"If you would rather not hear from me, reply \"no thanks\" and I will not write again.")
    return subject, body


def queue(con, rows, master, postal_address, resume_path):
    """One row per startup with a published address. Waits for your approval (send_status NULL)."""
    added = known = 0
    for c in rows:
        to = c["hr_email"]
        if not to:
            continue
        if con.execute("SELECT 1 FROM jobs WHERE source='services' AND ext_id=?", (str(c["id"]),)).fetchone():
            known += 1
            continue
        subject, body = write(c, master, postal_address)
        startup = c["short_name"] or c["name_en"] or c["search"]
        con.execute("""INSERT OR IGNORE INTO jobs(source, ext_id, title, company, location, region, remote, job_type, track,
                       category, url, description, tags, emails, score, score_notes, status, dedupe_key, found_at,
                       matched_on, resume_path, email_subject, email_body, tailored_by, email_source, contact_checked,
                       company_url)
                       VALUES('services',?,?,?,?,'abroad',1,'prospect','freelance','Services',?,?,'[]',?,50,'YC startup',
                       'matched',?,?,?,?,?,?,'template',?,1,?)""",
                    (str(c["id"]), "Freelance development offer", startup, c["city"] or "", c["website"] or "",
                     c["address"] or "", f'["{to}"]', re.sub(r"[\W_]+", "", startup.lower()) + "|services",
                     db.now(), db.today(), str(resume_path), subject, body,
                     f"published on the startup's own website: {c['hr_email_where']}", c["website"] or None))
        added += 1
    con.commit()
    return added, known
