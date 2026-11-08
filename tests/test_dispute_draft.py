"""dispute_draft, offline: the practice disputes of app/demo_disputes.json. Every test fails on a PayPal call or a
database read, and checks that respx saw none. The clock is 2026-11-05 00:00 UTC."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import chat, db, dispute_draft, paypal, policy_pack, scam_check
from app.dispute_draft import NO, UNKNOWN, YES

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
CAP = "5DA60250XD148261W"  # a capture ID, as a scenario's seller_transaction_id

IDS = list(dispute_draft.SCENARIOS)
RAW = json.loads((Path(dispute_draft.__file__).parent / "demo_disputes.json").read_text())
KIND = {"INR": "inr", "SNAD": "snad", "UNAUTH": "unauth", "CARD": "card"}
BUYER_KEYS = {"buyer", "payer", "messages", "notes", "supporting_info", "email", "communication_details", "documents"}
MARKS = {
    "PP-SIM-INR-01": [("tracking", YES), ("validated", YES), ("before", YES), ("delivered", YES), ("address", YES),
                      ("signature", UNKNOWN)],
    "PP-SIM-INR-02": [("tracking", YES), ("validated", YES), ("before", YES), ("delivered", YES), ("address", YES),
                      ("signature", YES)],
    "PP-SIM-INR-03": [("tracking", YES), ("validated", YES), ("before", YES), ("delivered", NO), ("address", UNKNOWN)],
    "PP-SIM-INR-04": [("tracking", NO), ("validated", NO), ("before", NO), ("delivered", NO), ("address", UNKNOWN)],
    "PP-SIM-SNAD-05": [("not_covered", NO), ("listing", YES)],
    "PP-SIM-SNAD-06": [("not_covered", NO), ("listing", YES), ("returned", YES)],
    "PP-SIM-UNAUTH-07": [("protected", YES), ("within_2", YES), ("tracking", YES), ("validated", YES),
                         ("delivered", YES), ("address", YES), ("signature", UNKNOWN)],
    "PP-SIM-UNAUTH-08": [("protected", NO), ("within_2", NO), ("tracking", YES), ("validated", YES),
                         ("delivered", YES), ("address", YES), ("signature", UNKNOWN)],
    "PP-SIM-CARD-09": [],
    "PP-SIM-INR-10": [("tracking", YES), ("validated", YES), ("before", YES), ("delivered", YES), ("address", NO),
                      ("signature", UNKNOWN)],
}
ATTACH = "[Attach the carrier's receipt that shows the delivery address.]"
LAMP = ("Hand-glazed ceramic table lamp, 40 cm tall, with a linen shade. New and unused, packed in its original box with "
        "foam corners.")
BAG = "Brown full-grain leather messenger bag, 38 x 28 cm, with a padded laptop sleeve and brass buckles."
DRAFTS = {
    "PP-SIM-INR-01": {"to": "buyer", "missing": [], "sentences": [
        "Hello, thank you for getting in touch about your order.",
        "I shipped it on 2026-11-01 with Practice Carrier, tracking number SIM-TRK-0001.",
        "The carrier shows it was delivered on 2026-11-02 to the address on the order.",
        "[Add anything you want the buyer to know.]"]},
    "PP-SIM-INR-02": {"to": "paypal", "missing": [], "sentences": [
        "I shipped the item on 2026-10-20 with Practice Carrier, tracking number SIM-TRK-0002.",
        "The carrier shows it was delivered on 2026-10-22 to the address on the Transaction Details page.", ATTACH]},
    "PP-SIM-INR-03": {"to": "paypal", "sentences": [
        "I shipped the item on 2026-10-24 with Practice Carrier, tracking number SIM-TRK-0003.", ATTACH], "missing": [
        "✗ Delivered: no delivery scan. The last scan, on 2026-10-25, says in transit",
        "? Shipped to the Transaction Details address: compare your carrier receipt"]},
    "PP-SIM-INR-04": {"to": "buyer", "sentences": [
        "Hello, thank you for getting in touch about your order.", "[Add anything you want the buyer to know.]"],
        "missing": ["✗ Tracking added to this payment", "✗ Delivered: no tracking in PayPal",
                    "? Shipped to the Transaction Details address: compare your carrier receipt"]},
    "PP-SIM-SNAD-05": {"to": "buyer", "missing": [], "sentences": [
        "Hello, thank you for telling me about the problem with your order.",
        f"My listing described the item as: \"{LAMP}\"", "[Explain how the item you sent matches this description.]",
        "[Add anything you want the buyer to know.]"]},
    "PP-SIM-SNAD-06": {"to": "paypal", "missing": [], "sentences": [
        f"My listing described the item as: \"{BAG}\"",
        "[Explain how the item you sent matches this description, and attach photos taken before shipping.]",
        "The item came back to me: the carrier shows it delivered on 2026-11-01, return tracking number SIM-TRK-R006.",
        "[Say what you found when you checked the returned item.]"]},
    "PP-SIM-UNAUTH-07": {"to": "paypal", "missing": [], "sentences": [
        "I shipped the item on 2026-11-02 with Practice Carrier, tracking number SIM-TRK-0007.",
        "The carrier shows it was delivered on 2026-11-04 to the address on the Transaction Details page.", ATTACH]},
    "PP-SIM-UNAUTH-08": {"to": "paypal", "sentences": [
        "I shipped the item on 2026-11-04 with Practice Carrier, tracking number SIM-TRK-0008.",
        "The carrier shows it was delivered on 2026-11-04 to the address on the Transaction Details page.", ATTACH],
        "missing": ["✗ Eligible for Seller Protection against unauthorized payments",
                    "✗ Shipped no later than 2 days after the dispute opened: shipped 2026-11-04, opened 2026-10-31 "
                    "00:00 UTC"]},
    "PP-SIM-CARD-09": {"none": "No draft for a card case."},
    "PP-SIM-INR-10": {"to": "paypal", "sentences": [
        "I shipped the item on 2026-10-22 with Practice Carrier, tracking number SIM-TRK-0010.", ATTACH],
        "missing": ["✗ Not delivered to the Transaction Details address: the carrier shows another address"]},
}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(dispute_draft, "now", lambda: NOW)


@pytest.fixture(autouse=True)
def offline(monkeypatch, api):
    """A PayPal call or a database read fails the test; respx sees nothing."""
    def refuse(*args, **kwargs):
        raise AssertionError("dispute_draft called PayPal or the database")
    monkeypatch.setattr(paypal, "_call", refuse)
    monkeypatch.setattr(db, "_connect", refuse)
    yield
    assert not api.calls


def state_of(did: str) -> dict:
    return dispute_draft.graph.invoke({"dispute_id": did})


def keys(v, path=()):
    """Every key path in a JSON value."""
    if isinstance(v, dict):
        for k, x in v.items():
            yield (*path, k)
            yield from keys(x, (*path, k))
    elif isinstance(v, list):
        for x in v:
            yield from keys(x, path)


# A. the scenarios

def test_u36_every_scenario_has_a_practice_id_paypal_never_takes():
    assert IDS == [s["id"] for s in RAW["scenarios"]] and len(IDS) == 10  # no ID twice
    for did in IDS:
        assert dispute_draft.SIM_ID.fullmatch(did) and not chat.REAL_ID.fullmatch(did)  # never the real-ID reply
        assert dispute_draft.SIM_ID.fullmatch(did.lower()) is None  # chat.dispute_id upper-cases it first


def test_u36_each_dispute_keeps_its_fields_through_keep():
    for did in IDS:
        s, d = dispute_draft.SCENARIOS[did], state_of(did)["dispute"]
        assert {"create_time", "seller_response_due_date", "reason", "status", "dispute_channel",
                "dispute_amount"} <= set(d), did
        assert scam_check.TXN_ID.fullmatch(s["capture"]["id"])
        assert d["disputed_transactions"] == [{"seller_transaction_id": s["capture"]["id"]}]
        assert dispute_draft._instant(d["create_time"]) == NOW + timedelta(hours=s["dispute"]["create_time"])
        assert "dispute_id" not in d  # keep() never takes the ID: it stays in the graph's state


def test_u36_no_buyer_data_and_the_fictional_format_is_named():
    paths = list(keys(RAW))
    assert not [p for p in paths if p[-1] in BUYER_KEYS]
    assert all(p[-2] == "listing" for p in paths if p[-1] == "name")
    assert "sim_carrier" in RAW["_about"] and "fictional" in RAW["_about"]
    assert all(s.get("sim_carrier") is not None for s in RAW["scenarios"])


def test_u36_the_marks_dont_depend_on_the_hour(monkeypatch):
    def marks():
        return {did: [(x["key"], x["mark"]) for x in state_of(did)["checklist"]] for did in IDS}
    assert marks() == MARKS
    monkeypatch.setattr(dispute_draft, "now", lambda: NOW + timedelta(hours=23, minutes=59))
    assert marks() == MARKS


# B. the graph

def test_u37_every_node_owns_its_fields_and_the_graph_is_fixed():
    nodes = {n: getattr(dispute_draft, n) for n in dispute_draft.graph.nodes if n != "__start__"}
    assert {n: set(fn.owns) for n, fn in nodes.items()} == {
        "load_scenario": {"found", "dispute", "transaction", "tracking", "paid", "shipments", "carrier", "listing"},
        "classify_case": {"kind"}, "build_checklist": {"checklist"}, "build_draft": {"draft"},
        "write_reply": {"reply", "shown"}}
    edges = {(e.source, e.target) for e in dispute_draft.graph.get_graph().edges}
    assert edges == {("__start__", "load_scenario"), ("load_scenario", "classify_case"),
                     ("classify_case", "build_checklist"), ("build_checklist", "build_draft"),
                     ("build_draft", "write_reply"), ("write_reply", "__end__")}
    assert list(dispute_draft.graph.get_input_jsonschema()["properties"]) == ["dispute_id"]
    assert "llm" not in vars(dispute_draft)


@pytest.mark.parametrize("did", IDS)
def test_u37_each_scenario_runs_offline(did):
    out = dispute_draft.run(did)
    assert out["kind"] == KIND[did.split("-")[2]]
    assert out["reply"].startswith(f"{dispute_draft.SIMULATED}\n\nDispute {did}: ")


@pytest.mark.parametrize("did", ["PP-SIM-INR-99", "PP-R-YGU-10196437", "PP-SIM-inr-01", "nonsense", ""])
def test_u37_an_unknown_id_is_not_found(did):
    out = dispute_draft.run(did)
    assert (out["kind"], out["shown"], out["checklist"]) == (None, [], [])
    assert out["reply"] == "\n\n".join([dispute_draft.SIMULATED, dispute_draft.NOT_FOUND.format(
        did if dispute_draft.SIM_ID.fullmatch(did) else "with that ID", ", ".join(IDS)), dispute_draft.ANYTHING_ELSE])


def test_u37_paypals_shapes_parse_as_paypal_sends_them():
    """A scenario's capture and trackers, read in the shapes of PayPal's GETs."""
    assert dispute_draft.tracker_fields([
        {"status": "CANCELLED", "shipment_date": "2026-11-01"},
        {"status": "SHIPPED", "shipment_direction": "RETURN", "shipment_date": "2026-11-01"},
        {"status": "SHIPPED", "shipment_date": "2026-11-02", "tracking_number_validated": True}]) == {
        "trackers": [{"date": "2026-11-02", "validated": True}]}
    assert dispute_draft.capture_fields({"status": "REFUNDED", "seller_protection": {
        "status": "ELIGIBLE", "dispute_categories": ["ITEM_NOT_RECEIVED", "bad value"]}}) == {
        "status": "REFUNDED", "protection": "ELIGIBLE", "categories": ["ITEM_NOT_RECEIVED"]}


def test_u37_snad_keeps_paypals_codes_only():
    raw = {"extensions": {"merchant_contacted": True, "merchandize_dispute_properties": {
        "product_details": {"product_received": "RETURNED", "sub_reasons": ["DAMAGED", "CANARY", ["DIFFERENT"]],
                            "description": "CANARY the buyer's own words"},
        "return_shipping_address": {"address_line_1": "CANARY"}}}}
    assert dispute_draft.snad_codes(raw) == {"extensions": {"merchandize_dispute_properties": {"product_details": {
        "sub_reasons": ["DAMAGED"], "product_received": "RETURNED"}}}}
    assert dispute_draft.snad_codes({"extensions": "CANARY"}) == {} == dispute_draft.snad_codes({})


# C. the checklist

def test_u38_every_quote_is_its_notes_own_words():
    verified = policy_pack.verified()
    for key, (note, quote) in dispute_draft.QUOTES.items():
        assert note in verified and quote in verified[note]["text"].removesuffix(f"\n{scam_check.SUPPORT}"), key


def test_u38_checklists_lines_carry_their_key():
    """Each of checklist()'s lines says which it is, whatever the scenario holds: never found by its text."""
    for kind in ("inr", "unauth", "snad"):
        for transaction, tracking in ((CAPTURED, SHIPPED), ({"categories": []}, {"trackers": []})):
            for line in dispute_draft.checklist(kind, OPENED, transaction, tracking):
                assert line["text"].startswith(dispute_draft.KEYS[line["key"]])


