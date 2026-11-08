"""dispute_draft: practice disputes. A seller pastes a PP-SIM-… ID (the demo's Practice disputes tab) and reads a
checklist grouped by what to do with each line, and a draft response to paste in the Resolution Center. Every fact
comes from a fictional scenario in app/demo_disputes.json, in PayPal's own shapes (a dispute, a capture, its trackers),
never from PayPal: GoodToShip doesn't read real disputes (decided 2026-11-07).

No PayPal call, no database, no LLM, no interrupt(). Every word is this module's, a scenario value validated here, or
a verified policy_pack note quoted word for word. A draft sentence prints only when every fact it needs is ✓: what is
missing is listed, never written as a sentence. No line speaks of the outcome. The scenarios hold no buyer, so the
draft says "the buyer".
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from string import Formatter
from functools import cache
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app import policy_pack
from app.scam_check import TXN_ID, money, owns, sanitize

ENUM = re.compile(r"[A-Z_]{1,60}")
YES, NO, UNKNOWN = "✓", "✗", "?"
# PayPal's codes (its Disputes v1 spec), in the seller's words
REASONS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Item not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Item not as described",
    "UNAUTHORISED": "Unauthorized payment",
    "CREDIT_NOT_PROCESSED": "Credit not processed",
    "DUPLICATE_TRANSACTION": "Duplicate payment",
    "INCORRECT_AMOUNT": "Incorrect amount",
    "PAYMENT_BY_OTHER_MEANS": "Paid by other means",
    "CANCELED_RECURRING_BILLING": "Canceled recurring billing",
    "PROBLEM_WITH_REMITTANCE": "Problem with remittance",
    "OTHER": "Other reason",
}
KINDS = {"MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "inr", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "snad",
         "UNAUTHORISED": "unauth"}
CARD = {"EXTERNAL", "ALERT"}  # the card issuer or bank decides, not PayPal
CHANNELS = {"INTERNAL": "PayPal case", "EXTERNAL": "card or bank case", "ALERT": "pre-chargeback alert"}
# CHARGEBACK is a stage of an INTERNAL case: a PayPal claim, never a card chargeback (PayPal's spec)
STAGES = {"INQUIRY": "inquiry stage", "CHARGEBACK": "claim stage", "PRE_ARBITRATION": "first appeal stage",
          "ARBITRATION": "second appeal stage"}
STATUSES = {"OPEN": "open", "WAITING_FOR_BUYER_RESPONSE": "waiting for the buyer's response",
            "WAITING_FOR_SELLER_RESPONSE": "waiting for your response", "UNDER_REVIEW": "under review by PayPal",
            "RESOLVED": "resolved", "OTHER": "status other"}
DUE_SHOWN = {"OPEN", "WAITING_FOR_SELLER_RESPONSE"}  # "Respond by" only while the seller can still respond
REFUNDED = {"REFUNDED": "refunded", "PARTIALLY_REFUNDED": "partially refunded"}
ELIGIBLE = {"ELIGIBLE", "PARTIALLY_ELIGIBLE"}
# checklist()'s lines, by key: why each counts is in QUOTES
KEYS = {"protected": "Eligible for Seller Protection against unauthorized payments",
        "within_2": "Shipped no later than 2 days after the dispute opened",
        "tracking": "Tracking added to this payment", "validated": "Tracking number validated by the carrier",
        "before": "Shipped before the dispute opened",
        "address": "Shipped to the Transaction Details address: compare your carrier receipt",
        "refund": "Refund issued on this payment"}
# a tracker's shipment_date is a day in the seller's timezone, which PayPal doesn't give: from UTC-12 to UTC+14, so the
# shipment was between 14 h before that day's midnight UTC and 36 h after it
SHIPPED_FROM, SHIPPED_TO = timedelta(hours=-14), timedelta(hours=36)
UNAUTH_DAYS = timedelta(days=2)  # pp_unauthorized_claim: shipped no later than two days after PayPal's notice

SIM_ID = re.compile(r"PP-SIM-(?:INR|SNAD|UNAUTH|CARD)-[0-9]{2}")  # never chat.REAL_ID's PP-D- or PP-R-
SCENARIOS = {s["id"]: s for s in json.loads((Path(__file__).parent / "demo_disputes.json").read_text())["scenarios"]}
TIMES = ("create_time", "update_time", "seller_response_due_date")  # the dispute's hour offsets
NUMBER = re.compile(r"[A-Z0-9-]{4,40}")  # a tracking number
CARRIER_NAME = re.compile(r"[A-Za-z0-9 .&'-]{1,40}")
# sim_carrier's scans: GoodToShip's own fictional format, as PayPal has no carrier delivery events
EVENTS = {"PICKED_UP": "picked up", "IN_TRANSIT": "in transit", "DELIVERED": "delivered",
          "RETURNED": "returned to sender"}
# a SNAD buyer's codes, read from PayPal's Disputes v1 spec (paypal-rest-api-specifications,
# openapi/customer_disputes_v1.json, 1.11), not yet seen in a sandbox payload
SUB_REASONS = {"DAMAGED": "damaged", "DIFFERENT": "a different item", "MISSING_PARTS": "missing parts",
               "OTHER": "another problem"}
RECEIVED = {"YES": "yes", "NO": "no", "RETURNED": "returned"}
# pp_proof_of_shipment: a signature was required for transactions before 26 Jan 2026 over 750 USD, and is only
# recommended after. Noon UTC on the 27th is the 27th in every timezone (UTC-12 to UTC+14). No amount is read: a
# payment from before that date gets "?"
SIGNATURE_FROM = datetime(2026, 1, 27, 12, tzinfo=timezone.utc)
DRAFTED = ("inr", "unauth", "snad")

SIMULATED = "Simulated dispute: fictional data, for practice. Nothing here is from PayPal."
NOT_FOUND = "There is no practice dispute {}. The practice disputes are {}."
ASKS = "What PayPal asks you for:"
NO_ASKS_HERE = "PayPal hasn't listed what it asks for yet."  # never "check your Resolution Center": it isn't there
PAGES = "PayPal's pages for this list:"
GROUPS = {"ready": "Ready (✓): what you can send now",
          "missing": "Missing (✗): what's against you, or what you still need",
          "check": "Check yourself (?): GoodToShip can't see this"}
DRAFT_TO = {"buyer": "Draft message to the buyer, to paste in your Resolution Center:",
            "paypal": "Draft response to PayPal, to paste in your Resolution Center:"}
MISSING = "Missing before you send this:"
YOU_SEND = "You send it in PayPal: GoodToShip doesn't."
ANYTHING_ELSE = "Anything else I can help with?"
NO_DRAFT = {"card": "No draft for a card case.", "resolved": "No draft: this dispute is resolved.",
            "stage": "No draft for an appeal.", "other": "No draft for this reason yet."}
# the lines the carrier and the listing add to checklist()'s
DELIVERED = "Delivered"
AT_ADDRESS = "Delivered to the Transaction Details address"
SIGNATURE = "Signature"
NOT_COVERED = "Not covered by Seller Protection"
# why a line counts: a sentence of its note, word for word (U38 checks each against the note)
_TRACKING_QUOTE = ("pp_proof_of_delivery", "An online and verifiable tracking number.")
QUOTES = {
    "protected": ("pp_unauthorized_claim", "In the case of Unauthorized Transaction claims, the payment must be marked "
                  "“eligible” or “partially eligible” for PayPal’s Seller Protection program on the Transaction Details "
                  "page."),
    "within_2": ("pp_unauthorized_claim", "In the case of Unauthorized Transaction claim, you must provide valid proof of "
                 "shipment or proof of delivery that demonstrates that the item was shipped or provided to the buyer no "
                 "later than two days after PayPal notified you of the dispute or reversal."),
    "tracking": _TRACKING_QUOTE, "validated": _TRACKING_QUOTE,
    "delivered": ("pp_proof_of_delivery", "You provide PayPal with valid proof of shipment or delivery."),
    "address": ("pp_ship_to_transaction_address", "You must ship the item to the shipping address on the Transaction "
                "Details page in your PayPal account for the transaction."),
    "address_no": ("pp_ship_to_transaction_address", "If you originally ship the item to the recipient’s shipping "
                   "address on the Transaction Details page but the item is later redirected to a different address, "
                   "you will not be eligible for PayPal’s Seller Protection program."),
    "signature": ("pp_proof_of_shipment", "Transactions processed before January 26th, 2026, and over $750.00 USD (or "
                  "equivalent amounts in different currencies) require signature confirmations. For transactions after "
                  "this date, PayPal still recommends a signature confirmation although it's not mandatory."),
    "not_covered": ("pp_snad_not_covered", "The following items or transactions are not eligible for PayPal’s Seller "
                    "Protection program: Items where the buyer claims (either with PayPal or their card issuer) that the "
                    "item you sent isn’t what was ordered (referred to as a Significantly Not as Described claim)."),
}
# the draft: (the lines each sentence needs ✓, its text). {…} comes from the scenario; […] is the seller's to write
_TO_PAYPAL = [
    (("tracking",), "I shipped the item on {ship_date} with {carrier}, tracking number {number}."),
    (("delivered", "address"), "The carrier shows it was delivered on {delivered_date} to the address on the "
                               "Transaction Details page."),
    ((), "[Attach the carrier's receipt that shows the delivery address.]")]
DRAFTS = {
    ("inr", "buyer"): [
        ((), "Hello, thank you for getting in touch about your order."),
        (("tracking",), "I shipped it on {ship_date} with {carrier}, tracking number {number}."),
        (("delivered", "address"), "The carrier shows it was delivered on {delivered_date} to the address on the order."),
        ((), "[Add anything you want the buyer to know.]")],
    ("inr", "paypal"): _TO_PAYPAL,
    ("unauth", "paypal"): _TO_PAYPAL,  # never a message to the account holder: an unauthorized claim is PayPal's
    ("snad", "buyer"): [
        ((), "Hello, thank you for telling me about the problem with your order."),
        (("listing",), "My listing described the item as: \"{description}\""),
        ((), "[Explain how the item you sent matches this description.]"),
        ((), "[Add anything you want the buyer to know.]")],
    ("snad", "paypal"): [
        (("listing",), "My listing described the item as: \"{description}\""),
        ((), "[Explain how the item you sent matches this description, and attach photos taken before shipping.]"),
        (("returned",), "The item came back to me: the carrier shows it delivered on {return_date}, return tracking "
                        "number {return_number}."),
        (("returned",), "[Say what you found when you checked the returned item.]")],
}
# the lines "Missing before you send this" lists when they aren't ✓. A sentence on any other line is optional
# a value a sentence names, when the scenario lacks it: listed as missing, as its sentence goes
VALUES = {"ship_date": "Shipment date", "carrier": "Carrier", "number": "Tracking number",
          "delivered_date": "Delivery date", "description": "Your listing's description",
          "return_date": "Return delivery date", "return_number": "Return tracking number"}
COUNTS = {"inr": ("tracking", "delivered", "address"), "unauth": ("protected", "within_2", "tracking", "delivered", "address"),
          "snad": ("listing",)}


# ---- PayPal's shapes: a scenario's dispute, capture and trackers, each field validated ----

def _enum(v) -> str | None:
    return v if isinstance(v, str) and ENUM.fullmatch(v) else None


def _instant(v) -> datetime | None:
    """A PayPal time with its timezone, in UTC, or None."""
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) and len(v) <= 40 else None
    except ValueError:
        return None
    return t.astimezone(timezone.utc) if t and t.tzinfo else None


def _day(v) -> date | None:
    """A tracker's shipment_date, YYYY-MM-DD, or None."""
    try:
        return date.fromisoformat(v) if isinstance(v, str) and len(v) == 10 else None
    except ValueError:
        return None


