"""Payments for plans, through pluggable gateways.

A gateway is a row in control.db (`payment_gateways`: which markets it serves, on or off) plus an adapter
class here, chosen by the row's `kind`. Today the test gateway serves India and Taiwan: the whole purchase
flow runs and is recorded, but no money moves. To add a real one (Razorpay for India, ECPay for Taiwan,
Stripe elsewhere): write its adapter (start a checkout; verify the gateway's confirmation), register it in
ADAPTERS, put its keys in .env, and switch its row on in the admin panel.

A successful payment moves the account to the plan for 30 days and ends the trial.

Razorpay and Stripe use their hosted pages (no card data reaches this service). The customer comes back to
/payments/return, where the answer is verified (Razorpay: HMAC signature; Stripe: the session is read back from
Stripe), and each also sends a signed webhook (/webhooks/razorpay, /webhooks/stripe) so a payment is recorded even
when the customer closes the page. ECPay (Taiwan) is not written yet.
"""
import datetime as dt
import hashlib
import hmac
import json
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


ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP"}   # currencies with no minor unit


def _minor(amount, currency):
    return int(round(amount)) if currency.upper() in ZERO_DECIMAL else int(round(amount * 100))


def _return_url(payment, gateway):
    from .ops import env
    base = env("JOBBOT_PUBLIC_URL", "").rstrip("/")
    if not base:
        raise ValueError("JOBBOT_PUBLIC_URL is not set, so the payment page cannot send the customer back")
    return f"{base}/payments/return?gateway={gateway}&payment={payment['id']}"


class Razorpay(Gateway):
    """India: UPI, cards, net banking, through a Razorpay Payment Link (a hosted page, so no card data touches us).
    Keys in DATA/.env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, and RAZORPAY_WEBHOOK_SECRET for the webhook."""
    API = "https://api.razorpay.com/v1"

    def _keys(self):
        from .ops import env
        key, secret = env("RAZORPAY_KEY_ID"), env("RAZORPAY_KEY_SECRET")
        if not (key and secret):
            raise ValueError("Razorpay is not set up: RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are missing")
        return key, secret

    def ready(self):
        try:
            self._keys()
            return True
        except ValueError:
            return False

    def start(self, payment, gateway_row):
        import requests
        key, secret = self._keys()
        r = requests.post(f"{self.API}/payment_links", auth=(key, secret), timeout=30, json={
            "amount": _minor(payment["amount"], payment["currency"]), "currency": payment["currency"],
            "reference_id": f"jp-{payment['id']}", "description": f"JobPilot {payment['plan']} (30 days)",
            "callback_url": _return_url(payment, "razorpay"), "callback_method": "get",
            "notes": {"payment_id": str(payment["id"]), "account_id": str(payment["account_id"])}})
        data = r.json()
        if r.status_code >= 300 or not data.get("short_url"):
            raise ValueError(f"Razorpay refused the checkout: {(data.get('error') or {}).get('description') or r.status_code}")
        ct._write("UPDATE payments SET reference=? WHERE id=?", data["id"], payment["id"])   # the payment link id, until paid
        return {"mode": "redirect", "url": data["short_url"]}

    def confirm(self, payment, data):
        """The customer came back with Razorpay's signed answer (or the webhook said so: data['webhook'])."""
        if data.get("webhook"):
            return True, data["webhook"]
        _, secret = self._keys()
        fields = [data.get(k, "") for k in ("razorpay_payment_link_id", "razorpay_payment_link_reference_id",
                                             "razorpay_payment_link_status", "razorpay_payment_id")]
        expected = hmac.new(secret.encode(), "|".join(fields).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, data.get("razorpay_signature", "")):
            raise ValueError("the payment answer's signature does not match")
        if fields[0] != payment.get("reference") or fields[1] != f"jp-{payment['id']}":
            raise ValueError("the payment answer is for another payment")
        return fields[2] == "paid", fields[3]

    @staticmethod
    def webhook(raw, signature):
        """(payment id, Razorpay payment id) from a verified payment_link.paid event, or None."""
        from .ops import env
        secret = env("RAZORPAY_WEBHOOK_SECRET")
        if not secret or not hmac.compare_digest(hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), signature or ""):
            raise PermissionError("bad signature")
        event = json.loads(raw)
        if event.get("event") != "payment_link.paid":
            return None
        link = event["payload"]["payment_link"]["entity"]
        ref = link.get("reference_id") or ""
        return (int(ref[3:]), event["payload"]["payment"]["entity"]["id"]) if ref.startswith("jp-") else None


