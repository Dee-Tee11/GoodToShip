# GoodToShip

**Should I ship this order?** GoodToShip answers that for PayPal sellers. A seller pastes a buyer's chat or a screenshot, and GoodToShip checks the payment the buyer claims against PayPal itself and says: ship, wait, or don't ship.

Built for the PayPal AI Hackathon (deadline 12 Nov 2026, 20:00 Lisbon). PayPal Sandbox only, no real cases.

- **Live demo:** [goodtoship.onrender.com](https://goodtoship.onrender.com) (sell something, pay as a test buyer, ask the chat to check the buyer's messages, watch every step live)
- **Demo video:** [youtu.be/txNc40kjuZ0](https://youtu.be/txNc40kjuZ0)
- **Full reference** (every graph, rule, endpoint and test): [docs/DETAILS.md](docs/DETAILS.md)

## The problem

Payment scams hit sellers before shipping: a fake "payment sent" email, an overpayment with a request to refund the difference, a payment already used for an earlier order, a screenshot of a payment that doesn't exist. Once the item ships, it's gone.

The losses are real and documented. In the UK alone, the police's Action Fraud unit counted about **£7.9M lost to fake PayPal emails from January to September 2020** ([Action Fraud, 2020](https://www.actionfraud.police.uk/alert/fake-paypal-emails-lead-to-nearly-8-million-in-losses-this-year)), after **£1.12M in the last quarter of 2019** ([Action Fraud, 2019](https://www.actionfraud.police.uk/alert/fake-paypal-emails-lead-to-over-1-million-in-losses); [Computer Weekly](https://www.computerweekly.com/news/252477940/Scammers-using-fake-PayPal-emails-stole-over-1m-in-the-UK-in-last-quarter)). Sellers on the [eBay community forum](https://community.ebay.com/t5/Selling/Scam-buyer/m-p/31183904) describe the same story: an email that looked like PayPal, an item shipped, no money.

It is getting harder to spot by eye. In 2026 scammers sent fake purchase notices through **genuine PayPal emails** ([Malwarebytes, April 2026](https://www.malwarebytes.com/blog/news/2026/04/more-paypal-emails-hijacked-to-deliver-tech-support-scams)), and phishing overall reached **1,069,681 attacks in Q2 2026**, its highest level in years ([APWG Q2 2026](https://docs.apwg.org/reports/apwg_trends_report_q2_2026.pdf)). A sender check or a careful look at the email no longer settles it.

The seller has a chat, a screenshot and a few minutes to decide. PayPal has the facts, but nobody reads them for the seller. GoodToShip does.

## What it does

| | |
|---|---|
| **Check a buyer** (`scam_check`) | Reads the chat or screenshot, finds the payment ID, asks PayPal whether that payment exists, is completed, matches the seller's price, is recent, has no tracking yet and wasn't used before. Returns a verdict and next steps. |
| **Ask a question** (`chat`) | Answers PayPal questions (holds, Seller Protection, disputes, refunds) from checked notes, and says what a check's result means and what to do next. |
| **Practise a dispute** (`dispute_draft`) | On a fictional dispute, shows a ✓/✗/? checklist of what the seller can prove and a draft response to paste in PayPal. |
| **Ask for a person** | Hands the conversation to an operator, only after the seller says yes. In the demo the tester plays the operator. |

## How it works

```
Seller ──► chat ──► scam_check ──► PayPal REST (reads only)
            │            │
            │            └─► LLM: warning signs and 1–3 sentences, no tools
            └─► dispute_draft (templates, no LLM, no PayPal)
```

Three LangGraph graphs on FastAPI, deployed on Render, with Postgres on Neon. The verdict table, the chat's routing and every number the seller reads are plain code.

## Tools used

- **PayPal (the core).** REST sandbox: Transaction Search (`/v1/reporting/transactions`) and Payments v2 captures to check the payment the buyer claims; Tracking (`/v1/shipping/trackers`) to see if it already shipped; `verify-webhook-signature` for webhooks. The demo also uses Orders v2 (create, capture) to play the buyer. Everything in the graphs is a read.
- **AI: Groq, `openai/gpt-oss-120b`** (strict JSON-schema output) finds warning signs in the chat, routes the chat's intent and writes at most 1 to 3 checked sentences. `qwen/qwen3.8-27b` reads screenshots. **LangGraph** runs the three graphs; **LangSmith** traces them (EU, made-up chats only).
- **Tavily** searches PayPal's own pages when no verified note matches (optional).
- **Sponsor: Render** hosts the live demo (`render.yaml`); Postgres is on Neon.

## Decisions and why

**The LLM never decides, and never states a fact.**
`compute_verdict` is a pure function and the only writer of the verdict (`@owns` on each state field stops any other node). The LLM finds warning signs, each with the buyer's exact words, and a sign counts only if those words are really in the chat. It writes at most 1 to 3 sentences, with sources, and code checks each one: facts come in as placeholders filled by code, and numbers, links and verdict words that its sources don't hold are rejected. Two failures give a fixed template.
*Why:* the seller acts on this, in money. A confident wrong "you can ship" costs more than a missing sentence.

**The buyer is untrusted, and the LLM has no tools.**
The chat, email and screenshot text are data. They are sanitised, fenced in tags and never reach the chat's classifier or the web search. The LLM has no credentials and no tools, and a message that tries to give it instructions gets a fixed reply.
*Why:* a scammer writes the input.

**PayPal calls are reads.**
No graph writes to PayPal: the seller acts in PayPal. The only writes are the demo's three sandbox calls (create order, capture, add test tracking), outside the graphs, to play the buyer and the shipment.
*Why:* GoodToShip checks and helps. It must not do anything that changes a payment.

**"Not found" never means scam alone.**
PayPal gives buyer and seller different transaction IDs for the same payment, and Transaction Search can lag by hours. A payment PayPal can't find is `WAIT`, and it is `SCAM_LIKELY` only with two confirmed warning signs. A PayPal outage is "unavailable", never "not found".
*Why:* wrongly calling an honest buyer a scammer is also a failure.

**Answers come from verified notes, not from the model's memory.**
The chat shows notes word for word, each from one PayPal US help page, looked up by key and offered only after a person read the page and set `verified`. Nothing matching is found by similarity. With no note, the chat searches PayPal's US pages, or says "I don't have that information" and links PayPal. A test fetches every page and checks the sentences are still there.
*Why:* a plausible but invented PayPal rule is the worst answer a seller can get.

**Practice disputes only (2026-11-07).**
We first built reading real disputes, then removed it. A real response needs a carrier's delivery data and the seller's listing, which PayPal doesn't give, and in the sandbox a sale can be disputed only 30 minutes to 3 hours after it. So disputes run on fictional scenarios (`PP-SIM-…`), with no PayPal call and no LLM. A real dispute ID gets a fixed text pointing to PayPal's Resolution Center.
*Why:* a coherent demo beats a half-real tool.

**One model, `openai/gpt-oss-120b`.**
Groq's strict JSON-schema output holds on it. `qwen/qwen3.8-27b` failed strict JSON often (15/40 on the chat eval against 31/40) and is used only to read screenshots.
*Why:* every call depends on a closed, parseable choice.

**A person for what the notes don't cover.**
When the chat can't help, it offers an operator, and the conversation is shared only if the seller says yes. The demo's operator is a simulation on the tester's own page, with no token.
*Why:* escalate honestly rather than guess.

**Demo scope (2026-11-08).**
No seller login, no approval board, the chat's conversation in memory, English only. Each demo session is anonymous, deleted when it ends or after 2 hours, with per-IP limits.
*Why:* the product shown is the sandbox demo, and these served features that were dropped. Open review points that depend on auth are out of scope, not blockers ([RECHECK.md](RECHECK.md)).

## How we measure it

- **695 offline tests** (PayPal and Groq mocked): the verdict table, the guardrails, every chat route, the demo's limits and isolation. `tests/test_db.py` runs the real SQL against a throwaway Postgres.
- **Evals with the real model**, with made-up chats only: E06 (44 chats, routing), E07 (61 seller conversations, scored by rules and a judge), E08 (the 33 cases E07 failed most), E09 (every PayPal note still matches its page), E10 (can-I-ship and what-now phrasings), and screenshot evals with drawn chats.
- Failures found by the evals are kept as open points, not hidden: see [RECHECK.md](RECHECK.md) (for example #52, the answer step falling back on some follow-ups) and [RISKS.md](RISKS.md).

## Limits, said plainly

- Sandbox only, no real money, no real cases. LangSmith tracing is off unless `LANGSMITH_TRACING=true`, for made-up chats only.
- The warning-sign check reads closed patterns in a chat. There is no measured catch rate on real scams yet (RECHECK #16).
- The chat's answer step can still fall back to quoting the notes (RECHECK #52).
- Groq's free tier fits about 2 checks a minute. Under load, checks fail closed: signals unknown, fixed template.
- The demo has no login, and its `/scam-check` endpoint is open with per-IP limits (RECHECK #3).
- Seller Protection notes assume a US business account.

## Run it

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # PayPal sandbox keys, GROQ_API_KEY, DATABASE_URL; DEMO_MODE=true for /demo
                        # PAYPAL_WEBHOOK_ID is required to start: any placeholder works without webhooks
docker run -d -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres
uvicorn app.main:app --reload --env-file .env
pytest                   # offline
pytest -m eval -s        # real Groq model, keys from .env
```

Open `/demo`. To see the graphs node by node: `langgraph dev --studio-url https://eu.smith.langchain.com`. Variables, the Render deploy and the webhook setup are in [docs/DETAILS.md](docs/DETAILS.md#configuration).

## Where things are

```
app/scam_check.py     the check graph and its verdict table
app/chat.py           the chat graph
app/dispute_draft.py  practice disputes
app/policy_pack.py    the verified notes
app/output_guardrails.py  the checks on LLM-written sentences
app/paypal.py         PayPal reads (and the webhook's signature check)
app/demo.py, app/static/demo.html   the live demo
tests/                pytest, evals and their datasets
CHANGES.md · RECHECK.md · RISKS.md   what changed, what is open, what could go wrong
```

MIT licence.