def test_u38_the_groups_and_each_quote_once():
    out = dispute_draft.run("PP-SIM-INR-03")["checklist"]
    assert [(g["group"], g["heading"]) for g in out] == [(g, dispute_draft.GROUPS[g]) for g in ("ready", "missing", "check")]
    quotes = [x["why"] for g in out for x in g["lines"] if x["why"]]
    assert len(quotes) == len(set(quotes)) == 3  # tracking, delivered, address: "validated" shares tracking's
    assert out[0]["lines"][0] == {"mark": YES, "text": "Tracking added to this payment: SIM-TRK-0003, Practice Carrier",
                                  "why": 'PayPal: "An online and verifiable tracking number." (Proof of shipment or '
                                         'delivery)'}


def test_u38_an_unverified_note_gives_no_quote(monkeypatch):
    monkeypatch.setitem(policy_pack.POLICY_PACK, "pp_proof_of_delivery",
                        policy_pack.POLICY_PACK["pp_proof_of_delivery"] | {"verified": False})
    out = dispute_draft.run("PP-SIM-INR-01")
    assert "Proof of shipment or delivery" not in out["reply"]
    assert "✓ Tracking added to this payment: SIM-TRK-0001, Practice Carrier\n✓ Tracking number" in out["reply"]


def test_u38_no_refund_is_no_gap_and_a_refund_is_ready():
    for did in IDS:
        assert not [x for x in state_of(did)["checklist"] if x["key"] == "refund"]  # COMPLETED: "none", not shown
    s = state_of("PP-SIM-INR-01")
    s["transaction"] = s["transaction"] | {"status": "REFUNDED"}
    lines = dispute_draft.build_checklist(s)["checklist"]
    assert lines[-1] == {"key": "refund", "mark": YES, "group": "ready", "text": "Refund issued on this payment: refunded",
                         "quote": None}


