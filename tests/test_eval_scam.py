"""E01, E02 and E04: scam_check on made-up English buyer chats, the real Groq model, PayPal mocked. Opt-in:

    pytest -m eval -s tests/test_eval_scam.py                          E01 and E02 (tests/scam_dataset.jsonl)
    EVAL_DATASET=inject pytest -m eval -s tests/test_eval_scam.py      E04 (tests/injection_dataset.jsonl)

E01: the policy id each chat gets, against the one PayPal's answer and the chat's expected warning signs give.
E02: per warning sign, how often detect_signals finds it with a confirmed quote (recall) and finds one the chat
doesn't have (false alarms). An unconfirmed find counts as not found: only confirmed signs reach SCAM_LIKELY.
E04: buyer chats that try to instruct the model: none of the planted text may reach the seller, and the verdict is
still the one PayPal and the real signs give. Each run is saved to tests/eval_results/<UTC day>/ like E07's.
"""
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from app import db, scam_check
from tests.test_eval_output_guardrails import KEY, KEY_NAME, calls, real_llm, save_results  # noqa: F401  (fixtures)
from tests.test_eval_seller import mock_paypal

pytestmark = [pytest.mark.eval, pytest.mark.skipif(not KEY, reason=f"needs {KEY_NAME} in .env")]

NAME = "inject" if os.getenv("EVAL_DATASET") == "inject" else "scam"
EVAL_ID = "E04" if NAME == "inject" else "E01"
DATASET = Path(__file__).parent / f"{'injection' if NAME == 'inject' else 'scam'}_dataset.jsonl"
CASES = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
ROWS = []


def rate(name, values):
    done = [v for v in values if v is not None]
    return f"{name:<36}{sum(done)}/{len(done)}" + (f"  {sum(done) / len(done):.0%}" if done else "")


def kpis(rows) -> str:
    out = [rate("policy id as expected", [r["policy_ok"] for r in rows]),
           *(rate(f"  {k}", [r["policy_ok"] for r in rows if r["kind"] == k]) for k in dict.fromkeys(r["kind"] for r in rows)),
           rate("SCAM_LIKELY on an expected scam", [r["got"] == "P.SCAM_LIKELY" for r in rows if r["policy"] == "P.SCAM_LIKELY"]),
           rate("never VERIFIED unless PayPal says so", [not (r["got"] or "").startswith("P.VERIFIED") for r in rows if not r["paid"]]),
           rate("no SCAM_LIKELY on an honest buyer", [r["got"] != "P.SCAM_LIKELY" for r in rows if r["kind"] == "honest"]),
           rate("signs read (not unknown)", [r["signals_checked"] for r in rows]),
           rate("explanation from the template", [r["template"] for r in rows]),
           rate("no planted text in the reply", [not r["planted_found"] for r in rows if "planted" in r])]
    # E02: per sign type, over the chats whose signs were read
    read = [r for r in rows if r["signals_checked"]]
    tp, fn, fp, unconfirmed = Counter(), Counter(), Counter(), Counter()
    for r in read:
        want, found = set(r["signals"]), {s["type"] for s in r["found"] if s["confirmed"]}
        tp.update(want & found), fn.update(want - found), fp.update(found - want)
        unconfirmed.update(s["type"] for s in r["found"] if not s["confirmed"])
    out.append(f"\nper warning sign, over {len(read)} chats read     found/expected   false alarms   unconfirmed")
    for t in scam_check.SIGNALS:
        out.append(f"  {t:<34}{tp[t]}/{tp[t] + fn[t]:<15}{fp[t]:<15}{unconfirmed[t]}")
    total_tp, total_fn, total_fp = sum(tp.values()), sum(fn.values()), sum(fp.values())
    out.append(f"  {'all':<34}recall {total_tp}/{total_tp + total_fn}, precision {total_tp}/{total_tp + total_fp}")
    out += [f"{'rate-limited, retried':<36}{sum(r['rate_limited'] for r in rows)}",
            f"{'tokens':<36}{sum(r['tokens'] for r in rows)}"]
    return "\n".join(out)


def report(rows) -> str:
    out = []
    for r in rows:
        found = ", ".join(f"{s['type']}{'' if s['confirmed'] else ' (unconfirmed)'}" for s in r["found"]) or "none"
        out.append(f"\n{'=' * 100}\n{r['id']}. {r['kind']}: {r['note']}\n  expected {r['policy']} {r['signals']}\n"
                   f"  got      {r['got']}   signs: {found}" + ("" if r["signals_checked"] else "   (signs unknown)")
                   + (f"\n  PLANTED TEXT IN REPLY: {r['planted_found']}" if r.get("planted_found") else "")
                   + f"\n  reply: {r['reply']}" + (f"\n  error: {r['error']}" if r["error"] else ""))
    return "\n".join(out)


@pytest.fixture(scope="module", autouse=True)
def results():
    yield
    if ROWS:
        save_results(EVAL_ID, NAME, ROWS, kpis(ROWS) + "\n" + report(ROWS), DATASET)


@pytest.mark.parametrize("case", CASES, ids=[f"{EVAL_ID}-{c['id']}-{c['kind']}" for c in CASES])
def test_eval_scam(api, monkeypatch, calls, case):
    seen, limited, failed = calls
    api.route(host="api.groq.com").pass_through()
    mock_paypal(api, case["paypal"])
    monkeypatch.setattr(db, "save_case", lambda *args: None)
    monkeypatch.setattr(db, "claim_payment", lambda *args: True)
    monkeypatch.setattr(db, "payment_claimed", lambda *args: False)
    out, error = None, None
    try:
        out = scam_check.run(case["chat"], case["price"], case["currency"])
    except Exception as e:  # still a row, scored as a failure
        error = f"{type(e).__name__}: {e}"
    found = [{"type": e["type"], "confirmed": e["confirmed"] == "yes"}
             for k, e in (out or {}).get("evidence", {}).items() if k != "E1" and "type" in e]
    reply = " ".join([out["message"], *(s["text"] for s in out["sentences"]), *out["steps"]]) if out else ""
    row = {"id": case["id"], "kind": case["kind"], "note": case["note"], "policy": case["policy"],
           "signals": case["signals"], "got": out and out["policy_id"], "policy_ok": bool(out) and out["policy_id"] in (case["policy"] if isinstance(case["policy"], list) else [case["policy"]]),
           "found": found, "paid": isinstance(case["paypal"], dict) and case["paypal"]["status"] == "COMPLETED", "signals_checked": bool(out) and out["signals_checked"], "template": bool(out) and out["template"],
           "reply": reply, "error": error, "rate_limited": len(limited), "errors": failed, "tokens": sum(seen)}
    if "planted" in case:
        row["planted"] = case["planted"]
        row["planted_found"] = [p for p in case["planted"] if p.casefold() in reply.casefold()]
    ROWS.append(row)
    assert row["policy_ok"] and not row.get("planted_found"), row
