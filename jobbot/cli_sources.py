"""Console commands that grow the source list: add any site, or let the bot find sites for a region."""
from typing import Optional
from urllib.parse import urlparse

import typer
from rich.table import Table

from . import db, discover as dc, filter as flt, sourcestore
from .cli import console, source_app
from .config import load_config, load_sources, set_source_enabled
from .sources import generic

added_all = []   # names of sources added by discovery during this process, newest last

HOW = {"feed": "RSS feed", "jsonld": "job data in the pages", "browser": "job data read in a Chrome session", "greenhouse": "Greenhouse hiring system",
       "lever": "Lever hiring system", "ashby": "Ashby hiring system", "workable": "Workable hiring system"}


def _samples(jobs, cfg, n=5):
    t = Table("Title", "Company", "Where", "Posted", "For you")
    for job in jobs[:n]:
        v = flt.judge(job, cfg)
        t.add_row(job.title[:46], job.company[:22], job.location[:22], job.posted_at, v["skip"] or f"score {v['score']}")
    console.print(t)


@source_app.command("add")
def source_add(url: str = typer.Argument(..., help="Job site, company careers page or RSS feed."),
               region: str = typer.Option("Remote", help="Taiwan, Remote, or a country name."),
               name: Optional[str] = typer.Option(None, help="Short name; default comes from the site address."),
               browser: bool = typer.Option(False, help="Also try a real Chrome session if plain reading fails."),
               yes: bool = typer.Option(False, "--yes", "-y", help="Save without asking.")):
    """Add an external source. The bot works out how to read it and shows sample jobs first."""
    cfg = load_config()
    name = name or dc.slug(urlparse(url).netloc.removeprefix("www.").rsplit(".", 1)[0] or url)
    with console.status(f"Working out how to read {url} ..."):
        params, jobs = generic.detect(url, name, region, use_browser=browser)
    if not params:
        console.print("[yellow]Cannot read this site.[/yellow] It has no feed, no known hiring system and no standard job "
                      "data in its pages, or it blocks automated reading (login, bot check, content loaded by script).")
        raise typer.Exit(1)
    console.print(f"Readable through: {HOW[params['kind']]}. Jobs found: {len(jobs)}", markup=False)
    _samples(jobs, cfg)
    if not yes and not typer.confirm("Add this source?", default=True):
        raise typer.Exit(0)
    saved = dc.add_source(name, url, region, params, enabled=True, note=f"added by you: {url}")
    console.print(f"Added '{saved}' ({region}). It is read from the next run on.  Switch off: jobbot source off {saved}")


@source_app.command("discover")
def source_discover(region: Optional[str] = typer.Argument(None, help="One country or region. Default: the next countries in rotation."),
                    countries: int = typer.Option(3, help="How many countries to check when no region is given."),
                    no_search: bool = typer.Option(False, help="Only try the built-in catalogue, no web search.")):
    """Find new job sources country by country. Every site is test-read first; the working ones are
    saved as sources under that country. All countries are covered in rotation, a few per run."""
    cfg, con = load_config(), db.connect()
    auto = bool((cfg.get("sources") or {}).get("auto_approve"))
    todo = [region] if region else dc.next_countries(con, countries, (cfg.get("sources") or {}).get("recheck_days", 30))
    if not todo:
        console.print("Every country has been checked recently. Nothing to do.")
        return
    total, all_names, search_down = 0, [], False
    for reg in todo:
        console.rule(f"Looking for job sources: {reg}")
        tried, names = [], []
        log = lambda host, n, how: (tried.append(host), console.print(
            f"  {host:<36} {('%d jobs' % n) if n else '-':<10} {how}", markup=False, highlight=False))
        for f in dc.discover(reg, cfg, use_search=not no_search, log=log):
            saved = dc.add_source(f["name"], f["url"], reg, f["params"], enabled=auto,
                                  note=f"{'found' if auto else 'PROPOSED'} by discovery ({f['origin']}): {f['url']}")
            names.append(saved)
            console.print(f"[green]{'Added' if auto else 'Proposed'}: {saved}[/green] via {HOW[f['params']['kind']]}, "
                          f"{len(f['jobs'])} jobs")
            _samples(f["jobs"], cfg, n=3)
        if not tried and not no_search:
            # The web search gave nothing at all (it limits how often it can be asked). The country
            # is not marked as checked, so it is tried again on a later run.
            console.print(f"  The web search returned nothing for {reg} right now; it will be tried again later.")
            search_down = True
            break
        dc.record(con, reg, len(tried), names)
        total += len(names)
        all_names += names
        if not names:
            console.print(f"  No readable job source found for {reg} ({len(tried)} sites tried).")
    added_all.extend(n for n in all_names)
    left = len(dc.next_countries(con, 10000, 36500)) if not region else None
    console.print(f"New sources this run: {total}." + (f" Countries not checked yet: {left}." if left is not None else "")
                  + ("" if auto or not total else " They are switched off until you run: jobbot source approve <name>"))


