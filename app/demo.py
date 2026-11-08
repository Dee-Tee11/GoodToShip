"""The live demo: a tester makes a sale, pays it in PayPal's sandbox checkout with a shared buyer account, then asks
the chat graph (main.demo_chat), one conversation per session, to check the buyer's chat, and watches every node and
PayPal call of the check as it runs. With their yes, the seller can hand the conversation to a person from GoodToShip
(the handoffs table): the seller's messages then go to that person and GoodToShip doesn't answer, until the seller or
the person closes it. Closing deletes what the person saw and starts the chat over, as a new session's.

On only with DEMO_MODE=true, and only against PayPal's sandbox (main.lifespan refuses to start otherwise). Creating and
capturing orders, and adding test tracking to a paid one, are the app's only PayPal writes. They play the buyer's
purchase and the seller's shipment, and stay out of the graphs. Practice disputes are fictional, from a file: no PayPal.
The session is anonymous: the page keeps its id in sessionStorage, and everything goes when it ends.
"""
import logging
import os
import secrets
import threading
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import Path as UrlPath
from fastapi.responses import FileResponse, RedirectResponse
from langchain_core.messages import RemoveMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pydantic import BaseModel, Field

from app import chat, db, dispute_draft, paypal, ratelimit, scam_check

log = logging.getLogger(__name__)
ENABLED = os.getenv("DEMO_MODE", "").strip().lower() == "true"
REQUIRED_ENV = ("DEMO_BUYER_EMAIL", "DEMO_BUYER_PASSWORD")
CURRENCY = "USD"  # a US sandbox business account, as in the README's Seller Protection notes
MAX_REPLY = 2000
PAGE = Path(__file__).parent / "static" / "demo.html"
# the chat graph with one thread per demo session (thread_id = session id), forgotten when the session ends. Each turn
# leaves only its latest state, without its messages (_keep_latest).
# ponytail: in memory, so a restart or Render's sleep forgets every conversation; PostgresSaver if they must survive one
chat_graph = chat._build(InMemorySaver())
# each session's conversation, for a handoff: what the seller sent (a screenshot only
# when it was read) and every reply. Outside the graph, so InMemorySaver doesn't copy it into every checkpoint.
# ponytail: no cap; 30 messages and 3 read screenshots an hour per IP bound how fast it grows, and db.DEMO_MAX for
# how long (a session ends 2 hours after it started, even with its tab open)
_transcripts: dict[str, list[dict]] = {}
_busy: set[str] = set()  # sessions inside busy(): a chat message or "End demo"
_busy_lock = threading.Lock()  # turn() runs in the threadpool


def enabled():
    if not ENABLED:
        raise HTTPException(404)


def session(x_demo_session: str = Header("", max_length=64)) -> str:
    """The caller's demo session, kept alive by this request. 401 once it ended, went idle or reached db.DEMO_MAX."""
    if not x_demo_session or not db.touch_demo_session(x_demo_session):
        raise HTTPException(401, "this demo session has ended, reload the page to start a new one")
    return x_demo_session


@contextmanager
def busy(session_id: str, taken: str = "your last message is still running, wait for its answer"):
    """One at a time per session, 409 with `taken` otherwise: a chat message, "End demo" and closing a handoff all
    change the session's chat thread."""
    with _busy_lock:
        if session_id in _busy:
            raise HTTPException(409, taken)
        _busy.add(session_id)
    try:
        yield
    finally:
        _busy.discard(session_id)


def turn(x_demo_session: str = Header("", max_length=64)):
    """session(), held in busy() until the request ends. Taken before the session is checked, so one that just ended
    is a 401. Use with scope="function": the session is given back before the answer leaves, ready for the next
    message. Not for a chat message, which outlives the function: main.demo_chat hands busy() to its thread."""
    with busy(x_demo_session):
        yield session(x_demo_session)


router = APIRouter(dependencies=[Depends(enabled)])


class SaleIn(BaseModel):
    item: str = Field(min_length=1, max_length=60)
    amount: Decimal = Field(ge=1, le=1000, max_digits=6, decimal_places=2)


