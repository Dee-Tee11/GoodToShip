"""The real SQL, against a throwaway Postgres (RECHECK #7). Skipped unless TEST_DATABASE_URL is set:

    docker run -d --rm --name vg-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_db.py
"""
import os
import uuid

import psycopg
import pytest

from app import db

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL or "")
    db.init()


def event(resource_id):
    return {"id": f"WH-{uuid.uuid4().hex}", "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "summary": "Payment completed", "resource": {"id": resource_id, "status": "COMPLETED"}}


def demo_session():
    """A session with a captured sale, its webhook events, a check that claimed the payment, and a handoff."""
    sid, order, cap, case = (uuid.uuid4().hex for _ in range(4))
    db.new_demo_session(sid)
    db.add_demo_sale(sid, order, "Camera", "45.00")
    db.set_demo_capture(order, cap, "COMPLETED")
    db.add_demo_case(sid, case)
    assert db.claim_payment(cap, case)
    db.save_case(case, "VERIFIED", {"verdict": "VERIFIED"})
    assert db.claim_event(event(order)) and db.claim_event(event(cap))
    db.add_handoff(sid, [{"who": "seller", "text": "operator"}], {"case_id": case})
    return sid, order, cap, case


def rows(order, cap, case):
    with db._connect() as c:
        return {
            "demo_sales": c.execute("SELECT count(*) FROM demo_sales WHERE order_id = %s", (order,)).fetchone()[0],
            "demo_cases": c.execute("SELECT count(*) FROM demo_cases WHERE case_id = %s", (case,)).fetchone()[0],
            "scam_cases": c.execute("SELECT count(*) FROM scam_cases WHERE id = %s", (case,)).fetchone()[0],
            "claimed_payments": c.execute("SELECT count(*) FROM claimed_payments WHERE transaction_id = %s",
                                          (cap,)).fetchone()[0],
            "webhook_events": c.execute("SELECT count(*) FROM webhook_events WHERE body->'resource'->>'id' IN (%s, %s)",
                                        (order, cap)).fetchone()[0],
            "handoffs": c.execute("SELECT count(*) FROM handoffs WHERE last_case->>'case_id' = %s", (case,)).fetchone()[0],
        }


ALL = {"demo_sales": 1, "demo_cases": 1, "scam_cases": 1, "claimed_payments": 1, "webhook_events": 2, "handoffs": 1}
NONE = dict.fromkeys(ALL, 0)


def test_ending_a_session_deletes_all_it_left_and_nothing_else():
    mine, other = demo_session(), demo_session()
    state = db.demo_state(mine[0])
    assert [s["capture_id"] for s in state["sales"]] == [mine[2]] and len(state["events"]) == 2
    assert [(h["status"], h["replies"]) for h in state["handoffs"]] == [("open", [])]
    assert db.end_demo_sessions(mine[0]) == [mine[0]]
    assert rows(*mine[1:]) == NONE and not db.touch_demo_session(mine[0])
    assert rows(*other[1:]) == ALL and db.touch_demo_session(other[0])
    db.end_demo_sessions(other[0])


def test_only_idle_or_expired_sessions_are_swept():
    idle, expired, active = demo_session(), demo_session(), demo_session()
    with db._connect() as c:
        c.execute("UPDATE demo_sessions SET last_seen = now() - interval '31 minutes' WHERE id = %s", (idle[0],))
        # its tab still polls, but it started more than DEMO_MAX ago
        c.execute("UPDATE demo_sessions SET created_at = now() - interval '121 minutes' WHERE id = %s", (expired[0],))
    assert not db.touch_demo_session(idle[0])  # idle sessions can't be revived
    assert not db.touch_demo_session(expired[0])
    swept = db.end_demo_sessions()
    assert idle[0] in swept and expired[0] in swept and active[0] not in swept
    assert rows(*idle[1:]) == rows(*expired[1:]) == NONE
    assert rows(*active[1:]) == ALL
    db.end_demo_sessions(active[0])


def test_a_sale_belongs_to_its_session_only():
    mine, other = demo_session(), demo_session()
    assert db.demo_sale_exists(mine[0], mine[1]) and not db.demo_sale_exists(other[0], mine[1])
    db.end_demo_sessions(mine[0])
    db.end_demo_sessions(other[0])


def test_a_handoff_takes_messages_and_answers_until_it_is_closed():
    sid = demo_session()[0]  # its handoff is open
    with pytest.raises(psycopg.errors.UniqueViolation):  # one open per session
        db.add_handoff(sid, [{"who": "seller", "text": "again"}], None)
    (first,) = db.demo_state(sid)["handoffs"]
    assert db.relay(sid, {"who": "seller", "text": "still there?"})
    assert db.answer_handoff(first["id"], "Hi, Ana from Vendor Guard here.") and db.answer_handoff(first["id"], "Yes.")
    (listed,) = [h for h in db.open_handoffs() if h["id"] == first["id"]]
    assert [t["text"] for t in listed["transcript"]] == ["operator", "still there?", "Hi, Ana from Vendor Guard here.", "Yes."]
    assert "session_id" not in listed and listed["updated_at"] > listed["shared_at"]
    assert db.open_handoff_session(first["id"]) == sid and db.handoff_open(sid)
    assert not db.close_handoff(sid, first["id"] + 1)  # the person's Close: only the card they clicked
    assert db.close_handoff(sid, first["id"]) and not db.close_handoff(sid)
    assert not db.relay(sid, {"who": "seller", "text": "late"}) and not db.answer_handoff(first["id"], "late")
    assert db.open_handoff_session(first["id"]) is None and not db.handoff_open(sid)
    with db._connect() as c:  # what the person saw is deleted
        assert c.execute("SELECT transcript, last_case FROM handoffs WHERE id = %s", (first["id"],)).fetchone() == ([], None)
    db.add_handoff(sid, [{"who": "seller", "text": "a new one"}], None)  # after closing: a new one
    assert [(h["status"], h["replies"]) for h in db.demo_state(sid)["handoffs"]] == [
        ("closed", ["Hi, Ana from Vendor Guard here.", "Yes."]), ("open", [])]
    db.end_demo_sessions(sid)


def test_test_tracking_marks_a_paid_sale_of_its_session_once():
    (sid, order, cap, _), other = demo_session(), demo_session()
    assert db.claim_demo_tracking(other[0], order, "T1") is None  # another session's sale
    assert db.claim_demo_tracking(sid, order, "T1") == cap
    assert db.claim_demo_tracking(sid, order, "T2") is None  # already marked: a second click
    db.release_demo_tracking(order, "T2")  # not its number: still marked
    assert [s["tracking"] for s in db.demo_state(sid)["sales"]] == ["T1"]
    db.release_demo_tracking(order, "T1")  # PayPal refused it: a click can try again
    assert db.claim_demo_tracking(sid, order, "T3") == cap
    unpaid = uuid.uuid4().hex
    db.add_demo_sale(sid, unpaid, "Lens", "10.00")
    assert db.claim_demo_tracking(sid, unpaid, "T4") is None
    db.end_demo_sessions(sid)
    db.end_demo_sessions(other[0])


def test_u12_claims_hold_in_the_real_sql():
    e, cap = event(uuid.uuid4().hex), uuid.uuid4().hex
    assert db.claim_event(e) and not db.claim_event(e)  # ON CONFLICT DO NOTHING, rowcount 1 then 0
    assert not db.payment_claimed(cap)
    assert db.claim_payment(cap, "case-1") and not db.claim_payment(cap, "case-2")
    assert db.payment_claimed(cap)
    with db._connect() as c:
        c.execute("DELETE FROM webhook_events WHERE id = %s", (e["id"],))
        c.execute("DELETE FROM claimed_payments WHERE transaction_id = %s", (cap,))