def test_u38_snad_has_no_seller_protection_proof():
    s = state_of("PP-SIM-SNAD-05")
    assert s["checklist"][0] == {"key": "not_covered", "mark": NO, "group": "missing",
                                 "text": "Not covered by Seller Protection", "quote": "not_covered"}
    assert {x["key"] for x in s["checklist"]} == {"not_covered", "listing"}  # no tracking, address or signature
    assert "What the buyer reports: damaged. Item received: yes." in dispute_draft.run("PP-SIM-SNAD-05")["reply"]


def test_u38_the_signature_rule_reads_the_payment_date():
    delivered = {"signed": False}
    # not required after 26 Jan 2026: nothing against the seller, so "Check yourself", not "Missing" (2026-11-07)
    after = dispute_draft._signature("2026-01-27T12:00:00+00:00", delivered)
    assert (after["mark"], after["group"]) == (UNKNOWN, "check") and "Not required" in after["text"]
    before = dispute_draft._signature("2026-01-27T11:59:00+00:00", delivered)  # the 26th somewhere
    assert before["mark"] == UNKNOWN and "check whether this payment needs one" in before["text"]
    assert dispute_draft._signature(None, delivered)["mark"] == UNKNOWN
    assert dispute_draft._signature("2026-11-01T00:00:00+00:00", {"signed": True})["mark"] == YES


