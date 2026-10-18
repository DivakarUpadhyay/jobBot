"""Commands for running the service: backups, restore, alerts and health."""
import json
from pathlib import Path

import typer
from rich.console import Console

from . import ops
from .cli import app

console = Console()
backup_app = typer.Typer(no_args_is_help=True, help="Database backups: make, list, check, restore, and prove a restore works.")
app.add_typer(backup_app, name="backup")


@backup_app.command("now")
def backup_now():
    """Make a checked, compressed backup of the database now (the service keeps running)."""
    info = ops.backup("manual")
    console.print(f"Backup made: {info['file']} ({info['size_mb']} MB, {info['seconds']} s)", markup=False)
    if info["copied_to"]:
        console.print(f"Copied to: {info['copied_to']}", markup=False)


@backup_app.command("list")
def backup_list():
    """The kept backups, newest first."""
    files = ops.backups()
    if not files:
        console.print("No backups yet. Make one with: jobbot backup now")
    for f in files:
        console.print(f"{f.name}  {f.stat().st_size / 1e6:.1f} MB", markup=False)


@backup_app.command("verify")
def backup_verify(file: Path = typer.Argument(None, help="A backup file; default the newest")):
    """Check a backup: integrity check and row counts."""
    file = file or (ops.backups() or [None])[0]
    if not file:
        raise typer.Exit(console.print("No backups yet.") or 1)
    ok, detail, counts = ops.verify(file)
    console.print(f"{'OK' if ok else 'FAILED'}: {file}  {detail}  {json.dumps(counts)}", markup=False)
    raise typer.Exit(0 if ok else 1)


@backup_app.command("drill")
def backup_drill():
    """Restore the newest backup into a scratch folder and compare it with the live database."""
    r = ops.restore_drill()
    console.print(f"{'OK' if r['ok'] else 'FAILED'}: {r['backup']}: {r['note']}", markup=False)
    console.print(f"restored: {json.dumps(r['restored_counts'])}\nlive:     {json.dumps(r['live_counts'])}", markup=False)
    raise typer.Exit(0 if r["ok"] else 1)


@backup_app.command("restore")
def backup_restore(file: Path, yes: bool = typer.Option(False, "--yes", help="Do it without asking")):
    """Put a backup in place of the live database. Stop the loop and both sites first.
    The live file is kept beside it as jobbot.db.before-restore-<time>."""
    if not yes and not typer.confirm(f"Replace the live database with {file}?"):
        raise typer.Exit(1)
    r = ops.restore(file)
    console.print(f"Restored. The previous database is kept as {r['previous_kept_as']}", markup=False)


@app.command("alerts")
def alerts(resolve: int = typer.Option(None, help="Mark this alert id as dealt with")):
    """Open alerts: failed runs, sending accounts paused for bounces, old backups, a full disk."""
    if resolve:
        console.print("Resolved." if ops.resolve_alert(resolve) else "No such open alert.")
        return
    ops.check()
    rows = ops.open_alerts()
    if not rows:
        console.print("No open alerts.")
    for a in rows:
        console.print(f"#{a['id']}  {a['at']}  {a['kind']:<16} {a['message']}", markup=False, highlight=False)


@app.command("health")
def health():
    """Database, scheduler heartbeat, disk and backups."""
    ok, detail = ops.health(deep=True)
    console.print_json(json.dumps(detail))
    raise typer.Exit(0 if ok else 1)


secrets_app = typer.Typer(no_args_is_help=True, help="Encrypted storage of mailbox passwords.")
app.add_typer(secrets_app, name="secrets")


@secrets_app.command("encrypt")
def secrets_encrypt(owner: bool = typer.Option(False, help="Also encrypt the owner's own .env (DATA/.env)")):
    """Encrypt the mailbox app passwords in every customer's .env file (and with --owner, in yours).
    Back up DATA/.secret.key (or set JOBBOT_SECRET_KEY) first: without the key the passwords cannot be read."""
    from . import DATA, control as ct, secrets_box
    files = [ct.home_of(a) / ".env" for a in ct._all("SELECT * FROM accounts WHERE id != 1")]
    if owner:
        files.append(DATA / ".env")
    total = sum(secrets_box.seal_env_file(f) for f in files)
    console.print(f"{total} password(s) encrypted in {len(files)} file(s). Key: JOBBOT_SECRET_KEY or {DATA / '.secret.key'}",
                  markup=False)
