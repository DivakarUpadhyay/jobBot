"""Console commands for speculative applications to Taiwan companies by city, district and industry."""
from collections import Counter
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from . import HomePath, db, prospects as pr, render
from .cli import app, console
from .config import load_config

prospects_app = typer.Typer(no_args_is_help=True, help="Speculative applications to Taiwan listed companies.")
app.add_typer(prospects_app, name="prospects")

CityOpt = typer.Option(None, help="City, e.g. Taipei, 新北, Hsinchu (city and county). Several with commas.")
DistrictOpt = typer.Option(None, help="District, e.g. 內湖, 南港, 竹北. Several with commas.")
IndustryOpt = typer.Option(None, help="Industry code or name: 30, 資訊服務, IT, fintech, semiconductor ... Several with commas.")
TierOpt = typer.Option(None, help="1 = software / finance / telecom / computers only, 2 = also large tech firms, 3 = all.")
CountryOpt = typer.Option("Taiwan", help="Taiwan (listed companies, bilingual email) or India (companies by city, English).")


@prospects_app.command("founders")
def founders(limit: int = typer.Option(3000, help="Most startup websites to look up in this run."),
             workers: int = typer.Option(20)):
    """Service offers to USA / Canada startups (Y Combinator directory): import, find the address each startup
    publishes on its website, write the email. They wait until services.postal_address is set and you approve."""
    from . import prospects_founders as pf
    con = db.connect()
    console.print(f"Startups stored: {pf.import_companies(con)}")
    rows = [r for r in pf.select(con, not_checked=True) if r["website"]][:limit]
    console.print(f"Looking up {len(rows)} startup websites...")
    found = pr.find_many(con, rows, workers=workers)
    console.print(f"Address found for {found} of {len(rows)}.")
    postal = (load_config().get("services") or {}).get("postal_address") or "[postal address needed]"
    added, known = pf.queue(con, pf.select(con, need_email=True), render.load_master(), postal, "")
    console.print(f"Service offers written: {added} new, {known} before. They wait for your approval.")


@prospects_app.command("world")
def world(countries: Optional[str] = typer.Option(None, help="Countries, comma separated; default: all in prospects_world."),
          find: bool = typer.Option(True, help="Also look up each company's business inbox on its website."),
          workers: int = typer.Option(16)):
    """Companies with a current CEO and a website, by country (Wikidata), and the business inbox each publishes."""
    from . import prospects_world as pw
    con = db.connect()
    wanted = [c.strip() for c in countries.split(",")] if countries else list(pw.COUNTRIES)
    for country in wanted:
        console.print(f"  {country}: {pw.import_country(con, country)} companies with a CEO", highlight=False)
    pw.resolve_labels(con)
    if find:
        rows = [r for r in con.execute("SELECT * FROM companies WHERE origin=? AND biz_checked_at IS NULL AND website<>''",
                                       (pw.ORIGIN,)) if r["country"] in wanted]
        console.print(f"Looking up {len(rows)} websites...")
        console.print(f"Business inbox found for {pw.find_business(con, rows, workers=workers)} of {len(rows)}.")


@prospects_app.command("export-csv")
def export_csv(path: Path = typer.Argument(Path("ceo-contacts.csv"), help="Where to write the list.")):
    """All companies with who runs them, the published address and whether they may be emailed, as one CSV."""
    from . import prospects_world as pw
    console.print(f"{pw.export_csv(db.connect(), path)} companies written to {path.resolve()}")


@prospects_app.command("world-queue")
def world_queue(countries: Optional[str] = typer.Option(None, help="Countries, comma separated; default: all allowed."),
                limit: Optional[int] = typer.Option(None)):
    """Write the freelance offer for each company that may be emailed. They wait for `approve --services`."""
    from . import prospects_world as pw
    con = db.connect()
    postal = (load_config().get("services") or {}).get("postal_address") or "[postal address needed]"
    added, skipped = pw.queue(con, render.load_master(), postal,
                              [c.strip() for c in countries.split(",")] if countries else None, limit)
    console.print(f"Offers written: {added}; skipped (not allowed, already contacted or listed): {skipped}. "
                  f"They wait for your approval.")