def _list(v) -> list:
    return v if isinstance(v, list) else []


def keep(raw) -> dict:
    """The dispute without the buyer: PayPal's own shape with fewer fields, each validated and dropped if it fails.
    Never buyer, messages, notes, extensions (snad_codes() reads their codes alone), offer, items, supporting_info,
    documents, adjudications, money_movements, refund_details, links or the ID, which stays in the graph's state."""
    if not isinstance(raw, dict):
        return {}
    d = {}
    for k in ("create_time", "update_time", "seller_response_due_date"):
        if _instant(raw.get(k)):
            d[k] = raw[k]
    for k in ("reason", "status", "dispute_life_cycle_stage", "dispute_channel"):
        if _enum(raw.get(k)):
            d[k] = raw[k]
    if isinstance(a := raw.get("dispute_amount"), dict) and money(a.get("value"), a.get("currency_code")):
        d["dispute_amount"] = {"currency_code": a["currency_code"], "value": a["value"]}
    if txs := [{"seller_transaction_id": t["seller_transaction_id"]} for t in _list(raw.get("disputed_transactions"))
               if isinstance(t, dict) and isinstance(t.get("seller_transaction_id"), str)
               and TXN_ID.fullmatch(t["seller_transaction_id"])]:
        d["disputed_transactions"] = txs
    asked = []
    for e in _list(raw.get("evidences")):  # only what PayPal asks the seller for: never the buyer's evidence
        if isinstance(e, dict) and e.get("source") == "REQUESTED_FROM_SELLER" and _enum(e.get("evidence_type")):
            item = {"evidence_type": e["evidence_type"], "source": "REQUESTED_FROM_SELLER"}
            if isinstance(info := e.get("action_info"), dict) and isinstance(info.get("mandatory"), bool):
                item["action_info"] = {"mandatory": info["mandatory"]}
            asked.append(item)
    if asked:
        d["evidences"] = asked
    return d