@router.get("/demo", include_in_schema=False)
def page():
    return FileResponse(PAGE)


@router.get("/", include_in_schema=False)
def root():  # the service's bare URL opens the demo; 404 like every demo route when DEMO_MODE is off
    return RedirectResponse("/demo")


@router.post("/demo/api/session")
def new_session(request: Request):
    ratelimit.hit(request, "session")
    _forget(db.end_demo_sessions())  # ponytail: idle sessions are swept here and at boot; a cron job if traffic needs it
    session_id = secrets.token_urlsafe(24)
    db.new_demo_session(session_id)
    _greet(session_id)  # no busy(): nobody has this id yet
    return {"session_id": session_id, "client_id": os.environ["PAYPAL_CLIENT_ID"], "currency": CURRENCY,
            "buyer_email": os.environ["DEMO_BUYER_EMAIL"], "buyer_password": os.environ["DEMO_BUYER_PASSWORD"],
            "intro": chat.intro(True)}  # not in the transcript: fixed text, nothing of the seller's


def _paypal_error(r, what: str) -> HTTPException:
    """502 with PayPal's own issue code (INSTRUMENT_DECLINED, ORDER_NOT_APPROVED…), so the tester sees why."""
    try:
        body = r.json()
        issue = (body.get("details") or [{}])[0].get("issue") or body.get("name")
    except (ValueError, AttributeError, IndexError):
        issue = None
    return HTTPException(502, f"PayPal refused the {what} ({r.status_code}{', ' + issue if issue else ''})")


def _no_answer(e: Exception, what: str) -> HTTPException:
    """502 for a PayPal call that failed without an answer to show (a timeout, a refused connection, an odd body), as
    the tester's note, not a bare 500. A write may still have happened: a capture sent again is the same capture (its
    PayPal-Request-Id), an order sent again is a second unpaid order, and tracking sent again can be a second tracker
    (RISKS.md, accepted for the sandbox)."""
    log.warning("PayPal %s failed: %s", what, type(e).__name__)
    return HTTPException(502, f"PayPal didn't answer the {what} ({type(e).__name__}). Try again in a moment.")


@router.post("/demo/api/orders")
def create_order(body: SaleIn, request: Request, session_id: str = Depends(session)):
    ratelimit.hit(request, "sale")
    item, amount = scam_check.sanitize(body.item) or "Item", f"{body.amount:.2f}"
    try:
        r = paypal._call("POST", "/v2/checkout/orders", headers={"PayPal-Request-Id": str(uuid.uuid4())}, json={
            "intent": "CAPTURE",
            "purchase_units": [{"description": item, "amount": {"currency_code": CURRENCY, "value": amount}}],
        })
    except Exception as e:
        raise _no_answer(e, "order") from e
    if r.is_error:
        raise _paypal_error(r, "order")
    order_id = r.json()["id"]
    db.add_demo_sale(session_id, order_id, item, amount)
    return {"order_id": order_id}


@router.post("/demo/api/orders/{order_id}/capture")
def capture_order(order_id: str, session_id: str = Depends(session)):
    if not db.demo_sale_exists(session_id, order_id):  # the id comes from the browser: only this session's sales
        raise HTTPException(404, "no such sale in this demo session")
    # a fixed request id: if the browser retries, PayPal returns the first capture instead of capturing again
    try:
        r = paypal._call("POST", f"/v2/checkout/orders/{order_id}/capture",
                         headers={"PayPal-Request-Id": f"capture-{order_id}"}, json={})
    except Exception as e:
        raise _no_answer(e, "capture") from e
    if r.is_error:
        raise _paypal_error(r, "capture")
    try:
        cap = r.json()["purchase_units"][0]["payments"]["captures"][0]
        out = {"capture_id": cap["id"], "status": cap["status"], "amount": cap["amount"]["value"],
               "currency": cap["amount"]["currency_code"]}
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(502, "PayPal's capture response had no capture in it")
    db.set_demo_capture(order_id, out["capture_id"], out["status"])
    return out


