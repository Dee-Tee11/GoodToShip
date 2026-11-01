"""The live demo: a tester makes a sale, pays it in PayPal's sandbox checkout with a shared buyer account, then asks
the chat graph (main.demo_chat), one conversation per session, to check the buyer's chat, and watches every node and
PayPal call of the check as it runs. With their yes, the seller can hand the conversation to a person from Vendor Guard
(the handoffs table): the seller's messages then go to that person and Vendor Guard doesn't answer, until the seller or
the person closes it. Closing deletes what the person saw and starts the chat over, as a new session's.

On only with DEMO_MODE=true, and only against PayPal's sandbox (main.lifespan refuses to start otherwise). Creating and
capturing orders are the app's only PayPal writes. They play the buyer's purchase and stay out of the graphs.
The session is anonymous: the page keeps its id in sessionStorage, and everything goes when it ends.
"""
import base64
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
from fastapi.responses import FileResponse
from langchain_core.messages import RemoveMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pydantic import BaseModel, Field

from app import chat, db, llm, paypal, ratelimit, scam_check

log = logging.getLogger(__name__)
ENABLED = os.getenv("DEMO_MODE", "").strip().lower() == "true"
REQUIRED_ENV = ("DEMO_BUYER_EMAIL", "DEMO_BUYER_PASSWORD")
CURRENCY = "USD"  # a US sandbox business account, as in the README's Seller Protection notes
# the people who answer handoffs share it; off when unset. main.lifespan refuses one under 32 characters
OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "").strip()
MAX_REPLY = 2000
PAGE = Path(__file__).parent / "static" / "demo.html"
# the chat graph with one thread per demo session (thread_id = session id), forgotten when the session ends.
# ponytail: in memory, so a restart or Render's sleep forgets every conversation; PostgresSaver with dispute_defense
chat_graph = chat._build(InMemorySaver())
# each session's conversation, for a handoff, kept only with OPERATOR_TOKEN: what the seller sent (a screenshot only
# when a check read it) and every reply. Outside the graph, so InMemorySaver doesn't copy it into every checkpoint.
# ponytail: no cap; 30 messages and 3 checked screenshots an hour per IP, and 30 idle minutes, bound it
_transcripts: dict[str, list[dict]] = {}
_busy: set[str] = set()  # sessions inside busy(): a chat message or "End demo"
_busy_lock = threading.Lock()  # turn() runs in the threadpool


def enabled():
    if not ENABLED:
        raise HTTPException(404)


def session(x_demo_session: str = Header("", max_length=64)) -> str:
    """The caller's demo session, kept alive by this request. 401 once it ended or went idle."""
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


def operator(request: Request, x_operator_token: str = Header("", max_length=256)) -> None:
    """A person from Vendor Guard: the shared OPERATOR_TOKEN, in a header only, never in a URL. 404 when it isn't set,
    401 for a wrong one, before any database call; an address at its limit of wrong ones is a 429, the right one too."""
    if not OPERATOR_TOKEN:
        raise HTTPException(404)
    ratelimit.hit(request, "wrong token", count=False)  # before the compare, or a 200 would still tell a guess apart
    if not secrets.compare_digest(x_operator_token.encode(), OPERATOR_TOKEN.encode()):
        ratelimit.hit(request, "wrong token")  # only a wrong one counts: the page polls every 4 s
        raise HTTPException(401, "wrong operator token")


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


@router.post("/demo/api/session")
def new_session(request: Request):
    ratelimit.hit(request, "session")
    _forget(db.end_demo_sessions())  # ponytail: idle sessions are swept here and at boot; a cron job if traffic needs it
    session_id = secrets.token_urlsafe(24)
    db.new_demo_session(session_id)
    _greet(session_id)  # no busy(): nobody has this id yet
    return {"session_id": session_id, "client_id": os.environ["PAYPAL_CLIENT_ID"], "currency": CURRENCY,
            "buyer_email": os.environ["DEMO_BUYER_EMAIL"], "buyer_password": os.environ["DEMO_BUYER_PASSWORD"],
            "intro": chat.intro(bool(OPERATOR_TOKEN))}  # not in the transcript: fixed text, nothing of the seller's


def _paypal_error(r, what: str) -> HTTPException:
    """502 with PayPal's own issue code (INSTRUMENT_DECLINED, ORDER_NOT_APPROVED…), so the tester sees why."""
    try:
        body = r.json()
        issue = (body.get("details") or [{}])[0].get("issue") or body.get("name")
    except (ValueError, AttributeError, IndexError):
        issue = None
    return HTTPException(502, f"PayPal refused the {what} ({r.status_code}{', ' + issue if issue else ''})")


@router.post("/demo/api/orders")
def create_order(body: SaleIn, request: Request, session_id: str = Depends(session)):
    ratelimit.hit(request, "sale")
    item, amount = scam_check.sanitize(body.item) or "Item", f"{body.amount:.2f}"
    r = paypal._call("POST", "/v2/checkout/orders", headers={"PayPal-Request-Id": str(uuid.uuid4())}, json={
        "intent": "CAPTURE",
        "purchase_units": [{"description": item, "amount": {"currency_code": CURRENCY, "value": amount}}],
    })
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
    r = paypal._call("POST", f"/v2/checkout/orders/{order_id}/capture",
                     headers={"PayPal-Request-Id": f"capture-{order_id}"}, json={})
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