def capture_fields(c: dict) -> dict:
    """A capture, as GET /v2/payments/captures/{id} returns it -> the checklist's transaction fields."""
    sp = c.get("seller_protection") or {}
    return {"status": _enum(c.get("status")), "protection": _enum(sp.get("status")),
            "categories": [x for x in _list(sp.get("dispute_categories")) if _enum(x)]}


def tracker_fields(trackers: list) -> dict:
    """Trackers, as GET /v1/shipping/trackers lists them -> the checklist's tracking fields."""
    # a cancelled tracker is no shipment (scam_check._shipped), nor is a return's
    return {"trackers": [{"date": str(day) if (day := _day(t.get("shipment_date"))) else None,
                          "validated": v if isinstance(v := t.get("tracking_number_validated"), bool) else None}
                         for t in trackers if t.get("status") != "CANCELLED" and t.get("shipment_direction") != "RETURN"]}


def _line(mark: str, key: str, detail: str | None = None) -> dict:
    return {"key": key, "mark": mark, "text": f"{KEYS[key]}: {detail}" if detail else KEYS[key]}


def _shipped(tracking: dict, opened: datetime | None, key: str, by: timedelta) -> dict:
    """Whether the first shipment came before `by` after the dispute opened: ✓ in every timezone the seller could be
    in, ✗ in none of them, ? when it depends on the timezone. ✗ too with no tracking."""
    if not tracking["trackers"]:
        return _line(NO, key, "no tracking in PayPal")
    days = [d for t in tracking["trackers"] if (d := _day(t["date"]))]
    if not days or not opened:
        return _line(UNKNOWN, key)
    first, deadline = min(days), opened + by
    midnight = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    mark = YES if midnight + SHIPPED_TO <= deadline else NO if midnight + SHIPPED_FROM >= deadline else UNKNOWN
    return _line(mark, key, f"shipped {first}, opened {opened:%Y-%m-%d %H:%M} UTC")