@router.post("/demo/api/orders/{order_id}/ship")
def ship_order(order_id: str, request: Request, session_id: str = Depends(session)):
    """Test tracking on a paid sale of this session, so a check of its payment gives WAIT_SHIPPED. Orders v2's track
    call lists no PayPal-Request-Id (its schema, read 2026-11-02): the sale is marked before the call, so a second
    click while it runs is a 409. The fixed request id is sent anyway. An answer lost after PayPal added the tracker
    lets a later click add a second one (RISKS.md)."""
    ratelimit.hit(request, "ship")
    if not db.demo_sale_exists(session_id, order_id):  # the id comes from the browser: only this session's sales
        raise HTTPException(404, "no such sale in this demo session")
    tracking = f"VG-DEMO-{order_id}"  # the same body on every try
    if not (capture_id := db.claim_demo_tracking(session_id, order_id, tracking)):
        raise HTTPException(409, "this sale isn't paid, or it already has tracking")
    try:
        r = paypal._call("POST", f"/v2/checkout/orders/{order_id}/track",
                         headers={"PayPal-Request-Id": f"track-{order_id}"},
                         json={"capture_id": capture_id, "tracking_number": tracking, "carrier": "OTHER",
                               "carrier_name_other": "GoodToShip demo", "notify_payer": False})
        if r.is_error:
            raise _paypal_error(r, "tracking")
    except Exception as e:  # refused, or no answer (which may still have added it): unmarked, so a click can try again
        try:
            db.release_demo_tracking(order_id, tracking)
        except Exception as e2:  # PayPal's error is the one the tester needs; the sale stays marked
            log.error("release_demo_tracking failed: %s", type(e2).__name__)
        if isinstance(e, HTTPException):
            raise
        raise _no_answer(e, "tracking") from e
    return {"tracking": tracking}


@router.get("/demo/api/state")
def state(session_id: str = Depends(session)):
    out = db.demo_state(session_id)
    # live, not saved with the session: the page shows its operator section, and the intro offers a person, only then
    intro = chat.intro(True)
    for h in out["handoffs"]:  # the page shows it once, where the chat started over
        if h["status"] == "closed":
            h["notice"] = f"{chat.CLOSED}\n\n{intro}"
    # what the chat waits for, so a reloaded page never sends a dispute ID as the buyer's chat (it reads memory only)
    awaiting = chat_graph.get_state(_thread(session_id)).values.get("awaiting")
    # practice: the Practice disputes tab's fictional scenarios, from a file, any session's
    return {**out, "operators": True, "intro": intro, "practice": dispute_draft.cards(),
            "awaiting": awaiting}


@router.post("/demo/api/end")
def end(session_id: str = Depends(turn, scope="function")):
    _forget(db.end_demo_sessions(session_id))
    return {"ended": True}


# The operator is simulated (2026-11-08): the tester plays the person from GoodToShip on their own page, so each
# session sees and answers only its own shared conversation, never another tester's. No token.
@router.get("/demo/api/operator/handoffs")
def operator_handoffs(session_id: str = Depends(session)):
    """This session's open handoff: its transcript and last check's codes, never the session id."""
    return db.open_handoffs(session_id)


class ReplyIn(BaseModel):
    reply: str = Field(min_length=1, max_length=MAX_REPLY)


GONE = "this conversation was closed, or its demo session ended"


@router.post("/demo/api/operator/handoffs/{handoff_id}/reply")
def operator_reply(body: ReplyIn, handoff_id: int = UrlPath(ge=1, le=2**63 - 1),  # a bigint, or psycopg's 500
                   session_id: str = Depends(session)):
    """A person's answer, written by a person, as many as they like while it is open: the seller's page shows each
    from demo_state, and none enters the graph."""
    if not db.answer_handoff(handoff_id, body.reply, session_id):
        raise HTTPException(409, GONE)
    return {"answered": True}


@router.post("/demo/api/operator/handoffs/{handoff_id}/close")
def operator_close(handoff_id: int = UrlPath(ge=1, le=2**63 - 1), session_id: str = Depends(session)):
    if db.open_handoff_session(handoff_id) != session_id:  # another session's is "gone", as a closed one
        raise HTTPException(409, GONE)
    # never under one of the seller's messages: it could reach a half-wiped chat
    with busy(session_id, "the seller's last message is still on its way, try again in a moment"):
        _close(session_id, handoff_id)
    return {"closed": True}


