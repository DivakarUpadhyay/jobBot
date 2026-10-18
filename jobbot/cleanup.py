"""Remove generated files that are no longer needed, before each run.

Deleted:  resume PDFs of applications already sent, rejected or failed; every PDF folder from
          earlier days; the test / sample / check folders.
Kept:     the records of what was sent (database, Applications-Sent.xlsx, the date-wise CSVs).
          They are what stops the bot from emailing the same address twice, so they are never deleted.
A resume that is still waiting to be sent does not need its old PDF: it is rebuilt at send time.
"""
import shutil

from . import db
from .render import OUTPUT


def _inside_output(path):
    try:
        path.resolve().relative_to(OUTPUT.resolve())
        return path.resolve() != OUTPUT.resolve()
    except ValueError:
        return False


def _remove(folder):
    """Delete one folder under output/. Returns the number of PDF files removed."""
    if not folder.is_dir() or not _inside_output(folder):
        return 0
    n = sum(1 for _ in folder.rglob("*.pdf"))
    shutil.rmtree(folder, ignore_errors=True)
    return n


def run(con):
    """Returns (pdfs_deleted, folders_deleted)."""
    if not OUTPUT.exists():
        return 0, 0
    pdfs = folders = 0
    today = db.today()
    done = {str(r["id"]) for r in con.execute(
        "SELECT id FROM jobs WHERE send_status IN ('sent', 'rejected', 'failed')")}
    for day in [d for d in OUTPUT.iterdir() if d.is_dir()]:
        dated = len(day.name) == 10 and day.name[4] == "-" and day.name[7] == "-"
        if day.name in ("test", "base", "verify") or (dated and day.name < today):
            pdfs += _remove(day); folders += 1
        elif dated:   # today: only the jobs that are finished
            for job_dir in [j for j in day.iterdir() if j.is_dir() and j.name in done]:
                pdfs += _remove(job_dir); folders += 1
            if not any(day.iterdir()):
                day.rmdir()
    return pdfs, folders