def checklist(kind: str, dispute: dict, transaction: dict, tracking: dict) -> list[dict]:
    """INR and UNAUTH: what the payment shows, each line ✓ (it shows it), ✗ (it shows it isn't so) or ? (it doesn't
    show it). Pure."""
    opened = _instant(dispute.get("create_time"))
    lines = []
    if kind == "unauth":
        if transaction.get("protection") in ELIGIBLE and "UNAUTHORIZED_TRANSACTION" in transaction["categories"]:
            lines.append(_line(YES, "protected"))
        elif transaction.get("protection") == "NOT_ELIGIBLE" or (transaction.get("protection") in ELIGIBLE
                                                                  and transaction["categories"]):
            lines.append(_line(NO, "protected"))
        else:
            lines.append(_line(UNKNOWN, "protected"))
        lines.append(_shipped(tracking, opened, "within_2", UNAUTH_DAYS))
    trackers = tracking["trackers"]
    validated = [t["validated"] for t in trackers]
    lines += [_line(YES if trackers else NO, "tracking"),
              _line(YES if True in validated else UNKNOWN if None in validated else NO, "validated")]
    if kind == "inr":
        lines.append(_shipped(tracking, opened, "before", timedelta(0)))
    lines.append(_line(UNKNOWN, "address"))  # the carrier's lines (_shipping_lines) replace it when they know
    if transaction.get("status") in REFUNDED:
        lines.append(_line(YES, "refund", REFUNDED[transaction["status"]]))
    elif transaction.get("status"):
        lines.append(_line(NO, "refund", "none"))
    else:
        lines.append(_line(UNKNOWN, "refund"))
    return lines


def readable(code: str) -> str:
    """PROOF_OF_FULFILLMENT -> Proof of fulfillment: no table of PayPal's 85 evidence types."""
    return code.replace("_", " ").capitalize()


def respond_by(d: dict, now: datetime) -> str | None:
    """"Respond by 2026-11-14 10:00 UTC (9 days)", or None when there's no deadline the seller can still meet."""
    if d.get("status") not in DUE_SHOWN or not (due := _instant(d.get("seller_response_due_date"))):
        return None
    left = due - now
    when = ("past due" if left < timedelta(0) else "less than a day" if left < timedelta(days=1)
            else "1 day" if left.days == 1 else f"{left.days} days")
    return f"Respond by {due:%Y-%m-%d %H:%M} UTC ({when})"


def status(d: dict) -> str | None:
    if not (s := d.get("status")):
        return None
    return STATUSES.get(s) or readable(s).lower()