@router.post("/demo/api/handoff/close")
def seller_close(session_id: str = Depends(turn, scope="function")):
    """The seller's Stop sharing."""
    _close(session_id)
    return {"closed": True}


def _close(session_id: str, handoff_id: int | None = None) -> None:
    """The open handoff closed and the chat started over: the thread and the transcript go, so GoodToShip forgets
    everything before, and greets again (state's notice). Inside busy()."""
    if not db.close_handoff(session_id, handoff_id):
        raise HTTPException(409, GONE)
    _forget([session_id])
    _greet(session_id)


def _greet(session_id: str) -> None:
    """GoodToShip speaks first: the page shows the intro, so the thread is greeted and a "Hi" gets the one-line
    reply."""
    chat_graph.update_state(_thread(session_id), {"greeted": True}, as_node="other")


def _forget(session_ids: list[str]) -> None:
    """The chat thread and the transcript hold what the seller typed, the buyer's chat too (RECHECK #19): they go with
    the session."""
    for session_id in session_ids:
        chat_graph.checkpointer.delete_thread(session_id)
        _transcripts.pop(session_id, None)


def _thread(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _keep_latest(session_id: str) -> None:
    """The thread's latest checkpoint only, without its messages. InMemorySaver keeps every checkpoint, a screenshot in
    each, until delete_thread, and nothing reads a thread's history, nor a turn's messages once it ended.
    The saver's public API only. Inside the turn's busy()."""
    saver = chat_graph.checkpointer
    latest = saver.get_tuple(_thread(session_id))
    # stored as [], not left out: a reducer channel with a version and no value is rebuilt from the parents on every
    # load, LangGraph's path for migrated threads. Every turn starts by removing them anyway
    latest.checkpoint["channel_values"]["messages"] = []
    saver.delete_thread(session_id)
    # no checkpoint_id: stored with no parent; every channel's version, so every value is stored again. Its pending
    # writes go: a new turn's input never applies them
    saver.put({"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}, latest.checkpoint, latest.metadata,
              latest.checkpoint["channel_versions"])


def runs_check(session_id: str, message: str, image: bytes | None = None) -> list[str]:
    """The hourly limits this message takes, decided as chat decides: "check" for a check or a lookup
    (chat.runs_scam_check), "screenshot" for a screenshot read now, held for the price or checked
    (chat.reads_screenshot). None while a person has the conversation: chat_turn relays the message, unread. Asked of
    Postgres, not of the thread's consented: a restart forgets the thread, not the handoff."""
    if db.handoff_open(session_id):
        return []
    values = chat_graph.get_state(_thread(session_id)).values
    return [*(["check"] if chat.runs_scam_check(values, message, image) else []),
            *(["screenshot"] if chat.reads_screenshot(values, message, image) else [])]


def chat_turn(session_id: str, message: str, image: bytes | None, checks: list[str]):
    """One message through the chat graph, as main.demo_chat's lines: a check's PayPal calls and node updates as they
    happen, then the reply and how it was routed, for the live panel. checks is runs_check's answer, which
    main._chat took inside the same busy(). The turn joins the session's transcript, and a
    yes to handoff's question hands the whole transcript to a person (db.add_handoff). While a person has it, the
    message goes to them only, as "relayed": no graph, no LLM, no check (the yes left nothing awaited, so checks is
    empty)."""
    nodes, state = {}, {}
    url = None if image is None else chat.data_url(image)
    # sanitized, as the check and classify read it: the operator sees what GoodToShip saw ("price < 40" as
    # "price 40"), so a reply that went wrong on the cleaned text can be traced to it
    said = {"who": "seller", "text": scam_check.sanitize(message)}
    if url and "screenshot" in checks:  # read in this turn: held for the price, or by the check
        said["image"] = url
    elif url:  # never read: dropped at the consent step, or by cancel
        said["text"] = f"{said['text']}\n[screenshot, not read]".strip()
    if db.relay(session_id, said):  # a database error fails closed: GoodToShip doesn't answer either
        yield {"type": "relayed"}
        return
    # the thread is in memory, the session in Postgres: after a restart (Render's sleep, a deploy, --reload) the page
    # still works, and a price sent for a check that was forgotten would get the intro without a word
    restarted = not chat_graph.get_state(_thread(session_id)).values
    if restarted:
        _greet(session_id)
    content = message if image is None else [
        {"type": "text", "text": message},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    # the thread's messages start afresh each turn: no node reads past the newest. _keep_latest drops them when a turn
    # ends; this clears what a turn the graph raised in left
    turn_input = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), {"role": "user", "content": content}]}
    # the run's context, not configurable, so it isn't copied into the checkpoints. Only the demo has an operator page,
    # so only it offers a person. LIVE: check streams its
    # steps, only for a message that runs one: the custom stream moves every node onto LangGraph's background threads
    context = {"operators": True, **({"live": chat.LIVE} if checks else {})}
    case_id = None
    # durability="exit": one checkpoint, as the run ends (an error too), not one per step with the screenshot in each
    for mode, chunk in chat_graph.stream(turn_input, _thread(session_id), context=context, durability="exit",
                                         stream_mode=["custom", "updates", "values"] if checks else ["updates", "values"]):
        if mode == "custom":  # a check's step, for the live panel only
            if chunk["type"] == "node" and chunk["node"] == "ingest_input":
                case_id = chunk["update"]["case_id"]
            yield chunk
        elif mode == "updates":
            nodes.update(chunk)
        else:
            state = chunk  # the last one is this run's final state
    if case_id:  # so ending the session deletes it. After the run, which saved the check in the thread whatever happens
        db.add_demo_case(session_id, case_id)
    # after add_demo_case, so a failure here never loses the case's link. Not when the graph raised: that turn's
    # checkpoint stays until the next turn ends
    _keep_latest(session_id)
    reply = state["messages"][-1].content
    if restarted:  # "say check" only when this message didn't start one, nor ask about sharing
        reply = f"{chat.RESTARTED}{'' if state.get('awaiting') else chat.RESTARTED_CHECK}\n\n{reply}"
    route =next(node for node in nodes if node != "classify")
    pair = [said, {"who": "goodtoship", "text": reply}]
    if nodes.get("handoff", {}).get("consented"):  # the row holds everything so far, this turn too
        db.add_handoff(session_id, [*_transcripts.get(session_id, []), *pair], (state.get("cases") or [None])[-1])
    _transcripts.setdefault(session_id, []).extend(pair)  # after the row: a refused one leaves no "Sent" behind
    classified = nodes.get("classify") or {}
    # held: screenshots or payment IDs wait for the price, so the page's scenario chips don't send one (demo.html)
    # dispute_id: the ID the dispute node read, so the live panel tells a read from an explanation. checklist: a
    # practice dispute's, grouped, for the Practice tab's boxes: dispute_draft's own words, run again (a file, no PayPal),
    # its steps streamed for the live panel. Only when the chat's own run answered (its reply starts with SIMULATED,
    # not PRACTICE_FAILED), and a failure here costs the panel only, never the reply. ponytail: this run's steps, not
    # the chat's; the same file, so the same result. Stream the chat's own run (chat.LIVE in _read_dispute) if they
    # ever differ
    did = chat.dispute_id(message) if route == "dispute" else None
    steps, practice = [], None
    if did and dispute_draft.SIM_ID.fullmatch(did) and dispute_draft.SIMULATED in reply:
        try:
            practice = dispute_draft.run(did, steps.append)
        except Exception as e:
            log.warning("dispute_draft failed for the panel: %s", type(e).__name__)
            steps = []
    yield from steps
    yield {"type": "reply", "reply": reply, "route": route, "intent": classified.get("intent"),
           "notes": classified.get("entries", []), "awaiting": state.get("awaiting"), "held": bool(state.get("held")),
           "dispute_id": did,
           "checklist": practice and practice["checklist"]}
