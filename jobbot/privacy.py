"""A customer's rights over their data (GDPR articles 15, 17 and 20; India's DPDP Act 2023): a full export, and
deletion of the account.

Export: everything kept about the account, as one JSON document, plus the names of its files.
Deletion: the account's rows and files are removed, its users and tokens deleted, and the bot stopped. Payment
records are kept with the account number only, because tax law requires invoices to be kept (in India for 8
years); the account row stays as an empty, closed placeholder so those records still point somewhere.
The owner's account (1) cannot be deleted this way.
"""
import shutil

from . import control as ct
from . import db, scheduler

ACCOUNT_ROWS = ("acct_settings", "acct_documents", "acct_prefs", "acct_meta", "acct_process_log")


def export(account_id):
    acc = ct.account(account_id)
    if not acc:
        raise LookupError("no such account")
    out = {"format": "JobPilot data export, version 1", "exported_at": ct.now(),
           "account": {k: acc[k] for k in acc if k not in ("home",)},
           "users": [{k: u[k] for k in u if k != "password_hash"}
                     for u in ct._all("SELECT * FROM users WHERE account_id=?", account_id)],
           "api_keys": ct._all("SELECT t.id, t.kind, t.name, t.created_at, t.expires_at, t.last_used_at FROM tokens t "
                               "JOIN users u ON u.id=t.user_id WHERE u.account_id=? AND t.kind='key'", account_id),
           "schedules": ct._all("SELECT * FROM schedules WHERE account_id=?", account_id),
           "runs": ct._all("SELECT id, trigger, status, note, requested_at, started_at, finished_at FROM cycles WHERE account_id=?",
                           account_id),
           "payments": ct._all("SELECT * FROM payments WHERE account_id=?", account_id),
           "tickets": ct._all("SELECT * FROM tickets WHERE account_id=?", account_id),
           "audit": ct._all("SELECT at, action, result, detail FROM audit WHERE account_id=?", account_id),
           "do_not_contact": ct._all("SELECT address, reason, added_at, note FROM suppressions WHERE account_id=?", account_id)}
    for table in ACCOUNT_ROWS:
        out[table.replace("acct_", "")] = ct._all(f"SELECT * FROM {table} WHERE account_id=?", account_id)
    cols = ", ".join(f"p.{c}" for c in ("title", "company", "location", "url", "posted_at")) + ", " + \
        ", ".join(f"a.{c}" for c in db.APP_COLS)
    out["applications"] = ct._all(f"SELECT a.id, {cols} FROM applications a JOIN postings p ON p.id=a.posting_id "
                                  f"WHERE a.account_id=? AND (a.status IN ('matched','sent') OR a.send_status IS NOT NULL)", account_id)
    home = ct.home_of(acc)
    out["files"] = sorted(str(p.relative_to(home)).replace("\\", "/") for p in home.rglob("*")
                          if p.is_file() and p.name != ".env") if home.exists() and account_id != 1 else []
    out["note"] = ("Mailbox passwords are not included. Job posts are shared listings scraped from public sites; only your own "
                   "decisions and applications about them are yours and are included.")
    return out


def delete(account_id, who=None):
    """Remove the account's data. Returns a summary of what was removed and what was kept."""
    if account_id == 1:
        raise ValueError("the owner's account cannot be deleted")
    acc = ct.account(account_id)
    if not acc or acc["status"] == "deleted":
        raise LookupError("no such account")
    for s in scheduler.schedules(account_id):
        ct._write("UPDATE schedules SET enabled=0 WHERE id=?", s["id"])
    for c in ct._all("SELECT id FROM cycles WHERE account_id=? AND status IN ('queued','running')", account_id):
        scheduler.stop(account_id, c["id"])
    removed = {}
    con = ct.connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        for table in ("applications", *ACCOUNT_ROWS, "schedules", "cycles", "tickets", "audit", "alerts", "suppressions"):
            removed[table] = con.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,)).rowcount
        removed["tokens"] = con.execute("DELETE FROM tokens WHERE user_id IN (SELECT id FROM users WHERE account_id=?)",
                                        (account_id,)).rowcount
        removed["users"] = con.execute("DELETE FROM users WHERE account_id=?", (account_id,)).rowcount
        con.execute("UPDATE accounts SET status='deleted', name='Deleted account', slug=?, contact_email=NULL WHERE id=?",
                    (f"deleted-{account_id}", account_id))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    home = ct.home_of(acc)
    if home.exists() and home != ct.home_of({"home": "."}):
        shutil.rmtree(home, ignore_errors=True)
    kept = ct._one("SELECT COUNT(*) n FROM payments WHERE account_id=?", account_id)["n"]
    from . import ops
    ops.event("account deleted", account=account_id, by=(who or {}).get("user_id"), removed=removed, payments_kept=kept)
    return {"deleted": True, "removed": removed, "payments_kept": kept,
            "why_kept": "payment records are kept for tax law, with no name or email attached"}