class Stripe(Gateway):
    """Cards and wallets in most countries, through Stripe Checkout (a hosted page).
    Keys in DATA/.env: STRIPE_SECRET_KEY, and STRIPE_WEBHOOK_SECRET for the webhook."""
    API = "https://api.stripe.com/v1"

    def _key(self):
        from .ops import env
        key = env("STRIPE_SECRET_KEY")
        if not key:
            raise ValueError("Stripe is not set up: STRIPE_SECRET_KEY is missing")
        return key

    def ready(self):
        try:
            self._key()
            return True
        except ValueError:
            return False

    def start(self, payment, gateway_row):
        import requests
        back = _return_url(payment, "stripe")
        r = requests.post(f"{self.API}/checkout/sessions", auth=(self._key(), ""), timeout=30, data={
            "mode": "payment", "client_reference_id": str(payment["id"]),
            "success_url": back + "&session_id={CHECKOUT_SESSION_ID}", "cancel_url": back + "&cancelled=1",
            "line_items[0][quantity]": 1, "line_items[0][price_data][currency]": payment["currency"].lower(),
            "line_items[0][price_data][unit_amount]": _minor(payment["amount"], payment["currency"]),
            "line_items[0][price_data][product_data][name]": f"JobPilot {payment['plan']} (30 days)",
            "metadata[payment_id]": str(payment["id"]), "metadata[account_id]": str(payment["account_id"])})
        data = r.json()
        if r.status_code >= 300 or not data.get("url"):
            raise ValueError(f"Stripe refused the checkout: {(data.get('error') or {}).get('message') or r.status_code}")
        ct._write("UPDATE payments SET reference=? WHERE id=?", data["id"], payment["id"])
        return {"mode": "redirect", "url": data["url"]}

    def confirm(self, payment, data):
        """Asks Stripe itself whether the session was paid; what the browser says is never trusted."""
        if data.get("webhook"):
            return True, data["webhook"]
        import requests
        session_id = data.get("session_id") or ""
        if session_id != payment.get("reference"):
            raise ValueError("the payment answer is for another payment")
        s = requests.get(f"{self.API}/checkout/sessions/{session_id}", auth=(self._key(), ""), timeout=30).json()
        ok = (s.get("payment_status") == "paid" and s.get("client_reference_id") == str(payment["id"])
              and s.get("amount_total") == _minor(payment["amount"], payment["currency"]))
        return ok, s.get("payment_intent") or session_id

    @staticmethod
    def webhook(raw, signature, tolerance=300):
        """(payment id, reference) from a verified checkout.session.completed event, or None."""
        import time
        from .ops import env
        secret = env("STRIPE_WEBHOOK_SECRET")
        parts = dict(p.split("=", 1) for p in (signature or "").split(",") if "=" in p)
        stamp = parts.get("t", "")
        good = [v for k, v in (p.split("=", 1) for p in (signature or "").split(",") if "=" in p) if k == "v1"]
        expected = hmac.new((secret or "").encode(), f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest() if secret else ""
        if not secret or not any(hmac.compare_digest(expected, g) for g in good) or abs(time.time() - int(stamp or 0)) > tolerance:
            raise PermissionError("bad signature")
        event = json.loads(raw)
        if event.get("type") != "checkout.session.completed":
            return None
        s = event["data"]["object"]
        if s.get("payment_status") != "paid" or not str(s.get("client_reference_id") or "").isdigit():
            return None
        return int(s["client_reference_id"]), s.get("payment_intent") or s["id"]


ADAPTERS = {"dummy": TestGateway(), "razorpay": Razorpay(), "ecpay": NotSetUp(), "stripe": Stripe()}


def from_gateway(payment_id, data):
    """A gateway's answer that arrives without the customer signed in (the return page or a webhook)."""
    payment = ct._one("SELECT * FROM payments WHERE id=?", payment_id)
    if not payment:
        return None
    if payment["status"] == "paid":
        return payment   # the webhook and the return page both arrive; the second is a no-op
    return confirm(payment["account_id"], payment_id, data)


def _ready(kind):
    adapter = ADAPTERS.get(kind)
    if adapter is None or isinstance(adapter, NotSetUp):
        return False
    return adapter.ready() if hasattr(adapter, "ready") else True


def gateways(market=None, include_inactive=False):
    rows = ct._all("SELECT * FROM payment_gateways ORDER BY position, code")
    return [g | {"markets": g["markets"].split(","), "adapter_ready": _ready(g["kind"])}
            for g in rows if (include_inactive or g["active"]) and (not market or market in g["markets"].split(","))]


def gateway_update(code, body):
    if not ct._one("SELECT 1 FROM payment_gateways WHERE code=?", code):
        return None
    if body.get("active") and not _ready(ct._one("SELECT kind FROM payment_gateways WHERE code=?", code)["kind"]):
        raise ValueError("this gateway is not set up (adapter or keys missing), so it cannot be switched on")
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
