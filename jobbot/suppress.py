"""The do-not-contact list. Every account's sending checks it before each email.

An entry is an address (hr@acme.com) or a whole domain (@acme.com). Entries with no account apply to every
account: hard bounces and people who asked not to be written to again ("unsubscribe", "remove me"). Entries an
account adds itself apply to that account only. The suppressions table lives in the one database (control.py).
"""
import re

from . import control as ct
from . import db

OPT_OUT = re.compile(r"\b(unsubscribe|remove me|take me off|stop (e-?mailing|sending|contacting)|do not (e-?mail|contact|write)|"
                     r"don'?t (e-?mail|contact|write)|opt(ed)? out)\b", re.I)
REASONS = ("unsubscribed", "bounced", "complaint", "manual")


def _norm(address):
    a = (address or "").strip().lower()
    if a.startswith("@"):
        return a if re.fullmatch(r"@[a-z0-9.-]+\.[a-z]{2,}", a) else None
    return a if ct.EMAIL.match(a) else None


def blocked(account_id=None, con=None):
    """The set of addresses and @domains this account may not write to. Pass the caller's connection to read
    inside its own transaction."""
    account_id = account_id if account_id is not None else db.account_id()
    sql = "SELECT address FROM main.suppressions WHERE account_id IS NULL OR account_id=?"
    if con is not None:
        return {r[0] for r in con.execute(sql, (account_id,))}
    return {r["address"] for r in ct._all(sql, account_id)}


def is_blocked(address, block=None):
    a = (address or "").strip().lower()
    block = blocked() if block is None else block
    return a in block or ("@" + a.split("@")[-1]) in block


def add(address, reason, account_id=None, note=""):
    """Add one entry. account_id None = for every account. Returns the normalised entry, or None if not valid."""
    a = _norm(address)
    if not a or reason not in REASONS:
        return None
    ct._write("INSERT INTO suppressions(address, reason, account_id, added_at, note) VALUES(?,?,?,?,?) "
              "ON CONFLICT(address) DO UPDATE SET account_id=CASE WHEN excluded.account_id IS NULL THEN NULL ELSE account_id END, "
              "reason=CASE WHEN excluded.account_id IS NULL THEN excluded.reason ELSE reason END",
              a, reason, account_id, ct.now(), (note or "")[:300])
    return a


def remove(address, account_id=None):
    """Remove an entry. An account may only remove its own entries; the operator (account_id None) any."""
    a = (address or "").strip().lower()
    if account_id is None:
        return ct._write("DELETE FROM suppressions WHERE address=?", a)[0] == 1
    return ct._write("DELETE FROM suppressions WHERE address=? AND account_id=?", a, account_id)[0] == 1


def listing(account_id=None, limit=500):
    if account_id is None:
        return ct._all("SELECT * FROM suppressions ORDER BY added_at DESC LIMIT ?", limit)
    return ct._all("SELECT address, reason, added_at, note, account_id IS NULL AS everyone FROM suppressions "
                   "WHERE account_id IS NULL OR account_id=? ORDER BY added_at DESC LIMIT ?", account_id, limit)
