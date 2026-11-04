import base64
import binascii
import json
import logging
import os
import queue
import threading
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext
from decimal import Decimal
from functools import partial
from typing import Annotated

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from app import db, demo, llm, paypal, ratelimit, scam_check

log = logging.getLogger(__name__)


REQUIRED_ENV = ("DATABASE_URL", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID")


@asynccontextmanager
async def lifespan(_):
    # fail at boot, not on the first webhook: a missing value would otherwise pass /healthz and 500 every call
    required = REQUIRED_ENV + (demo.REQUIRED_ENV if demo.ENABLED else ())
    if missing := [k for k in required if not os.getenv(k, "").strip()]:
        raise RuntimeError(f"missing env vars: {', '.join(missing)}")
    if demo.ENABLED and ".sandbox." not in paypal.BASE:  # the demo pays with a shared account: never with real money
        raise RuntimeError("DEMO_MODE needs PayPal's sandbox (PAYPAL_BASE_URL)")
    if demo.ENABLED and demo.OPERATOR_TOKEN and len(demo.OPERATOR_TOKEN) < 32:  # it shows sellers' shared chats
        raise RuntimeError("OPERATOR_TOKEN must be at least 32 characters")
    db.init()
    if demo.ENABLED:
        db.end_demo_sessions()  # sessions that went idle while the service slept
    yield


# the biggest body we take: a 3 MB screenshot as 4 000 000 base64 chars, plus 8 000 chars of text escaped in JSON (up
# to 12 bytes each) and the keys. Checked on Content-Length before reading, and on the bytes read (chunked): 413
MAX_BODY = 4_200_000

app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY)  # RECHECK #3
app.include_router(demo.router)


@app.get("/healthz")
async def healthz():  # async: never waits for a worker thread, so busy checks can't fail Render's health check
    return {"ok": True}


def _handle(headers, body: bytes):
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid json")
    if not paypal.verify_webhook(headers, body):
        raise HTTPException(400, "bad signature")
    return {"status": "stored" if db.claim_event(event) else "duplicate"}


@app.post("/webhooks/paypal")
async def webhook(request: Request):
    return await run_in_threadpool(_handle, request.headers, await request.body())


def _b64(v: str) -> bytes:
    try:  # strict: the lenient decoder drops a data: prefix's letters into the bytes, then calls a real PNG "not a PNG"
        return base64.b64decode(v, validate=True)
    except binascii.Error:
        raise ValueError("the screenshot must be plain base64, with no data: prefix")


# capped as base64, before decoding: 4 MB of base64 is 3 MB of image, the cap older Groq docs gave for a base64
# image (Groq took 3.06 MB on 2026-10-21); a phone screenshot is far smaller
Screenshot = Annotated[str, Field(max_length=4_000_000), AfterValidator(_b64)]


class ScamCheckIn(BaseModel):
    text: str = Field("", max_length=scam_check.MAX_CHARS)
    image: Screenshot | None = None
    expected_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)  # the seller's price, never the buyer's
    expected_currency: str = Field(pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def _text_or_image(self):
        if not self.text and self.image is None:
            raise ValueError("send the chat as text, a screenshot, or both")
        if self.image is not None and not llm.image_type(self.image):
            raise ValueError("the screenshot must be a PNG or JPEG")
        return self


# ponytail: one global cap of 2 checks at a time; per-seller rate limit once auth lands (RECHECK #3).
# A thread's semaphore, not anyio's limiter: a demo check holds it in its own thread (_chat), with no task to own it
SCAM_LIMIT = threading.BoundedSemaphore(2)


def _admit(body: ScamCheckIn, request: Request) -> None:
    """The checks before a scam check runs: 422 for a chat too long, 429 for a caller over its hourly limit."""
    if len(scam_check.sanitize(body.text)) > scam_check.MAX_CHARS:  # NFKC can make text longer
        raise HTTPException(422, "chat too long")
    ratelimit.hit(request, "check", *(["screenshot"] if body.image else []))


@contextmanager
def _slot():
    """One of SCAM_LIMIT's slots for a check, or 503 when none is free."""
    if not SCAM_LIMIT.acquire(blocking=False):  # take the slot now: check-then-wait lets a whole burst through to queue
        raise HTTPException(503, "busy, try again")
    try:
        yield
    finally:
        SCAM_LIMIT.release()


@app.post("/scam-check")
async def scam_check_route(body: ScamCheckIn, request: Request):
    _admit(body, request)
    with _slot():
        try:
            run = partial(scam_check.run, body.text, str(body.expected_amount), body.expected_currency, body.image)
            return await anyio.to_thread.run_sync(run)
        except scam_check.Unreadable as e:
            raise HTTPException(422, str(e))


def _line(kind: str, **fields) -> bytes:
    return (json.dumps({"type": kind, **fields}, default=lambda o: o.model_dump() if isinstance(o, BaseModel) else str(o))
            + "\n").encode()


class ChatIn(BaseModel):
    message: str = Field("", max_length=scam_check.MAX_CHARS)
    image: Screenshot | None = None  # the buyer's chat as a screenshot, read only by the check

    @model_validator(mode="after")
    def _message_or_image(self):
        if not self.message and self.image is None:
            raise ValueError("send a message, a screenshot, or both")
        if self.image is not None and not llm.image_type(self.image):
            raise ValueError("the screenshot must be a PNG or JPEG")
        return self


def _chat(body: ChatIn, request: Request, session_id: str, held: ExitStack, put) -> None:
    """The whole message, in a thread of its own, holding the session's busy() until its end: it finishes even if the
    page goes away, so a check whose payment PayPal's claim took is always saved in the chat's thread. A message that
    runs a check, or has a screenshot read, takes those limits and a slot (demo.runs_check). An hourly limit or no free
    slot is an error line, and nothing is held."""
    with held:  # one message at a time: the thread can't change between the decision and the turn
        try:
            runs_check = demo.runs_check(session_id, body.message, body.image)
            ratelimit.hit(request, "message", *runs_check)
            with _slot() if runs_check else nullcontext():
                for line in demo.chat_turn(session_id, body.message, body.image, runs_check):
                    put(_line(line.pop("type"), **line))
        except HTTPException as e:
            put(_line("error", message=e.detail))
        except Exception as e:
            log.warning("demo chat failed: %s", type(e).__name__)
            put(_line("error", message="the message could not be answered, try again"))
    put(None)  # after busy() is given back: the page's next message never finds it taken


async def _read(lines: queue.SimpleQueue):
    # abandon_on_cancel: a page that goes away stops the reading, never the turn
    while (line := await anyio.to_thread.run_sync(lines.get, abandon_on_cancel=True)) is not None:
        yield line


@app.post("/demo/api/chat", dependencies=[Depends(demo.enabled)])
def demo_chat(body: ChatIn, request: Request, x_demo_session: str = Header("", max_length=64)):
    """One message to the chat graph, as NDJSON: a check's PayPal calls and node updates as they happen, then the
    reply. The updates show the tester what they sent (the sanitized chat, the screenshot's text, the quotes), which
    /scam-check never returns. busy() is taken before the session is checked, as demo.turn: a busy session is a 409,
    and one that just ended a 401. Then _chat's thread holds it."""
    with ExitStack() as held:
        held.enter_context(demo.busy(x_demo_session))
        session_id = demo.session(x_demo_session)
        lines = queue.SimpleQueue()
        threading.Thread(target=_chat, args=(body, request, session_id, held.pop_all(), lines.put), daemon=True).start()
    return StreamingResponse(_read(lines), media_type="application/x-ndjson")