# D. the draft

@pytest.mark.parametrize("did", IDS)
def test_u39_the_draft(did):
    assert state_of(did)["draft"] == DRAFTS[did]


@pytest.mark.parametrize("did", [d for d in IDS if not d.startswith("PP-SIM-CARD")])
def test_u39_a_sentence_needs_every_line_it_names(did):
    s = state_of(did)
    kind, to, values = s["kind"], s["draft"]["to"], dispute_draft._values(s)
    for i, line in enumerate(s["checklist"]):
        if line["mark"] != YES:
            continue
        for mark in (NO, UNKNOWN):
            draft = dispute_draft.build_draft(s | {"checklist": [*s["checklist"][:i], line | {"mark": mark},
                                                                 *s["checklist"][i + 1:]]})["draft"]
            needing = [text.format(**values) for needs, text in dispute_draft.DRAFTS[(kind, to)] if line["key"] in needs]
            assert not set(needing) & set(draft["sentences"]), (did, line["key"])
            assert (f"{mark} {line['text']}" in draft["missing"]) == (line["key"] in dispute_draft.COUNTS[kind])


@pytest.mark.parametrize("over, why", [({"status": "RESOLVED"}, "resolved"),
                                       ({"dispute_life_cycle_stage": "PRE_ARBITRATION"}, "stage")])