def _header(dispute_id: str, d: dict, kind: str, now: datetime) -> str:
    first = f"Dispute {dispute_id}"
    if reason := d.get("reason"):
        first += f": {REASONS.get(reason) or readable(reason)}"
    stage = d.get("dispute_life_cycle_stage")
    if kind == "card" or not stage:  # a card case has no PayPal stage
        stage = None
    else:
        stage = STAGES.get(stage) or f"{readable(stage).lower()} stage"
    if what := ", ".join(x for x in (CHANNELS.get(d.get("dispute_channel")), stage, status(d)) if x):
        first += f". {what[:1].upper()}{what[1:]}"
    first += "."
    amount = money((a := d.get("dispute_amount") or {}).get("value"), a.get("currency_code"))
    due = respond_by(d, now)
    second = " ".join([*([f"Disputed: {amount}."] if amount else []), *([f"{due}."] if due else [])])
    return f"{first}\n{second}" if second else first


def _asks(d: dict) -> str:
    asked = {}
    for e in d.get("evidences", []):
        asked[e["evidence_type"]] = asked.get(e["evidence_type"]) or e.get("action_info", {}).get("mandatory") is True
    if not asked:
        return NO_ASKS_HERE
    return "\n".join([ASKS, *(f"- {readable(t)}{' (required)' if required else ''}" for t, required in asked.items())])


def now() -> datetime:  # the clock, replaced in the tests
    return datetime.now(timezone.utc)


# ---- the graph ----

class DraftIn(TypedDict):
    dispute_id: str


class State(DraftIn, total=False):
    found: str  # found or not_found
    dispute: dict | None  # keep()'s fields, plus snad_codes()
    kind: str | None  # inr, unauth, snad, card or other; None without a dispute
    transaction: dict  # capture_fields'
    tracking: dict  # tracker_fields'
    paid: str | None  # the capture's create_time
    shipments: dict  # {"forward", "return"}: {"number", "carrier", "date"} or None
    carrier: dict  # sim_carrier's scans per tracking number, oldest first
    listing: dict | None  # {"name", "price", "description"}
    checklist: list[dict]  # [{"key", "mark", "group", "text", "quote"}]
    draft: dict | None  # {"to", "sentences", "missing"} or {"none"}
    reply: str
    shown: list[str]  # the notes shown in full, for the chat's next "what now?"


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _at(hours, now: datetime) -> str | None:
    """An hour offset from now, as PayPal writes a time."""
    return (now + timedelta(hours=hours)).isoformat() if type(hours) is int else None


def _date(at: str) -> str:
    return str(_instant(at).date())


def snad_codes(raw: dict) -> dict:
    """What a SNAD buyer reports, PayPal's codes only, at PayPal's own path: keep() drops extensions whole, as they
    also hold the buyer's own words."""
    pd = _dict(_dict(_dict(raw.get("extensions")).get("merchandize_dispute_properties")).get("product_details"))
    codes = {}
    if subs := [x for x in _list(pd.get("sub_reasons")) if isinstance(x, str) and x in SUB_REASONS]:
        codes["sub_reasons"] = subs
    if isinstance(r := pd.get("product_received"), str) and r in RECEIVED:
        codes["product_received"] = r
    return {"extensions": {"merchandize_dispute_properties": {"product_details": codes}}} if codes else {}


def _reported(d: dict) -> dict:
    return d.get("extensions", {}).get("merchandize_dispute_properties", {}).get("product_details", {})


def _shipment(trackers: list, back: bool) -> dict | None:
    """The first shipment to the buyer, or back from them: its number, carrier and day, validated."""
    for t in trackers:
        if t.get("status") == "CANCELLED" or (t.get("shipment_direction") == "RETURN") != back:
            continue
        if isinstance(n := t.get("tracking_number"), str) and NUMBER.fullmatch(n):
            carrier = _enum(t.get("carrier"))
            if carrier == "OTHER":
                name = t.get("carrier_name_other")
                carrier = name if isinstance(name, str) and CARRIER_NAME.fullmatch(name) else None
            return {"number": n, "carrier": carrier, "date": t["shipment_date"]}
    return None


def _scans(raw, now: datetime) -> dict:
    out = {}
    for number, events in _dict(raw).items():
        if NUMBER.fullmatch(number):
            out[number] = sorted(({"event": e["event"], "at": at,
                                   **{k: e[k] for k in ("to_order_address", "signed") if isinstance(e.get(k), bool)}}
                                  for e in _list(events) if isinstance(e, dict) and e.get("event") in EVENTS
                                  and (at := _at(e.get("at"), now))), key=lambda e: e["at"])
    return out


def _listing(raw) -> dict | None:
    """The fictional catalog's listing, or None if any field fails."""
    raw = _dict(raw)
    price = money(_dict(raw.get("price")).get("value"), _dict(raw.get("price")).get("currency_code"))
    name, description = (sanitize(v) if isinstance(v := raw.get(k), str) else "" for k in ("name", "description"))
    if not price or not 0 < len(name) <= 60 or not 0 < len(description) <= 300:
        return None
    return {"name": name, "price": price, "description": description}


