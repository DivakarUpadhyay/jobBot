"""Payments for plans, through pluggable gateways.

A gateway is a row in control.db (`payment_gateways`: which markets it serves, on or off) plus an adapter
class here, chosen by the row's `kind`. Today the test gateway serves India and Taiwan: the whole purchase
flow runs and is recorded, but no money moves. To add a real one (Razorpay for India, ECPay for Taiwan,
Stripe elsewhere): write its adapter (start a checkout; verify the gateway's confirmation), register it in
ADAPTERS, put its keys in .env, and switch its row on in the admin panel.

A successful payment moves the account to the plan for 30 days and ends the trial.
"""
import datetime as dt
import secrets

from . import control as ct

PERIOD_DAYS = 30


class Gateway:
    """What every adapter provides."""
    test = False

    def start(self, payment, gateway_row):
        """Begin a checkout. Returns what the browser needs next (a page to open, or test buttons)."""
        raise NotImplementedError

    def confirm(self, payment, data):
        """Check the gateway's answer. Returns (paid: bool, reference: str)."""
        raise NotImplementedError


class TestGateway(Gateway):
    """No money moves: the customer presses "pay" or "fail" on a test screen."""
    test = True

    def start(self, payment, gateway_row):
        return {"mode": "test", "message": "Test payment: no money will be taken.", "payment_id": payment["id"]}

    def confirm(self, payment, data):
        if data.get("result") == "success":
            return True, "TEST-" + secrets.token_hex(6).upper()
        return False, ""


class NotSetUp(Gateway):
    """A real gateway whose adapter is not written yet."""

    def start(self, payment, gateway_row):
        raise ValueError(f"{gateway_row['name']} is not set up yet")

    def confirm(self, payment, data):
        return False, ""


ADAPTERS = {"dummy": TestGateway(), "razorpay": NotSetUp(), "ecpay": NotSetUp(), "stripe": NotSetUp()}


def gateways(market=None, include_inactive=False):
    rows = ct._all("SELECT * FROM payment_gateways ORDER BY position, code")
    return [g | {"markets": g["markets"].split(","), "adapter_ready": not isinstance(ADAPTERS.get(g["kind"]), (NotSetUp, type(None)))}
            for g in rows if (include_inactive or g["active"]) and (not market or market in g["markets"].split(","))]


def gateway_update(code, body):
    if not ct._one("SELECT 1 FROM payment_gateways WHERE code=?", code):
        return None
    if body.get("active") and isinstance(ADAPTERS.get(ct._one("SELECT kind FROM payment_gateways WHERE code=?", code)["kind"]), NotSetUp):
        raise ValueError("this gateway's adapter is not written yet, so it cannot be switched on")
    markets = body.get("markets")
    if markets is not None:
        known = {m["code"] for m in ct._all("SELECT code FROM markets")}
        markets = [m.strip().upper() for m in (markets if isinstance(markets, list) else str(markets).split(",")) if m.strip()]
        if not markets or any(m not in known for m in markets):
            raise ValueError("markets must be known market codes")
    ct._write("UPDATE payment_gateways SET active=COALESCE(?, active), markets=COALESCE(?, markets), note=COALESCE(?, note) WHERE code=?",
              None if "active" not in body else int(bool(body["active"])), ",".join(markets) if markets else None, body.get("note"), code)
    return next(g for g in gateways(include_inactive=True) if g["code"] == code)


def checkout(account, plan_name, gateway_code=None):
    plan = ct._one("SELECT * FROM plans WHERE name=? AND active=1", plan_name)
    if not plan or plan["market"] != account["market"]:
        raise ValueError("that plan is not on sale in your country")
    if plan["price_month"] <= 0:
        raise ValueError("that plan is free; nothing to pay")
    choices = gateways(account["market"])
    gw = next((g for g in choices if g["code"] == gateway_code), None) if gateway_code else (choices[0] if choices else None)
    if not gw:
        raise ValueError("no payment method is available in your country yet")
    _, payment_id = ct._write("INSERT INTO payments(account_id, plan, amount, currency, gateway, status, created_at) VALUES(?,?,?,?,?,'created',?)",
                              account["id"], plan["name"], plan["price_month"], plan["currency"], gw["code"], ct.now())
    payment = ct._one("SELECT * FROM payments WHERE id=?", payment_id)
    return {"payment": payment, "gateway": {k: gw[k] for k in ("code", "name", "test_mode")},
            "next": ADAPTERS[gw["kind"]].start(payment, gw)}


def confirm(account_id, payment_id, data):
    payment = ct._one("SELECT * FROM payments WHERE id=? AND account_id=?", payment_id, account_id)
    if not payment:
        return None
    if payment["status"] != "created":
        raise ValueError(f"this payment is already {payment['status']}")
    gw = ct._one("SELECT * FROM payment_gateways WHERE code=?", payment["gateway"])
    adapter = ADAPTERS.get(gw["kind"]) if gw else None
    if not adapter:
        raise ValueError("unknown payment method")
    paid, reference = adapter.confirm(payment, data)
    if not paid:
        ct._write("UPDATE payments SET status='failed', note=? WHERE id=?", "declined" + (" (test)" if adapter.test else ""), payment_id)
        return ct._one("SELECT * FROM payments WHERE id=?", payment_id)
    acc = ct.account(account_id)
    start = max(dt.datetime.now(dt.timezone.utc), dt.datetime.fromisoformat(acc["paid_until"]) if acc.get("paid_until") else dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    until = (start + dt.timedelta(days=PERIOD_DAYS)).isoformat(timespec="seconds")
    ct._write("UPDATE payments SET status='paid', reference=?, paid_at=?, period_end=? WHERE id=?", reference, ct.now(), until, payment_id)
    ct._write("UPDATE accounts SET plan=?, trial_ends_at=NULL, paid_until=? WHERE id=?", payment["plan"], until, account_id)
    return ct._one("SELECT * FROM payments WHERE id=?", payment_id)


def history(account_id=None, limit=500):
    if account_id:
        return ct._all("SELECT * FROM payments WHERE account_id=? ORDER BY id DESC LIMIT ?", account_id, limit)
    return ct._all("SELECT p.*, a.name account_name FROM payments p JOIN accounts a ON a.id=p.account_id ORDER BY p.id DESC LIMIT ?", limit)


def collected(days=30):
    """Money actually received in the last N days, per currency (test payments listed apart)."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    out = {}
    for r in ct._all("SELECT p.currency, p.amount, g.test_mode FROM payments p JOIN payment_gateways g ON g.code=p.gateway "
                     "WHERE p.status='paid' AND p.paid_at >= ?", since):
        key = r["currency"] + (" (test)" if r["test_mode"] else "")
        out[key] = out.get(key, 0) + r["amount"]
    return out