def test_u39_no_draft_for_a_resolved_dispute_or_an_appeal(over, why):
    s = state_of("PP-SIM-INR-02")
    assert dispute_draft.build_draft(s | {"dispute": s["dispute"] | over})["draft"] == {"none": dispute_draft.NO_DRAFT[why]}
    assert dispute_draft.build_draft(s | {"kind": "other"})["draft"] == {"none": dispute_draft.NO_DRAFT["other"]}
    assert dispute_draft.build_draft({"dispute": None})["draft"] is None


def test_u39_no_draft_promises_or_guesses():
    for did in IDS:
        text = " ".join(state_of(did)["draft"].get("sentences", []))
        assert not re.search(r"(?i)refund|2 days|two days|notice|protect|\bwin|guarantee|[{}]|the buyer's name", text), did


# E. the reply

def test_u40_the_reply_in_order():
    verified = policy_pack.verified()
    reply = dispute_draft.run("PP-SIM-INR-01")["reply"]
    parts = reply.split("\n\n")
    assert parts[0] == dispute_draft.SIMULATED
    assert parts[1] == ("Dispute PP-SIM-INR-01: Item not received. PayPal case, inquiry stage, waiting for your response."
                        "\nDisputed: 45.00 USD. Respond by 2026-11-13 00:00 UTC (8 days).")
    assert parts[2] == dispute_draft.NO_ASKS_HERE  # never "check the dispute in your Resolution Center": it isn't there
    assert parts[3].startswith(dispute_draft.GROUPS["ready"]) and parts[4].startswith(dispute_draft.GROUPS["check"])
    assert parts[5] == f"{dispute_draft.DRAFT_TO['buyer']}\n{' '.join(DRAFTS['PP-SIM-INR-01']['sentences'])}"
    assert reply.endswith("\n\n".join([
        f"{dispute_draft.YOU_SEND}\n{policy_pack.note(verified['pp_respond_to_dispute'])}",
        "\n".join([dispute_draft.PAGES, *(f"{verified[k]['title']}: {verified[k]['source_url']}" for k in (
            "pp_proof_of_delivery", "pp_ship_to_transaction_address", "pp_proof_of_shipment"))]),
        dispute_draft.ANYTHING_ELSE]))
    assert dispute_draft.MISSING not in reply and scam_check.SUPPORT not in reply


def test_u40_whats_missing_comes_after_the_draft():
    reply = dispute_draft.run("PP-SIM-INR-03")["reply"]
    assert "\n\n".join([f"{dispute_draft.DRAFT_TO['paypal']}\n{' '.join(DRAFTS['PP-SIM-INR-03']['sentences'])}",
                        "\n".join([dispute_draft.MISSING, *DRAFTS["PP-SIM-INR-03"]["missing"]]),
                        dispute_draft.YOU_SEND]) in reply