@owns("found", "dispute", "transaction", "tracking", "paid", "shipments", "carrier", "listing")
def load_scenario(state):
    """The scenario in PayPal's shapes, each field validated, its offsets turned into times from now."""
    did = state.get("dispute_id")
    if not (isinstance(did, str) and SIM_ID.fullmatch(did) and (s := SCENARIOS.get(did))):
        return {"found": "not_found", "dispute": None}
    t0 = now()
    raw = s["dispute"] | {k: _at(s["dispute"].get(k), t0) for k in TIMES}
    trackers = [t | {"shipment_date": str((t0 + timedelta(hours=h)).date()) if type(h := t.get("shipment_date")) is int
                     else None} for t in _list(s.get("trackers")) if isinstance(t, dict)]
    capture = _dict(s.get("capture"))
    return {"found": "found", "dispute": keep(raw) | snad_codes(raw), "transaction": capture_fields(capture),
            "tracking": tracker_fields(trackers), "paid": _at(capture.get("create_time"), t0),
            "shipments": {"forward": _shipment(trackers, False), "return": _shipment(trackers, True)},
            "carrier": _scans(s.get("sim_carrier"), t0), "listing": _listing(s.get("listing"))}


@owns("kind")
def classify_case(state):
    if not (d := state.get("dispute")):
        return {"kind": None}
    if d.get("dispute_channel") in CARD:  # whatever the reason
        return {"kind": "card"}
    kind = KINDS.get(d.get("reason"), "other")
    return {"kind": "other" if kind in ("inr", "unauth") and not d.get("disputed_transactions") else kind}


def _ln(key: str, mark: str, text: str, quote: str | None = None) -> dict:
    """A line and its group. A ✗ that is no gap (no refund, no return) has none, so it isn't shown."""
    group = "check" if mark == UNKNOWN else "ready" if mark == YES else None if key in ("refund", "returned") else "missing"
    return {"key": key, "mark": mark, "group": group, "text": text, "quote": quote}


def _delivered(forward: dict | None, scans: list[dict], delivered: dict | None) -> dict:
    if delivered:
        return _ln("delivered", YES, f"{DELIVERED}: the carrier shows it delivered on {_date(delivered['at'])}",
                   "delivered")
    if not forward:
        detail = "no tracking in PayPal"
    elif not scans:
        detail = "no scans from the carrier"
    elif scans[-1]["event"] == "RETURNED":
        detail = f"no, the carrier shows it returned to sender on {_date(scans[-1]['at'])}"
    else:
        detail = (f"no delivery scan. The last scan, on {_date(scans[-1]['at'])}, says "
                  f"{EVENTS[scans[-1]['event']]}")
    return _ln("delivered", NO, f"{DELIVERED}: {detail}", "delivered")


def _signature(paid: str | None, delivered: dict) -> dict:
    if not (t := _instant(paid)) or t < SIGNATURE_FROM:
        return _ln("signature", UNKNOWN, f"{SIGNATURE}: check whether this payment needs one", "signature")
    if delivered.get("signed"):
        return _ln("signature", YES, f"{SIGNATURE}: signed for on delivery", "signature")
    return _ln("signature", UNKNOWN, f"{SIGNATURE}: not signed for. Not required for a payment made after 26 Jan 2026",
               "signature")


def _shipping_lines(state, base: list[dict]) -> list[dict]:
    """INR and UNAUTH: checklist()'s lines, the tracking number added, and the carrier's delivery, address and
    signature where checklist() has only "compare your carrier receipt"."""
    forward = state["shipments"]["forward"]
    scans = state["carrier"].get(forward["number"], []) if forward else []
    delivered = next((e for e in scans if e["event"] == "DELIVERED"), None)
    lines = []
    for x in base:
        key, text = x["key"], x["text"]
        if key == "tracking" and x["mark"] == YES and forward:
            text += f": {forward['number']}" + (f", {forward['carrier']}" if forward["carrier"] else "")
        if key != "address":
            lines.append(_ln(key, x["mark"], text, key if key in QUOTES else None))
            continue
        lines.append(_delivered(forward, scans, delivered))
        if delivered and "to_order_address" in delivered:
            ok = delivered["to_order_address"]
            lines.append(_ln("address", YES if ok else NO, (f"{AT_ADDRESS}: the carrier shows it" if ok
                                          else "Not " + AT_ADDRESS[0].lower() + AT_ADDRESS[1:]
                                          + ": the carrier shows another address"),
                             "address" if ok else "address_no"))
        else:
            lines.append(_ln("address", x["mark"], text, "address"))
        if delivered:
            lines.append(_signature(state.get("paid"), delivered))
    return [x for x in lines if x["group"]]