@prospects_app.command("india-import")
def india_import(cities: Optional[str] = typer.Option(None, help="Cities, comma separated; default: 36 major Indian cities.")):
    """Read Indian companies with a website, by headquarters city, from Wikidata."""
    from . import prospects_india
    con = db.connect()
    counts = prospects_india.import_companies(con, [c.strip() for c in cities.split(",")] if cities else None,
                                              on_city=lambda c, n: console.print(f"  {c}: {n}"))
    console.print(f"Stored {sum(counts.values())} companies in {sum(1 for n in counts.values() if n)} cities.")


@prospects_app.command("refresh")
def refresh():
    """Read the TWSE and TPEx company lists again: address -> city and district, industry, website, published email."""
    con = db.connect()
    with console.status("Reading the two exchange lists..."):
        counts = pr.refresh(con)
    console.print(f"Read: {counts}")
    summary(city=None, industry=None)


@prospects_app.command("summary")
def summary(city: Optional[str] = CityOpt, industry: Optional[str] = IndustryOpt):
    """How many companies per city, per district and per industry, and how many have an address to write to."""
    con = db.connect()
    rows = pr.select(con, city=city, industry=industry)
    by_city = Counter(r["city"] or "?" for r in rows)
    t = Table("City", "Companies", "Hiring address found", "Published address only", "Not looked up yet")
    for c, n in by_city.most_common():
        sub = [r for r in rows if (r["city"] or "?") == c]
        t.add_row(f"{c} {pr.CITY_EN.get(c, '')}", str(n), str(sum(bool(r["hr_email"]) for r in sub)),
                  str(sum(not r["hr_email"] and bool(r["published_email"]) for r in sub)),
                  str(sum(not r["email_checked_at"] for r in sub)))
    console.print(t)
    t = Table("District", "Companies")
    for d, n in Counter(f"{r['city'] or '?'} {r['district'] or '?'}" for r in rows).most_common(25):
        t.add_row(d, str(n))
    console.print(t)
    t = Table("Code", "Industry", "Fit tier", "Companies")
    for code, n in Counter((r["industry"] or "").zfill(2) for r in rows).most_common():
        zh, en, tier = pr.INDUSTRY.get(code, ("?", "?", 3))
        t.add_row(code, f"{zh} {en}", str(tier), str(n))
    console.print(t)


@prospects_app.command("list")
def list_(city: Optional[str] = CityOpt, district: Optional[str] = DistrictOpt, industry: Optional[str] = IndustryOpt,
          tier: Optional[int] = TierOpt, limit: int = typer.Option(50)):
    """The companies a filter picks, best fit first."""
    con = db.connect()
    t = Table("ID", "Company", "City", "District", "Industry", "Hiring address", "Published address")
    for r in pr.select(con, city=city, district=district, industry=industry, tier=tier, limit=limit):
        t.add_row(str(r["id"]), (r["name_zh"] or r["search"])[:20], r["city"] or "", r["district"] or "",
                  pr.INDUSTRY.get((r["industry"] or "").zfill(2), ("?",))[0], r["hr_email"] or "", r["published_email"] or "")
    console.print(t)


@prospects_app.command("find")
def find(city: Optional[str] = CityOpt, district: Optional[str] = DistrictOpt, industry: Optional[str] = IndustryOpt,
         tier: Optional[int] = TierOpt, limit: int = typer.Option(200, help="Most companies to look up in this run."),
         recheck: bool = typer.Option(False, help="Also look again at companies already looked up."),
         workers: int = typer.Option(12),
         search: bool = typer.Option(False, help="Also run a web search per company when its site shows no hiring address (slow)."),
         country: str = CountryOpt):
    """Read each company's own website for a hiring address (careers / recruiting / contact pages)."""
    con = db.connect()
    rows = [r for r in pr.select(con, city=city, district=district, industry=industry, tier=tier, not_checked=not recheck,
                                 country=country) if r["website"]][:limit]
    console.print(f"Looking up {len(rows)} company websites...")
    done = [0]

    def show(r, email, where):
        done[0] += 1
        if email:
            console.print(f"  {done[0]}/{len(rows)}  {(r['name_zh'] or r['search'])[:18]}  ->  {email}", markup=False)

    found = pr.find_many(con, rows, workers=workers, on_result=show, use_search=search)
    console.print(f"Hiring address found for {found} of {len(rows)} companies.")