@pytest.mark.parametrize("did, shown", [("PP-SIM-INR-01", ["pp_respond_to_dispute"]),
                                        ("PP-SIM-SNAD-05", ["pp_respond_to_dispute"]),
                                        ("PP-SIM-UNAUTH-08", ["pp_respond_to_dispute"]),
                                        ("PP-SIM-CARD-09", ["pp_chargeback"]), ("PP-SIM-INR-99", [])])
def test_u40_the_notes_shown_for_the_chats_next_what_now(did, shown):
    assert dispute_draft.run(did)["shown"] == shown


def test_u40_a_card_case_is_the_header_and_the_chargeback_note():
    reply = dispute_draft.run("PP-SIM-CARD-09")["reply"]
    assert reply == "\n\n".join([dispute_draft.SIMULATED, "Dispute PP-SIM-CARD-09: Item not received. Card or bank case, "
                                 "waiting for your response.\nDisputed: 150.00 USD. Respond by 2026-11-12 00:00 UTC "
                                 "(7 days).", dispute_draft.NO_DRAFT["card"], chat._note("pp_chargeback"),
                                 dispute_draft.ANYTHING_ELSE])


def test_u40_an_unverified_respond_note_isnt_shown(monkeypatch):
    monkeypatch.setitem(policy_pack.POLICY_PACK, "pp_respond_to_dispute",
                        policy_pack.POLICY_PACK["pp_respond_to_dispute"] | {"verified": False})
    out = dispute_draft.run("PP-SIM-INR-01")
    assert out["shown"] == [] and "How to respond to a dispute" not in out["reply"]
    assert f"\n\n{dispute_draft.YOU_SEND}\n\n" in out["reply"]


def test_u40_no_reply_speaks_of_the_outcome():
    """GoodToShip's own words: PayPal's notes, and the quotes from them, are PayPal's."""
    words = [chat._note(k) for k in chat.VERIFIED] + [q for _, q in dispute_draft.QUOTES.values()]
    for did in IDS:
        reply = dispute_draft.run(did)["reply"]
        for w in words:
            reply = reply.replace(w, "")
        assert not re.search(r"(?i)\b(?:win|won|lose|lost|protected|guarantee\w*|outcome|chance)\b", reply), did


# F. PayPal's shapes: classify_case, checklist(), keep() and the deadline, pure (moved from dispute_defense's tests)

@pytest.mark.parametrize("dispute, kind", [
    ({"dispute_channel": "EXTERNAL", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}, "card"),
    ({"dispute_channel": "ALERT", "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"}, "card"),
    ({"dispute_channel": "EXTERNAL"}, "card"),
    ({"dispute_channel": "INTERNAL", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}, "inr"),
    ({"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"}, "inr"),
    ({"dispute_channel": "INTERNAL", "reason": "UNAUTHORISED"}, "unauth"),
    ({"dispute_channel": "INTERNAL", "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"}, "snad"),
    ({"dispute_channel": "INTERNAL", "reason": "CREDIT_NOT_PROCESSED"}, "other"),
    ({"dispute_channel": "INTERNAL", "reason": "SOMETHING_NEW"}, "other"),
    ({"dispute_channel": "INTERNAL"}, "other"),
])
def test_u28_classify_case(dispute, kind):
    txs = {"disputed_transactions": [{"seller_transaction_id": CAP}]}
    assert dispute_draft.classify_case({"dispute": dispute | txs}) == {"kind": kind}