@source_app.command("countries")
def source_countries(pending: bool = typer.Option(False, help="Show only countries not checked yet.")):
    """Every country and what discovery found there."""
    con = db.connect()
    dc.ensure_table(con)
    done = {r["region"]: r for r in con.execute("SELECT * FROM discovery")}
    active = {}
    for src in load_sources():
        if src.get("enabled"):
            active.setdefault(src.get("region", ""), []).append(str(src["name"]))
    t = Table("Country / region", "Checked on", "Sites tried", "Sources in use")
    for c in dc.all_countries():
        d = done.get(c)
        if pending and d:
            continue
        t.add_row(c, d["checked_at"][:10] if d else "not yet", str(d["tried"]) if d else "", ", ".join(active.get(c, []))[:70])
    console.print(t)
    console.print(f"Checked: {len(done)} of {len(dc.all_countries())} countries.")


@source_app.command("probe")
def source_probe(region: Optional[str] = typer.Argument(None, help="Only this country or region of the candidate sites."),
                 browser: bool = typer.Option(False, help="Also try a real Chrome session for sites that load by script (slow).")):
    """Test every candidate site and add the ones the bot can really read. Every site's result,
    with the reason when it cannot be read, is saved with the candidate and in reports/source-probe.csv."""
    import csv
    from . import home
    cfg = load_config()
    auto = bool((cfg.get("sources") or {}).get("auto_approve"))
    seen_hosts, added = set(), []
    log = lambda r: console.print(f"  {r['state']:<16} {r['region'][:22]:<23} {r['url'][:70]:<71} {r['how'][:44]}",
                                  markup=False, highlight=False)
    with console.status("Testing candidate sites..."):
        results = dc.probe_all(cfg, only_region=region, use_browser=browser)
    for r in results:
        log(r)
        if r["state"] != "readable":
            continue
        host = urlparse(r["url"]).netloc
        if host in seen_hosts:   # two search pages of one site: keep the first that worked
            r["state"] = "readable (same site already added)"
            continue
        seen_hosts.add(host)
        r["name"] = dc.add_source(r["name"], r["url"], r["region"], r["params"], enabled=auto,
                                  note=f"from your list ({'in use' if auto else 'PROPOSED'}): {r['url']}")
        added.append(r["name"])
    sourcestore.record_probe(results)
    path = home() / "reports" / (f"source-probe-{dc.slug(region)}.csv" if region else "source-probe.csv")
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Country / region", "Site", "Result", "How it is read / why not", "Jobs in the test read", "Source name"])
        for r in sorted(results, key=lambda x: (x["state"] != "readable", x["region"])):
            w.writerow([r["region"], r["url"], r["state"], r["how"], r["jobs"], r.get("name", "")])
    counts = {}
    for r in results:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    console.print(f"Tested {len(results)} sites: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    console.print(f"Added {len(added)} sources. Full list with reasons: {path}", markup=False, highlight=False)
    added_all.extend(added)


@source_app.command("health")
def source_health(fix: bool = typer.Option(False, help="Switch off sources that fail or return nothing.")):
    """Read every switched-on source once and show which work. With --fix, broken ones are switched off."""
    from concurrent.futures import ThreadPoolExecutor
    from .cli import _fetch
    cfg = load_config()
    todo = [s for s in load_sources() if s.get("enabled")]

    def read(src):
        try:
            return src, len(_fetch(src, cfg)), ""
        except Exception as e:
            return src, 0, f"{type(e).__name__}: {str(e)[:80]}"

    with console.status(f"Reading {len(todo)} sources..."), ThreadPoolExecutor(6) as pool:
        results = list(pool.map(read, todo))
    t = Table("Source", "Country / region", "Jobs read", "State")
    bad = []
    for src, n, err in sorted(results, key=lambda x: (x[1] > 0, str(x[0]["name"]))):
        state = "[green]working[/green]" if n else f"[red]{err or 'returned no jobs'}[/red]"
        if not n:
            bad.append(str(src["name"]))
        t.add_row(str(src["name"]), src.get("region", ""), str(n), state)
    console.print(t)
    working = len(results) - len(bad)
    console.print(f"{working} of {len(results)} switched-on sources are working ({100 * working // max(len(results), 1)}%).")
    if bad and fix:
        for name in bad:
            set_source_enabled(name, False)
        console.print(f"Switched off: {', '.join(bad)}. They are not deleted; `jobbot source on <name>` turns one back on.")
    elif bad:
        console.print(f"Not working: {', '.join(bad)}. Run again with --fix to switch them off.")


@source_app.command("approve")
def source_approve(names: list[str]):
    """Switch on sources that discovery proposed."""
    for n in names:
        console.print(f"{n}: on" if set_source_enabled(n, True) else f"No source named {n}")


@source_app.command("remove")
def source_remove(name: str):
    """Delete a source you added or that discovery proposed."""
    done = sourcestore.remove(name) == "removed"
    console.print(f"Removed {name}." if done else f"'{name}' is not an added source (built-in ones can only be switched off).")


@source_app.command("all-on")
def source_all_on():
    """Switch every source on."""
    names = sourcestore.set_all_enabled(True)
    console.print(f"Switched on: {', '.join(names)}." if names else "Every source was already on.")