def _resume(region="taiwan"):
    """The general resume (Gold Card only for Taiwan), printed once to a PDF the queue points at."""
    master = render.load_master()
    out = Path(HomePath("output")) / "prospects" / region / "Divakar_Upadhyay_Senior_Software_Engineer.pdf"
    html = render.to_html(render.build(region, master=master))
    render.check_region_rule(html, region)
    with render.PdfPrinter(load_config()["browser"]["channel"]) as pdf:
        pdf.save(html, out)
    return master, out


@prospects_app.command("queue")
def queue(city: Optional[str] = CityOpt, district: Optional[str] = DistrictOpt, industry: Optional[str] = IndustryOpt,
          tier: Optional[int] = TierOpt, limit: int = typer.Option(300),
          published: bool = typer.Option(False, help="Also approve emails to the address in the exchange record "
                                                     "(often investor relations). Default: they wait for you."),
          country: str = CountryOpt):
    """Write the email for each picked company that has an address, and put it in the send queue."""
    con = db.connect()
    rows = pr.select(con, city=city, district=district, industry=industry, tier=tier, need_email=True, limit=limit,
                     country=country)
    master, resume = _resume("abroad" if country.lower() == "india" else "taiwan")
    added, waiting, known = pr.queue(con, rows, master, resume, published_ok=published)
    console.print(f"Queued {added} ({added - waiting} approved, {waiting} waiting for you); {known} were queued before.")
    console.print("They go out with `jobbot send` / the loop, after the job applications, "
                  f"at most {(load_config().get('prospects') or {}).get('per_day_per_account', 20)} per account per day.")


@prospects_app.command("preview")
def preview(company_id: int = typer.Argument(..., help="Company id from `jobbot prospects list`.")):
    """Show the email one company would get."""
    con = db.connect()
    row = con.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if not row:
        raise typer.Exit("No such company.")
    if row["origin"] == "IN-Wikidata":
        from . import prospects_india
        subject, body = prospects_india.write(row, render.load_master(), "(your sending address)")
    else:
        subject, body = pr.write(row, render.load_master(), "(your sending address)")
    console.print(f"To: {row['hr_email'] or row['published_email'] or '(no address)'}\nSubject: {subject}\n\n{body}",
                  markup=False, highlight=False)


@prospects_app.command("approve")
def approve(published: bool = typer.Option(False, "--published", help="Approve the ones to exchange-record addresses."),
            services: bool = typer.Option(False, "--services", help="Approve the service offers to startups.")):
    """Approve waiting speculative applications."""
    con = db.connect()
    if services:
        postal = (load_config().get("services") or {}).get("postal_address")
        if not postal:
            raise typer.Exit("Set your postal address first (US law requires it in offers): "
                             "set services.postal_address on the admin page, http://127.0.0.1:8765/admin")
        from . import prospects_founders as pf, prospects_world as pw
        for r in con.execute("SELECT c.*, j.id AS job_id FROM jobs j JOIN companies c ON c.id=CAST(j.ext_id AS INTEGER) "
                             "WHERE j.source='services' AND j.send_status IS NULL").fetchall():
            subject, body = (pw if r["origin"] == pw.ORIGIN else pf).write(r, render.load_master(), postal)
            con.execute("UPDATE jobs SET email_subject=?, email_body=?, send_status='approved' WHERE id=?",
                        (subject, body, r["job_id"]))
        con.commit()
        console.print("Service offers approved with your postal address.")
        return
    cond = "" if published else " AND COALESCE(email_source,'') NOT LIKE 'CHECK%'"
    n = con.execute("UPDATE jobs SET send_status='approved' WHERE job_type='prospect' AND send_status IS NULL" + cond).rowcount
    con.commit()
    console.print(f"Approved: {n}")


@prospects_app.command("status")
def status():
    """Speculative applications by state."""
    con = db.connect()
    t = Table("State", "Count")
    for r in con.execute("SELECT COALESCE(send_status,'waiting for you') s, COUNT(*) FROM jobs WHERE job_type='prospect' "
                         "GROUP BY 1 ORDER BY 2 DESC"):
        t.add_row(r[0], str(r[1]))
    console.print(t)
    for r in con.execute("SELECT outcome, COUNT(*) FROM jobs WHERE job_type='prospect' AND outcome IS NOT NULL GROUP BY 1"):
        console.print(f"{r[0]}: {r[1]}")