def _snad_lines(state, base: list[dict]) -> list[dict]:
    """SNAD: no Seller Protection, so none of its proofs (tracking, address); the listing, the return, the refund."""
    lines = [_ln("not_covered", NO, NOT_COVERED, "not_covered")]
    if listing := state.get("listing"):
        lines.append(_ln("listing", YES, f"Your listing's description ({listing['name']}, {listing['price']}): "
                                         f"\"{listing['description']}\""))
    else:
        lines.append(_ln("listing", UNKNOWN, "Your listing's description: GoodToShip has no listing for this sale"))
    back = state["shipments"]["return"]
    home = next((e for e in state["carrier"].get(back["number"], []) if e["event"] == "DELIVERED"), None) if back else None
    if home:
        lines.append(_ln("returned", YES, f"Item returned: the carrier shows it delivered back to you on "
                                          f"{_date(home['at'])}, return tracking {back['number']}"))
    elif back or _reported(state["dispute"]).get("product_received") == "RETURNED":
        lines.append(_ln("returned", UNKNOWN, "Item returned: the carrier shows no delivery back to you yet"))
    lines += [_ln("refund", x["mark"], x["text"]) for x in base if x["key"] == "refund"]
    return [x for x in lines if x["group"]]


@owns("checklist")
def build_checklist(state):
    if (kind := state.get("kind")) not in DRAFTED:
        return {"checklist": []}
    base = checklist(kind, state["dispute"], state["transaction"], state["tracking"])
    return {"checklist": _snad_lines(state, base) if kind == "snad" else _shipping_lines(state, base)}


def _values(state) -> dict:
    forward, back = state["shipments"]["forward"] or {}, state["shipments"]["return"] or {}
    scans = state["carrier"]
    delivered = next((e for e in scans.get(forward.get("number"), []) if e["event"] == "DELIVERED"), None)
    home = next((e for e in scans.get(back.get("number"), []) if e["event"] == "DELIVERED"), None)
    return {"ship_date": forward.get("date"), "carrier": forward.get("carrier"), "number": forward.get("number"),
            "delivered_date": _date(delivered["at"]) if delivered else None,
            "description": (state.get("listing") or {}).get("description"),
            "return_date": _date(home["at"]) if home else None, "return_number": back.get("number")}


@owns("draft")
def build_draft(state):
    """Each sentence only when every line it needs is ✓ and every value it names is there; then what is missing."""
    if not (d := state.get("dispute")):
        return {"draft": None}
    kind, stage = state.get("kind"), d.get("dispute_life_cycle_stage")
    why = ("card" if kind == "card" else "other" if kind not in DRAFTED else "resolved" if d.get("status") == "RESOLVED"
           else "stage" if stage not in ("INQUIRY", "CHARGEBACK") else None)
    if why:
        return {"draft": {"none": NO_DRAFT[why]}}
    to = "buyer" if stage == "INQUIRY" and kind != "unauth" else "paypal"
    marks, values = {x["key"]: x["mark"] for x in state["checklist"]}, _values(state)
    sentences, lacking = [], {}
    for needs, text in DRAFTS[(kind, to)]:
        if all(marks.get(k) == YES for k in needs):
            if gaps := [f for _, f, _, _ in Formatter().parse(text) if f and not values[f]]:
                lacking |= dict.fromkeys(gaps)
            else:
                sentences.append(text.format(**values))
    missing = [f"{x['mark']} {x['text']}" for x in state["checklist"] if x["key"] in COUNTS[kind] and x["mark"] != YES]
    missing += [f"{UNKNOWN} {VALUES[f]}: not in this scenario" for f in lacking]
    return {"draft": {"to": to, "sentences": sentences, "missing": missing}}


def groups(lines: list[dict], verified: dict) -> list[dict]:
    """The checklist as the reply and the demo's page show it: ready, missing, check, each line with PayPal's own words
    on why it counts, once per quote. A quote whose note isn't verified, or no longer says it, isn't shown."""
    seen, out = set(), []
    for group, heading in GROUPS.items():
        rows = []
        for x in (x for x in lines if x["group"] == group):
            why = None
            if (q := QUOTES.get(x["quote"])) and q not in seen and q[0] in verified and q[1] in verified[q[0]]["text"]:
                seen.add(q)
                why = f"PayPal: \"{q[1]}\" ({verified[q[0]]['title']})"
            rows.append({"mark": x["mark"], "text": x["text"], "why": why})
        if rows:
            out.append({"group": group, "heading": heading, "lines": rows})
    return out