@router.get("/demo/api/state")
def state(session_id: str = Depends(session)):
    out = db.demo_state(session_id)
    # live, not saved with the session: the page shows its operator section, and the intro offers a person, only then
    intro = chat.intro(bool(OPERATOR_TOKEN))
    for h in out["handoffs"]:  # the page shows it once, where the chat started over
        if h["status"] == "closed":
            h["notice"] = f"{chat.CLOSED}\n\n{intro}"
    return {**out, "operators": bool(OPERATOR_TOKEN), "intro": intro}


@router.post("/demo/api/end")
def end(session_id: str = Depends(turn, scope="function")):
    _forget(db.end_demo_sessions(session_id))
    return {"ended": True}


@router.get("/demo/api/operator/handoffs", dependencies=[Depends(operator)])
def operator_handoffs():
    """Every session's open handoff: its transcript and last check's codes, never the session id."""
    return db.open_handoffs()


class ReplyIn(BaseModel):
    reply: str = Field(min_length=1, max_length=MAX_REPLY)


GONE = "this conversation was closed, or its demo session ended"


@router.post("/demo/api/operator/handoffs/{handoff_id}/reply", dependencies=[Depends(operator)])
def operator_reply(body: ReplyIn, handoff_id: int = UrlPath(ge=1, le=2**63 - 1)):  # a bigint, or psycopg's 500
    """A person's answer, written by a person, as many as they like while it is open: the seller's page shows each
    from demo_state, and none enters the graph."""
    if not db.answer_handoff(handoff_id, body.reply):
        raise HTTPException(409, GONE)
    return {"answered": True}


@router.post("/demo/api/operator/handoffs/{handoff_id}/close", dependencies=[Depends(operator)])
def operator_close(handoff_id: int = UrlPath(ge=1, le=2**63 - 1)):
    if not (session_id := db.open_handoff_session(handoff_id)):
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
    """The open handoff closed and the chat started over: the thread and the transcript go, so Vendor Guard forgets
    everything before, and greets again (state's notice). Inside busy()."""
    if not db.close_handoff(session_id, handoff_id):
        raise HTTPException(409, GONE)
    _forget([session_id])
    _greet(session_id)


def _greet(session_id: str) -> None:
    """Vendor Guard speaks first: the page shows the intro, so the thread is greeted and a "Hi" gets the one-line
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


def runs_check(session_id: str, message: str, image: bytes | None = None) -> bool:
    """True when this message runs a scam check, decided as chat.check decides it."""
    return chat.runs_scam_check(chat_graph.get_state(_thread(session_id)).values, message, image)


def chat_turn(session_id: str, message: str, image: bytes | None, checks: bool):
    """One message through the chat graph, as main.demo_chat's lines: a check's PayPal calls and node updates as they
    happen, then the reply and how it was routed, for the live panel. checks is runs_check's answer, which
    main._chat took inside the same busy(). With OPERATOR_TOKEN, the turn joins the session's transcript, and a
    yes to handoff's question hands the whole transcript to a person (db.add_handoff). While a person has it, the
    message goes to them only, as "relayed": no graph, no LLM, no check (the yes left nothing awaited, so checks is
    False)."""
    nodes, state = {}, {}
    url = None if image is None else f"data:{llm.image_type(image)};base64,{base64.b64encode(image).decode()}"
    # sanitized, as the check and classify read it: the operator sees what Vendor Guard saw ("price < 40" as
    # "price 40"), so a reply that went wrong on the cleaned text can be traced to it
    said = {"who": "seller", "text": scam_check.sanitize(message)}
    if url and checks:  # only a check reads a screenshot (#26)
        said["image"] = url
    elif url:  # never read, and the seller was told it isn't kept
        said["text"] = f"{said['text']}\n[screenshot, not read]".strip()
    if OPERATOR_TOKEN and db.relay(session_id, said):  # a database error fails closed: Vendor Guard doesn't answer either
        yield {"type": "relayed"}
        return
    content = message if image is None else [
        {"type": "text", "text": message},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    # the thread's messages start afresh each turn: no node reads past the newest, and InMemorySaver would store the
    # whole history again in every checkpoint (a screenshot too: it stays only until the next turn or the session ends)
    turn_input = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), {"role": "user", "content": content}]}
    # the run's context, not configurable, so it isn't copied into the checkpoints. Only the demo has an operator page,
    # and only with OPERATOR_TOKEN: without it nobody could answer, so no person is offered. LIVE: check streams its
    # steps, only for a message that runs one: the custom stream moves every node onto LangGraph's background threads
    context = {"operators": bool(OPERATOR_TOKEN), **({"live": chat.LIVE} if checks else {})}
    case_id = None
    for mode, chunk in chat_graph.stream(turn_input, _thread(session_id), context=context,
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
    reply = state["messages"][-1].content
    route = next(node for node in nodes if node != "classify")
    if OPERATOR_TOKEN:  # no operator page, no transcript: nobody would read it
        pair = [said, {"who": "vendor_guard", "text": reply}]
        if nodes.get("handoff", {}).get("consented"):  # the row holds everything so far, this turn too
            db.add_handoff(session_id, [*_transcripts.get(session_id, []), *pair], state.get("last_case"))
        _transcripts.setdefault(session_id, []).extend(pair)  # after the row: a refused one leaves no "Sent" behind
    classified = nodes.get("classify") or {}
    yield {"type": "reply", "reply": reply, "route": route, "intent": classified.get("intent"),
           "notes": classified.get("entries", []), "awaiting": state.get("awaiting")}