@pytest.mark.parametrize("reason", ["MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "UNAUTHORISED"])
def test_u28_inr_or_unauth_without_a_payment_is_other(reason):
    assert dispute_draft.classify_case({"dispute": {"reason": reason}}) == {"kind": "other"}


def test_u28_no_dispute_no_kind():
    assert dispute_draft.classify_case({"dispute": None}) == {"kind": None}


SHIPPED = {"trackers": [{"date": "2026-11-02", "validated": None, "status": "SHIPPED"}]}
CAPTURED = {"status": "COMPLETED", "protection": "ELIGIBLE", "categories": ["ITEM_NOT_RECEIVED", "UNAUTHORIZED_TRANSACTION"]}
OPENED = {"create_time": "2026-11-04T10:00:00Z"}


def check(kind="inr", transaction=CAPTURED, tracking=SHIPPED, dispute=OPENED):
    return {x["text"].split(":")[0]: (x["mark"], x["text"]) for x in
            dispute_draft.checklist(kind, dispute, transaction, tracking)}


def shipped(day):
    return {"trackers": [{"date": day, "validated": None, "status": "SHIPPED"}]}


@pytest.mark.parametrize("day, expected", [("2026-11-02", YES), ("2026-11-03", UNKNOWN), ("2026-11-04", UNKNOWN),
                                           ("2026-11-05", NO)],
                         ids=["before in every timezone", "the day before", "the same day", "after in every timezone"])
def test_u29_shipped_before_the_dispute_opened_in_every_timezone(day, expected):
    """Opened at 10:00 UTC. A shipment_date is the seller's day, UTC-12 to UTC+14: 2026-11-03 ends at 2026-11-04 12:00
    UTC in UTC-12, after the opening, and 2026-11-05 starts at 2026-11-04 10:00 UTC in UTC+14, not before it."""
    assert check(tracking=shipped(day))["Shipped before the dispute opened"] == (
        expected, f"Shipped before the dispute opened: shipped {day}, opened 2026-11-04 10:00 UTC")


def test_u29_a_us_seller_shipping_after_a_late_evening_opening_is_never_yes():
    """Opened 2026-11-05 01:00 UTC (20:00 EST on the 4th), shipped at 21:00 EST on the 4th."""
    lines = check(tracking=shipped("2026-11-04"), dispute={"create_time": "2026-11-05T01:00:00Z"})
    assert lines["Shipped before the dispute opened"][0] == UNKNOWN


def test_u29_the_first_shipment_counts():
    tracking = {"trackers": [{"date": "2026-11-06", "validated": None, "status": "SHIPPED"},
                             {"date": "2026-11-01", "validated": None, "status": "SHIPPED"}]}
    assert check(tracking=tracking)["Shipped before the dispute opened"][0] == YES


def test_u29_inr_lines_in_order_and_each_mark():
    assert [(m, t) for m, t in check().values()] == [
        (YES, "Tracking added to this payment"),
        (UNKNOWN, "Tracking number validated by the carrier"),
        (YES, "Shipped before the dispute opened: shipped 2026-11-02, opened 2026-11-04 10:00 UTC"),
        (UNKNOWN, "Shipped to the Transaction Details address: compare your carrier receipt"),
        (NO, "Refund issued on this payment: none")]
    validated = lambda *v: {"trackers": [{"date": "2026-11-02", "validated": x, "status": "SHIPPED"} for x in v]}  # noqa: E731
    assert check(tracking=validated(False, True))["Tracking number validated by the carrier"][0] == YES
    assert check(tracking=validated(False))["Tracking number validated by the carrier"][0] == NO
    assert check(tracking=validated(False, None))["Tracking number validated by the carrier"][0] == UNKNOWN
    assert check(transaction={"status": "REFUNDED", "categories": []})["Refund issued on this payment"] == (
        YES, "Refund issued on this payment: refunded")
    assert check(transaction={"status": "PARTIALLY_REFUNDED", "categories": []})["Refund issued on this payment"][0] == YES
    assert check(transaction={"categories": []})["Refund issued on this payment"] == (UNKNOWN, "Refund issued on this payment")


def test_u29_no_tracking_is_no():
    lines = check(tracking={"trackers": []})
    assert lines["Tracking added to this payment"][0] == lines["Tracking number validated by the carrier"][0] == NO
    assert lines["Shipped before the dispute opened"] == (NO, "Shipped before the dispute opened: no tracking in PayPal")


@pytest.mark.parametrize("transaction, expected", [
    (CAPTURED, YES),
    ({"status": "COMPLETED", "protection": "PARTIALLY_ELIGIBLE", "categories": ["UNAUTHORIZED_TRANSACTION"]}, YES),
    ({"status": "COMPLETED", "protection": "NOT_ELIGIBLE", "categories": []}, NO),
    ({"status": "COMPLETED", "protection": "ELIGIBLE", "categories": ["ITEM_NOT_RECEIVED"]}, NO),
    ({"status": "COMPLETED", "protection": "ELIGIBLE", "categories": []}, UNKNOWN),
    ({"status": "COMPLETED", "protection": None, "categories": []}, UNKNOWN),
], ids=["eligible", "partially", "not eligible", "not for unauthorized", "no categories", "no protection"])
def test_u30_unauth_eligibility(transaction, expected):
    lines = list(check("unauth", transaction=transaction).items())
    assert lines[0] == ("Eligible for Seller Protection against unauthorized payments", (expected, lines[0][1][1]))


@pytest.mark.parametrize("day, expected", [("2026-11-04", YES), ("2026-11-05", UNKNOWN), ("2026-11-06", UNKNOWN),
                                           ("2026-11-07", NO)], ids=["D", "D+1", "D+2", "D+3"])
def test_u30_unauth_shipped_within_2_days_in_every_timezone(day, expected):
    """Opened 2026-11-04 10:00 UTC, so the 2 days end 2026-11-06 10:00 UTC: D+1 ends at 2026-11-06 12:00 UTC in UTC-12,
    and D+3 starts at 2026-11-06 10:00 UTC in UTC+14."""
    lines = check("unauth", tracking=shipped(day))
    assert lines["Shipped no later than 2 days after the dispute opened"][0] == expected
    assert list(lines) == ["Eligible for Seller Protection against unauthorized payments",
                           "Shipped no later than 2 days after the dispute opened", "Tracking added to this payment",
                           "Tracking number validated by the carrier", "Shipped to the Transaction Details address",
                           "Refund issued on this payment"]  # no "before the dispute opened": that is INR's


def test_u32_keep_takes_only_its_fields_and_drops_a_bad_value():
    raw = {"dispute_id": "PP-SIM-INR-01", "create_time": "x", "update_time": "2026-11-04T10:05:00.000Z",
           "seller_response_due_date": "2026-11-14", "reason": "<b>", "status": "waiting",
           "dispute_channel": "INTERNAL; DROP", "dispute_life_cycle_stage": "INQUIRY",
           "dispute_amount": {"currency_code": "usd", "value": "1e9"},
           "disputed_transactions": [{"seller_transaction_id": "../captures", "buyer": {"name": "CANARY"}}, "x"],
           "evidences": [{"evidence_type": "<script>", "source": "REQUESTED_FROM_SELLER"},
                         {"evidence_type": "PROOF_OF_RECEIPT_COPY", "source": "SUBMITTED_BY_BUYER", "notes": "CANARY"}],
           "messages": [{"content": "CANARY"}], "extensions": {"x": "CANARY"}}
    assert dispute_draft.keep(raw) == {"update_time": "2026-11-04T10:05:00.000Z", "dispute_life_cycle_stage": "INQUIRY"}
    assert dispute_draft.keep(["not", "a", "dict"]) == {}


@pytest.mark.parametrize("due, left", [("2026-11-14T10:00:00Z", "(9 days)"), ("2026-11-06T00:00:00Z", "(1 day)"),
                                       ("2026-11-05T23:59:00Z", "(less than a day)"),
                                       ("2026-11-04T10:00:00Z", "(past due)")])
def test_u35_the_deadline(due, left):
    assert dispute_draft.respond_by({"status": "OPEN", "seller_response_due_date": due}, NOW).endswith(left)


@pytest.mark.parametrize("d", [{"status": "OPEN"}, {"status": "UNDER_REVIEW", "seller_response_due_date": "2026-11-14T10:00:00Z"},
                               {"status": "RESOLVED", "seller_response_due_date": "2026-11-14T10:00:00Z"},
                               {"status": "OPEN", "seller_response_due_date": "2026-11-14"}])
def test_u35_no_respond_by_without_a_date_the_seller_can_still_meet(d):
    assert dispute_draft.respond_by(d, NOW) is None


def test_u39_a_value_the_scenario_lacks_is_listed_as_missing(monkeypatch):  # review, 2026-11-07
    """The tracking line is ✓ but the carrier has no name: its sentence goes, and the carrier is listed as missing."""
    s = state_of("PP-SIM-INR-01")
    s["shipments"]["forward"] = s["shipments"]["forward"] | {"carrier": None}
    draft = dispute_draft.build_draft(s)["draft"]
    assert not any("tracking number" in x for x in draft["sentences"])
    assert f"{UNKNOWN} Carrier: not in this scenario" in draft["missing"]