def _reports(d: dict) -> str | None:
    codes = _reported(d)
    said = [*([f"What the buyer reports: {', '.join(SUB_REASONS[x] for x in codes['sub_reasons'])}."]
              if codes.get("sub_reasons") else []),
            *([f"Item received: {RECEIVED[codes['product_received']]}."] if codes.get("product_received") else [])]
    return " ".join(said) or None


def render(state, now: datetime) -> tuple[str, list[str]]:
    """The reply and the notes shown in full. Pure: now is the clock."""
    did = state["dispute_id"] if SIM_ID.fullmatch(str(state.get("dispute_id"))) else "with that ID"
    if state.get("found") != "found" or not (d := state.get("dispute")):
        return "\n\n".join([SIMULATED, NOT_FOUND.format(did, ", ".join(SCENARIOS)), ANYTHING_ELSE]), []
    verified, kind, draft = policy_pack.verified(), state.get("kind"), state.get("draft") or {}
    parts = [SIMULATED, _header(did, d, kind, now)]
    if kind == "card":  # the card issuer decides: the header and its note
        shown = [k for k in ["pp_chargeback"] if k in verified]
        return "\n\n".join([*parts, draft["none"], *(policy_pack.note(verified[k]) for k in shown), ANYTHING_ELSE]), shown
    parts.append(_asks(d))
    if kind == "snad" and (reported := _reports(d)):
        parts.append(reported)
    parts += ["\n".join([g["heading"], *(f"{x['mark']} {x['text']}" + (f"\n  {x['why']}" if x["why"] else "")
                                         for x in g["lines"])]) for g in groups(state["checklist"], verified)]
    shown = []
    if "none" in draft:
        parts.append(draft["none"])
    else:
        parts.append(f"{DRAFT_TO[draft['to']]}\n{' '.join(draft['sentences'])}")
        if draft["missing"]:
            parts.append("\n".join([MISSING, *draft["missing"]]))
        shown = [k for k in ["pp_respond_to_dispute"] if k in verified]
        parts.append("\n".join([YOU_SEND, *(policy_pack.note(verified[k]) for k in shown)]))
    # the notes the list quotes, by title and page: not shown in full, as the seller hasn't read them
    quoted = dict.fromkeys(QUOTES[x["quote"]][0] for x in state["checklist"] if x["quote"] in QUOTES)
    if pages := [k for k in quoted if k in verified and k not in shown]:
        parts.append("\n".join([PAGES, *(f"{verified[k]['title']}: {verified[k]['source_url']}" for k in pages)]))
    return "\n\n".join([*parts, ANYTHING_ELSE]), shown


@owns("reply", "shown")
def write_reply(state):
    reply, shown = render(state, now())
    return {"reply": reply, "shown": shown}


def _build():
    g = StateGraph(State, input_schema=DraftIn)
    for node in (load_scenario, classify_case, build_checklist, build_draft, write_reply):
        g.add_node(node)
    g.add_edge(START, "load_scenario")
    g.add_edge("load_scenario", "classify_case")
    g.add_edge("classify_case", "build_checklist")
    g.add_edge("build_checklist", "build_draft")
    g.add_edge("build_draft", "write_reply")
    g.add_edge("write_reply", END)
    return g.compile()  # ponytail: no checkpointer, one short read of a file already in memory


graph = _build()


def run(dispute_id: str, report: Callable[[dict], None] | None = None) -> dict:
    """{reply, shown, kind, checklist}: the checklist grouped as the demo's page draws it. report, only from the
    demo (demo.chat_turn), gets each node's update, for the Practice tab's live panel."""
    out = {}
    for mode, chunk in graph.stream({"dispute_id": dispute_id}, stream_mode=["updates", "values"]):
        if mode == "values":
            out = chunk
        elif report:
            for node, update in chunk.items():
                report({"type": "node", "graph": "dispute_draft", "node": node, "update": update or {}})
    return {"reply": out["reply"], "shown": out["shown"], "kind": out.get("kind"),
            "checklist": groups(out.get("checklist") or [], policy_pack.verified())}


@cache  # the file is read once, at import: the 4 s poll gets the same list
def cards() -> list[dict]:
    """The demo's Practice disputes tab: each scenario's ID, title, story, reason and stage, from the file alone."""
    def stage(d):
        label = CHANNELS["EXTERNAL"] if d.get("dispute_channel") in CARD else STAGES.get(
            d.get("dispute_life_cycle_stage"), "")
        return label[:1].upper() + label[1:]
    return [{"id": s["id"], "title": s["title"], "story": s["story"],
             "reason": REASONS.get(s["dispute"].get("reason"), "Other reason"), "stage": stage(s["dispute"])}
            for s in SCENARIOS.values()]
