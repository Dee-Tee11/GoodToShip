# GoodToShip: the details

The short version is [../README.md](../README.md). This file is the full reference: every graph, rule, endpoint and test. Built for the PayPal AI Hackathon (deadline 12 Nov 2026, 20:00 Lisbon, PayPal Sandbox only).

[LangGraph](https://langchain-ai.github.io/langgraph/) graphs on FastAPI, deployed on Render, that protect a PayPal seller:

1. **`scam_check`: before shipping.** The seller pastes a buyer's chat, email or screenshot. GoodToShip checks the claimed payment against PayPal and returns a verdict: ship, wait, or don't ship.
2. **`dispute_draft`: practising a dispute response.** The seller pastes a practice dispute ID. GoodToShip shows a ✓/✗/? checklist of what the fictional case shows and a draft response to paste in PayPal. It never reads a real dispute: the seller answers their own in PayPal's Resolution Center.
3. **`chat`: the way in.** The seller writes in plain language (the demo page and LangGraph Studio's Chat tab today). The chat answers from checked notes, runs `scam_check`, runs `dispute_draft` on a practice dispute, or explains disputes.

PayPal is the source of truth in every graph, and GoodToShip never moves money: the graphs only read PayPal, and the seller acts in PayPal.

> **Status (2026-11-08):** everything below is built and tested offline; the demo runs on Render against PayPal's sandbox. Scope decisions are in the README's "Decisions and why". See [../CHANGES.md](../CHANGES.md) for what changed and [../RECHECK.md](../RECHECK.md) for open review points.

| Area | Status |
|---|---|
| Webhook intake: signature check and dedupe (`/webhooks/paypal`) | Done (tests U11, U12, S07) |
| Postgres storage, Render blueprint | Done, deployed |
| Seller authentication | Dropped for the submission (2026-11-08): the product shown is the anonymous sandbox demo |
| `scam_check` graph and `POST /scam-check` | Done, screenshots included, and `payment_check` for payment IDs the chat held (U01–U05, U09–U10, U17–U20, N01–N04, S01–S06) |
| Live sandbox demo (`/demo`): sell, pay as a test buyer, ask the chat to check the buyer's chat, watch every step | Built and deployed, run against the sandbox with a real payment (D01–D41) |
| `chat` graph and `policy_pack` | Done in Studio and on the demo page through `POST /demo/api/chat` (C01–C50, U16, U22, U26, E06, E07, E08, E10, D08, D10, D12–D17, D20–D22, D24, D25, D29, D32, D34, D50). No endpoint outside the demo (no auth, by decision) |
| `dispute_draft` graph: a checklist and a draft response on fictional practice disputes, no PayPal call | Done, reached from the chat and the demo's Practice disputes menu (U28–U30, U32, U35–U40, C45, C49, C50, D50, D51) |
| Evaluation set (E06–E10), demo video | Evals built and run; numbers and video for the submission |

## Trust boundaries

Everything that comes from the buyer (chat, email, screenshot) is **untrusted data**. The LLM has **no credentials and no tools**. No graph writes to PayPal: every graph only reads it, and the seller acts in PayPal. The one exception is the live demo (`DEMO_MODE`, sandbox only): code outside the graphs creates and captures orders there, to play the buyer's purchase, and adds test tracking to a paid one, to play the seller's shipment.

```
Seller UI ──► FastAPI (Render) ──► LangGraph ──► PayPal REST APIs, GETs only
 paste chat     /scam-check            │           payments, transaction search,
 practice ID    /demo/api/chat         ├──► LLM     tracking
                /webhooks/paypal       │   (structured output, no tools; not in dispute_draft)
 PayPal ──────► verify → dedupe        └──► Postgres
 Sandbox         DISPUTE.* CAPTURE.*        cases · claimed payments · webhook events
```

## Graph 1: `scam_check`

```mermaid
flowchart TD
  A[ingest_input] --> R[read_screenshot] --> B[sanitize_untrusted]
  B --> C[detect_signals]
  B --> D[extract_claims] --> E[verify_payment]
  C --> F[compute_verdict]
  E --> F
  F --> G[explain_and_reply] --> H{validate_output}
  H -- ok --> I[persist_case]
  H -- fail --> J[safe_template] --> I
```

`read_screenshot` runs only when the seller sends an image. A Groq vision model (`qwen/qwen3.8-27b`) copies its text and labels chat bubbles `Buyer:` (left) or `Seller:` (right). It writes `[?]` for words it can't read instead of guessing them. The copy is untrusted buyer text. It is joined to any pasted text and sanitised like a paste, so every later node works as before. If the copy fails, there is no verdict and no case: the API answers 422 and asks for another try or the pasted text.

A copy can still be wrong, so the quote check confirms a quote against the copy, not against the image. That cannot approve a payment, because PayPal still has to find the ID. A misread ID gives `WAIT`. The worst cases are a false warning, or a missed warning on a real payment, where the address rule is shown anyway.

`extract_claims` takes up to three 17-character transaction IDs from the chat, using a regex and no LLM. `verify_payment` tries each one against the capture API (`GET /v2/payments/captures/{id}`), then Transaction Search (which can lag by hours). Before it approves a completed payment of the seller's price, it reads the payment's tracking (`GET /v1/shipping/trackers?transaction_id=`): tracking means the payment already paid for an order the seller shipped, even one never checked here. Every tracker counts except a `CANCELLED` one, the only status PayPal documents as "the tracking number no longer applies", so a printed label counts too. Tracking it can't read counts as not confirmed.

The webhook-fed ledger is not consulted yet. Stored bodies are only CRC32-bound and never re-fetched from PayPal (the `ponytail:` note in `app/main.py`), and refund events don't carry the capture ID, so a ledger hit could approve a refunded or forged payment.

A PayPal 403, 429, 5xx, timeout or token failure counts as "unavailable", never as "not found". The amount is compared only to the seller's `expected_amount` and `expected_currency`, never to what the buyer claims.

`compute_verdict` is a pure function with no LLM, and it is the only node that writes `state.verdict`.

| Payment (API) | Signals | Verdict | Message to seller |
|---|---|---|---|
| Found, `DECLINED`, `FAILED`, `REFUNDED`, `PARTIALLY_REFUNDED` or `REVERSED` | any | `WAIT` | Don't ship. This payment was declined, failed or refunded. |
| Found, amount or currency differs from the seller's price | any | `WAIT` | Don't ship. The amount paid is not your price. |
| Found, `PENDING` or any status but `COMPLETED` | any | `WAIT` | Don't ship yet. |
| Found, `COMPLETED`, amount matches, more than 14 days old | any | `WAIT` | Don't ship. This payment is more than 14 days old, so it is likely from an earlier order. |
| Found, `COMPLETED`, amount matches, tracking already added in PayPal | any | `WAIT` | Don't ship. This payment already has tracking in PayPal, so it paid for an order you shipped. |
| Found, `COMPLETED`, amount matches, its tracking or its claim couldn't be checked | any | `WAIT` | Not confirmed. Check your PayPal Activity for this payment before you ship. |
| Found, `COMPLETED`, amount matches, already approved an earlier case | any | `WAIT` | Don't ship. This payment was already used to approve an order. |
| Found, `COMPLETED`, amount matches | none (checked) | `VERIFIED` | You can ship. Ship only to the address on this payment's Transaction Details page in PayPal. |
| Found, `COMPLETED`, amount matches | any, or unknown: the check failed, or fewer than 5 of the buyer's words and none found | `VERIFIED_WITH_WARNINGS` | Real payment. Ship only to the address on its Transaction Details page in PayPal, and don't refund differences outside PayPal. |
| Not found | ≥ 2 confirmed | `SCAM_LIKELY` | Don't ship the item unless the payment shows as Completed in your PayPal Activity. |
| Not found otherwise, API unavailable, or no ID in the chat | any | `WAIT` | Not confirmed. Check your PayPal Activity for this payment before you ship. |

The table is applied top-down. When the chat asks the seller to send money, pay a fee or follow a payment link, the message also says: "Never send money, pay a fee or enter card details to receive a payment."

"Not found" alone never means scam. PayPal gives the buyer and the seller different transaction IDs for the same payment ([PayPal Help](https://www.paypal.com/us/cshelp/article/why-are-there-different-buyer-and-seller-transaction-ids-for-the-same-transaction-ts2265)), so the ID a genuine buyer quotes is often unknown to the capture API, and Transaction Search can lag. Activity search in PayPal does find the buyer's ID.

`detect_signals` looks for nine closed patterns: overpayment refund, fake payment proof, money held until tracking, third-party shipping, upfront fee, urgency, moving off-platform, unprotected payment (Friends and Family, gift cards, MB WAY requests) and payment links. The buyer's words are what's left without the transaction IDs, the `Seller:` lines and the `Buyer:` labels. A chat with none isn't sent: its signals are unknown, with no Groq call, so a lone ID never gets `VERIFIED` ("no warning signs") for a chat nobody read. One with fewer than 5 is sent, and a sign found in it counts, but none found is unknown too, so "ID: …, thanks" doesn't get `VERIFIED` either. A word count, not what the words say; an unlabelled paste counts the seller's words too.

Each hit carries the buyer's exact words. A hit is *confirmed* only when those words are really in the chat, compared on letters and digits only (casefold, unaccented). An unconfirmed hit still blocks `VERIFIED` but never counts toward `SCAM_LIKELY`.

### `payment_check`: the payment only

```mermaid
flowchart TD
  A[ingest_input] --> D[extract_claims] --> E[verify_payment] --> F[compute_verdict] --> J[safe_template] --> I[persist_case]
```

The payment IDs the chat held alone, once the seller answers `payment` after the price (Graph 3), are looked up by `payment_check` (`scam_check.payment_graph`, `scam_check.lookup()`), built from `scam_check`'s own nodes, so every payment gate comes with `verify_payment`: amount, status, age, tracking and the claim. It has no chat and no LLM node: no Groq call. The seller asked for the payment only, so its `compute_verdict` (`payment_only_verdict`) reads no chat as no warning sign (the user's decision, 2026-11-11): the same table, so a payment that passes every gate is `VERIFIED`, and one PayPal can't find gets `WAIT`, never `SCAM_LIKELY`. Its `verify_payment` is `scam_check`'s, so a `VERIFIED` is claimed as a check's is: the same payment then gets `WAIT_REUSED` from a lookup or a check. Its input is only `chat` (the IDs), `expected_amount` and `expected_currency`, so no claim or payment can be passed in. `safe_template` writes the reply, as there is no explanation to validate. The case is saved like a check's. A test pins the nodes the two graphs don't share, so a node added to `scam_check` has to be decided for `payment_check` too. It is in `langgraph.json`, so Studio shows it.

## Graph 2: `dispute_draft`, practice disputes

The seller pastes a practice dispute ID (`PP-SIM-…`, from the demo's Practice disputes menu) and gets a checklist grouped by what to do with each line, and a **draft response** to paste in the Resolution Center. Every fact comes from a fictional scenario in `app/demo_disputes.json`, never from PayPal. `app/dispute_draft.py`, served to Studio as `dispute_draft`.

```mermaid
flowchart LR
  I[PP-SIM-… ID, from the chat] --> L[load_scenario] --> C[classify_case] --> B[build_checklist] --> D[build_draft] --> W[write_reply]
```

**Why practice only (decided 2026-11-07).** Helping with a real dispute would need a real carrier's delivery data and the seller's own listing, which PayPal doesn't give. In the sandbox a sale can be disputed only once its Transaction Search row exists, from more than 30 minutes to about 3 hours after it, and PayPal has no carrier delivery events. So GoodToShip doesn't read disputes in PayPal: a real dispute ID in the chat gets a fixed text that sends the seller to the Resolution Center and offers a practice dispute.

- **No PayPal call, no database, no LLM, no `interrupt()`, no checkpointer.** GoodToShip only drafts: the seller sends the response in PayPal.
- **The ID** matches `dispute_draft.SIM_ID`, `PP-SIM-<INR|SNAD|UNAUTH|CARD>-<NN>`, which `chat.REAL_ID` (`PP-D-<digits>` in PayPal's spec, `PP-R-<3 letters>-<8 digits>` in the sandbox) never matches. An unknown one gets "There is no practice dispute …" and the list.
- **The scenarios** keep PayPal's shapes for `dispute`, `capture` and `trackers` (Disputes v1, Payments v2, Tracking), and `load_scenario` reads them as PayPal's answers: `keep()` takes only its fields, each validated and dropped if it fails (the times, the reason, status, stage and channel, the amount, the capture IDs, and what PayPal asks the seller for), never the buyer, messages, notes, offer, items, documents or links. Times are hour offsets from now, so the deadlines always look live; each "before or after" has more than 50 hours to spare, so no mark changes with the hour. `sim_carrier` (scans per tracking number: picked up, in transit, delivered, returned to sender, with "delivered to the order's address" and "signed for") and `listing` (a made-up catalog's name, price and description) are **GoodToShip's own fictional format**, not PayPal fields. A SNAD buyer's codes (`sub_reasons`, `product_received`) are read from PayPal's spec path, codes only, never the buyer's words. No buyer data, ever: the draft says "the buyer".
- **`classify_case`:** `EXTERNAL` or `ALERT` (a card or bank case) is `card`, whatever the reason. Then `MERCHANDISE_OR_SERVICE_NOT_RECEIVED` is `inr`, `..._NOT_AS_DESCRIBED` is `snad` and `UNAUTHORISED` is `unauth`. INR or UNAUTH with no capture ID, and any other reason, is `other`. `build_checklist` and `build_draft` cover INR, UNAUTH and SNAD; a card case and other reasons get no lines and no draft.

| Reason | INTERNAL, inquiry | INTERNAL, claim (`CHARGEBACK` stage) | EXTERNAL or ALERT (card or bank) |
|---|---|---|---|
| Item not received | Message to the buyer | Response to PayPal | No draft: the header and `pp_chargeback` |
| Not as described | Message to the buyer, quoting the listing | Response to PayPal, quoting the listing and the return | No draft |
| Unauthorized | Response to PayPal | Response to PayPal | No draft |
| Any other reason, an appeal, a resolved dispute | No draft | No draft | No draft |

- **The checklist:** ✓ the scenario shows it, ✗ it shows it isn't so, ? GoodToShip can't see it. In three groups: "Ready (✓): what you can send now", "Missing (✗): what's against you, or what you still need" and "Check yourself (?): GoodToShip can't see this". Under a line, why it counts is a sentence of its verified note, word for word, with its title, once. A refund line shows only when there is one.

  | Line | INR | UNAUTH | Read from |
  |---|---|---|---|
  | Eligible for Seller Protection against unauthorized payments | | yes | the capture's `seller_protection` |
  | Shipped no later than 2 days after the dispute opened | | yes | the first tracker's `shipment_date` against `create_time` + 48 h |
  | Tracking added to this payment, with its number | yes | yes | the trackers, without `CANCELLED` ones or returns |
  | Tracking number validated by the carrier | yes | yes | `tracking_number_validated` |
  | Shipped before the dispute opened | yes | | the first tracker's `shipment_date` against `create_time` |
  | Delivered | yes | yes | `sim_carrier`'s delivery scan |
  | Delivered to the Transaction Details address | yes | yes | `sim_carrier`, or "?": compare your carrier receipt |
  | Signature | yes | yes | "?" unless signed for: not required for a payment after 26 Jan 2026, so never under Missing; to check before (`pp_proof_of_shipment`) |

  SNAD shows "What the buyer reports" as context, then a fixed "Not covered by Seller Protection" (`pp_snad_not_covered`), the listing's description and the return, and none of the Seller Protection proofs.

  `shipment_date` is a day in the seller's timezone, which PayPal doesn't give, so the shipment was somewhere from 14 h before that day's midnight UTC (UTC+14) to 36 h after it (UTC-12). A date line is ✓ only when the whole of that span is in time, ✗ only when none of it is, and "?" otherwise.
- **The draft** is fixed sentences, each with the lines it needs: it prints only when they are all ✓ and every value it names is there. "[…]" parts are the seller's to write. It then lists "Missing before you send this:", the lines that count for the reason and aren't ✓, and any value a ✓ sentence names that the scenario lacks ("? Carrier: not in this scenario"), so no sentence goes unsaid. No sentence speaks of a refund, of "two days after PayPal's notice" (not verified: the checklist measures from the dispute's opening) or of the outcome.
- **The reply:** "Simulated dispute: fictional data, for practice. Nothing here is from PayPal.", the header (reason, PayPal case or card/bank case, stage, status, amount, and "Respond by … UTC (N days)" while the status is `OPEN` or `WAITING_FOR_SELLER_RESPONSE`) and what PayPal asks for, the checklist, the draft and what's missing, "You send it in PayPal: GoodToShip doesn't." with `pp_respond_to_dispute`, the pages of the quoted notes, and "Anything else I can help with?". `shown` is `pp_respond_to_dispute` (`pp_chargeback` for a card case), so the chat's next "what now?" answers how to respond.
- **Calls:** PayPal 0, Groq 0, database 0.

## Graph 3: `chat`

```mermaid
flowchart TD
  M[seller message] --> R{route_in: no LLM}
  R -- waiting for the chat, or a price or a pasted chat at the price step --> K[check]
  R -- "check", or looks like a pasted chat --> S[start_check]
  R -- "waiting for the answer to handoff's question" --> H[handoff]
  R -- "a dispute ID in a short message (no LLM call)" --> D
  R -- otherwise, also any other text at the price step --> C[classify: LLM]
  C -- question or another_payment --> Q[question]
  C -- "can_ship or next_steps, or a question the backstop reads as one" --> A[advise]
  C -- scam_check --> S
  C -- dispute --> D[dispute]
  C -- other, not_paypal or topics --> O[other]
  C -- "human, or the word operator (no LLM call)" --> H
  C -- anything but question, dispute, can_ship, next_steps or human, at the price step --> K
```

One Groq call per message, `classify`, picks closed choices only: an intent (`question`, `scam_check`, `dispute`, `not_paypal`, `another_payment`, `can_ship`, `next_steps`, `human`, `topics` or `other`) up to 2 keys of verified `policy_pack` notes, best first, and, for a follow-up, the numbers of up to 2 sentences of the notes just shown.

- **What it sees:** the current message, sanitised and inside `<question>` tags; the titles of the verified notes; and, when there is one, a `<previous>` block, also sanitised: the seller's last message that `classify` read, the notes in the last reply, numbered sentence by sentence, and the last check's outcome as codes (its label, its note's key and the labels of its warning signs) with the seller's price for it, so another order's price reads as another order.
- **What it never sees:** the buyer's chat, a check's quotes or evidence, the amount PayPal shows, or search results.
- **What it can't do:** it has no tools and writes no text the seller reads.

A follow-up answered from notes makes a second call, the **answer step** (`_grounded` inside `question`, not a node of its own, so nothing new is kept in the state). It runs only when every note `classify` left for this message was in the last reply and it picked sentences from it; a note the seller hasn't seen yet is shown in full. So never on a first question, right after a check, at the price step, or for a dispute.

- **What it sees:** `ANSWER_PROMPT`, the sentences of those notes (at most 2, after `classify`'s outcome-note rule), numbered and inside `<notes>`, and the current message, sanitised, inside `<question>` tags.
- **What it never sees:** the buyer's chat, a check's evidence or quotes, amounts, search results, or a message that matched `INSTRUCTION` (that one never gets past `classify`).
- **What it writes:** 1 to 3 sentences, each with the numbers of the note sentences it relies on. `output_guardrails.check_answer` checks them in code. If any sentence fails, or the call fails, the seller gets the reply below, which quotes the notes, then points to PayPal or, on the demo, offers a person.

The context lives in three state fields, never in `messages` (the demo clears those every turn): `last_question` (written by `classify`, cleared by a check, so a message from before the check is never the next one's context), `shown` (by every node that replies) and `last_case` (`{case_id, policy_id, signals}` plus, from the check's E1 only, `price` and `paid`, the seller's price and PayPal's amount as "40.00 USD", each dropped unless it matches a strict regex; by `check`). A reply about the last check, and the `another_payment` reply, name those amounts in code's words: "About your last check (case c882f1b4, your price 40.00 USD, PayPal shows 20.00 USD):". The graph's input is `messages` only, so a client can't set them.

Every reply is written in `app/chat.py`, by `scam_check`, or comes from PayPal (a page title and its link), except a follow-up's answer step, which is checked as above. The replies are in English.

- **question:**
  - Verified notes are shown word for word, at most 2 per reply. A PayPal note ends with its page; GoodToShip's own notes have no source line.
  - A follow-up gets the answer step's sentences, then "From my notes:" with the titles of the notes they cite and, for a PayPal note, its page. A note it doesn't cite isn't shown.
  - If the answer step fails, the reply shows each note's title and the 1 or 2 sentences `classify` picked, word for word, or the whole note when `classify` picked it again with no sentence. It ends with "Still unsure? … Contact PayPal", and on the demo, unless the note carries that line itself, with "Want a person from GoodToShip to look at this? Say operator." If none answers, the reply is the no-note one below (search, or "I don't have that information").
  - When a picked note is the last check's own outcome note, the reply starts "About your last check (case …):", so the seller sees which check it is about.
  - An outcome note ("VERIFIED: what to do", "WAIT, not paid: what to do", …, which say "You saw: …") is shown only when it is the last check's own: `classify` drops any other in code, whatever the LLM picked. `vg_verdicts` says "You can ship", so it is dropped too when there is no check. If that leaves no note, the reply is "How to check a payment".
  - PayPal's hold note ("Why a payment is on hold", which says to add tracking and that PayPal releases the hold after delivery) is shown only to a seller who talks about a hold: in the message or the one before (`HOLD`, a short EN and PT regex: "on hold", "held", "withheld", "unavailable", "PayPal keeps my money", "retido", "reteve", "cativo", …), or when the last reply showed it. A check in between clears the message before. Never beside the hold-until-tracking sign (`vg_signal_payment_held_until_tracking`, picked or in the last reply): that hold is the buyer's story. `classify` drops it otherwise, whatever the LLM picked, so a payment that isn't in the account never gets it (E07 cases 18 and 43). If that leaves no note, the reply is "How to check a payment", or a dispute's usual notes for `dispute`.
  - **another_payment:** after a check, a question about another order, buyer, item or payment gets "Your last check (case …) covered only that payment. To check the other payment, say check." With no check in the session, it gets "How to check a payment".
  - A seller who describes a warning sign gets its `vg_signal_*` note (the card's step for it).
  - A reply ends with at most one closing line: "I stopped the check…" at the price step, else "Want me to check the payment in PayPal? Say check." (offers the check without starting it) when a note about a payment scam is shown for the first time and there was no check, else "Still unsure? … Contact PayPal" when a GoodToShip note carried it or the answer step failed. Not offered for an address, a courier or WhatsApp: there is no payment to check. A dispute's note ends with two (below): the practice offer and PayPal's line.
  - If no note fits, Tavily searches PayPal's US pages, and the chat lists up to 3 titles and links that start with `https://www.paypal.com/us/` (filtered in code).
  - If the search finds nothing, fails, or there is no key, the reply is "I don't have that information" and PayPal's US business help link.
  - On the demo, both replies with no note (the PayPal pages, and "I don't have that information"), and a follow-up whose answer step failed (instead of "Still unsure? …", unless the note carries it), end with "Want a person from GoodToShip to look at this? Say operator." Not at the price step, where "I stopped the check…" stays the only closing line.
- **advise** (`can_ship`, "can I ship?", and `next_steps`, "what now?"): the LLM only decided that the seller asks; code answers from `last_case` and `shown`, with notes and its own words. No answer step, no search, no PayPal call.
  - `can_ship` with no check (or only an injection since): "I can't tell whether you can ship without checking that payment." and how to check. After a check: its own note, under "About your last check (…). It covers only that payment: for another order or buyer, say check." for VERIFIED and VERIFIED_WITH_WARNINGS, under "Not yet. Your last check (…) said:" for WAIT_NOT_CLEARED and WAIT_NOT_CONFIRMED, which can still change, and "No. Your last check (…) said:" for the other WAITs and SCAM_LIKELY. So only a VERIFIED check's note ever says "You can ship". An amount with its currency ("40 dollar", "25 USD", "60 usd", "$40") other than both the check's price and PayPal's amount gets the `another_payment` reply, from any node about to show the check's note (`_answer`); a bare number ("the other 5") doesn't count.
  - `next_steps`, the last reply first, then the last check: after a warning sign's note, that note and "Before you ship" (`vg_before_you_ship`, built from `POLICY["P.SCAM_LIKELY"]`, the SCAM_LIKELY Activity step and `SHIP`, no check needed); after "already shipped", that note again and never "Before you ship"; after the chargeback note, "The notes in my last reply are all I have" and more help, never the 10-day rule; after a note that means a dispute (`DRAFT_OFFER_NOTES`, not the shipping-proof notes), or right after a message that names a dispute or claim (`DISPUTED`, English only; `classify` reads it, as the hold, and passes it in `entries`; not with a chargeback word; 2026-11-08), PayPal's notes on how to respond and by when (`pp_respond_to_dispute`, `pp_dispute_deadline`), and PayPal's line (the last reply made the practice offer); else the last check's own note; with neither, "Before you ship" and how to check.
  - One closing line, as in `question` (a dispute's two apart): "Still unsure? … Contact PayPal", the check offer after a payment scam's sign with no check, or "I stopped the check…" at the price step. On the demo, a second "what now?" with nothing new (below) offers a person instead of "Still unsure? …".
  - **Backstop, after `classify` and never before** (so "what should I do if a buyer asks for F&F?" is classified by the LLM): when the LLM said `question`, a short EN/PT regex read with `fullmatch` sends the message to `advise` (`classify` applies it and writes the intent, so the router and `advise` only read it). `SHIP_ASK` ("can I ship?", "posso enviar?") does so unless the reply would come from the last check's own note; `NEXT_STEPS` ("what now?", "o que faço?") only when the reply would repeat the last one or have no note. E10 measures both on look-alikes. A `next_steps` reply whose notes were all in the last reply says "The notes in my last reply are all I have on this." instead of showing them again.
- **scam_check:**
  - The code asks for the price ("25.00 USD"), reads it with a regex and validates it like `POST /scam-check`. Then it asks for the buyer's chat, unless something is held (below).
  - That next message goes to `scam_check.run()` exactly as typed, with no classification and no search. "cancel" stops at either step, and "operator" ends it to ask for a person (below).
  - At the price step, text that is neither a price, a pasted chat nor "cancel" goes to `classify`. A question or a dispute is answered and ends the check, and the reply says so. Anything else gets "I need the amount…" again, so "twenty five euros" is still asked again. The chat step never goes to `classify`: what comes there is the buyer's.
  - The reply is the verdict, the message and the sentences, never the evidence, which holds the buyer's quotes.
  - Instead of the chat, the seller can send a screenshot of it (PNG or JPEG, as a base64 `data:` URL in an `image_url` part, checked by its first bytes) with or without text. It goes to `scam_check.run()` like a screenshot sent to `/scam-check`, and counts as a check and a screenshot on the demo. If it can't be read, the seller is asked again.
  - **What comes before the price is held for it** (RECHECK #26, #31), before `check` too, which it starts: up to 3 items in `held`, a state field, oldest first (decided with the user, 2026-11-03).
    - A screenshot is read when it comes (`scam_check.read`, the copy the check makes, one Groq call), and only its cleaned copy is held, with the IDs in it and the one amount it shows: never the image. The reply names the IDs it found ("I read your screenshot and found payment 3Y66…"), or says it has none. One that can't be read is said at once, and nothing is held. The seller's own text sent with it is never kept, and at the price step the reply says so.
    - A message of at most 400 characters with a payment ID and no `Buyer:`/`Seller:` line holds its IDs (up to 3), never its text. The reply names them, and says that the rest of the message wasn't kept when it had more words.
    - A 4th item drops the oldest, and the reply says which; so does an item that would make the held text longer than one chat (8 000 characters), so `detect_signals` never reads more than it does today. With more than one held, the reply lists them.
    - When a screenshot is held, the price then checks them all, without asking for the chat: `scam_check` on the copies and the IDs, newest first, so the check's 3 IDs are the newest. A screenshot sent with the price is read and held like the others first.
    - IDs held alone wait for one more answer (the user's design, 2026-11-11: the chat is read only when the seller has doubts). The price asks "Any doubts about the buyer? Paste their chat … No doubts? Say payment, and I'll check only the payment in PayPal." (`chat.ASK_DOUBT`) and takes no check. `payment` or `no` (any case, a final `.` or `!`) looks them up with `payment_check`, with no screenshot. Anything else is the buyer's chat, as at the chat step, never classified: `scam_check` reads it, with its screenshot, and the IDs after it, so it can give `VERIFIED`. A screenshot that can't be read keeps the IDs. The page's scenario chips wait at this step too (`held`).
    - The amount a screenshot shows is the buyer's: it is never the price and is never shown before it, so the seller writes their own (a buyer who paid half shows half). After the check, when PayPal shows another amount for that payment, the card says "Your screenshot shows 250.00 USD, but PayPal shows 25.00 USD for this payment. Only PayPal's amount counts." Read by a regex: a number next to the code of a currency PayPal takes, exactly one distinct amount, else none.
    - `held` is cleared wherever the price is: the check, cancel, a question at the price step, `handoff`, `operator` after the price too.
  - Where the chat can still change the outcome (`VERIFIED_WITH_WARNINGS`, and `WAIT` "not confirmed", which two warning signs make `SCAM_LIKELY` for a payment PayPal can't find), a lookup's card says "I checked only the payment in PayPal, not the buyer's chat", and how to check the chat too. On the same outcomes, a check whose signals are unknown says "I couldn't check the buyer's chat for warning signs: it had too little text, or reading it failed." So the buyer's words in a short message with an ID are never read for warning signs.
- **dispute:**
  - A short message with a dispute ID, in any case, comes here from `route_in` with no `classify` call, a payment ID beside it too. Not a pasted chat (`Buyer:` lines, or over 400 characters), which starts a check, and not while a check waits for the buyer's chat or `handoff` for its answer. At the price step it goes to `classify`, and a `dispute` intent reads the ID too.
  - A practice ID (`PP-SIM-…`) is upper-cased and run by `dispute_draft` (Graph 2): no `classify` call, no PayPal call, any session's. Its reply ends with "Anything else I can help with?", or with the check's stop at the price step.
  - A real dispute ID (`chat.REAL_ID`, `PP-D-…` or `PP-R-…`) gets a fixed text, with no call at all: "I can't read your disputes in PayPal: you respond to them in your Resolution Center. To practise a response, paste a practice dispute ID, such as PP-SIM-INR-01." It is caught so that it never reaches `classify`, where the LLM could write as if it had read the dispute.
  - Without an ID: "The buyer opens a dispute in PayPal, and you respond to it in your Resolution Center", the offer of a practice dispute ("To practise your response, paste a practice dispute ID, such as PP-SIM-INR-01…"), and the 2 notes `classify` ranked first, or the first 2 dispute notes, then PayPal's "Still unsure? … Contact PayPal" line. Right after another dispute reply, the notes only.
  - A `question` or `next_steps` reply that shows a note meaning the seller has a dispute (`pp_dispute_vs_claim`, `pp_dispute_deadline`, `pp_respond_to_dispute`, `pp_snad_not_covered`, `pp_unauthorized_claim`) ends with that offer and then PayPal's "Still unsure? … Contact PayPal" line (`DRAFT_OR_PAYPAL`), on the demo too instead of the offer of a person: the seller chooses between practising with GoodToShip and asking PayPal (the user's decision, 2026-11-07). Once per stretch of dispute replies (2026-11-08): not when the last reply showed a dispute's note, so made the offer, or was a practice dispute (`_offered`). Not the proof-of-shipment notes, which answer shipping questions too. The chat can't tell whether a practice dispute was already read, so "what now?" after one offers it again (RECHECK #46).
  - A dispute ID clears `last_question`, as a new check does, so the message before it isn't the next `classify`'s "previous message".
  - A chargeback never gets PayPal's 10-day rule, in one gate in `classify` for every node (RECHECK #34): when the LLM picks `pp_chargeback`, or this message or the one before has a chargeback word (`CHARGEBACK`, English only: chargeback, charged back, card issuer; not "estorno", which is any refund in Portuguese), `pp_dispute_deadline` becomes `pp_chargeback`; also when the last reply showed the chargeback note and this message names no dispute or claim (`DISPUTED`; 2026-11-08, E08 31), so "how long do I have?" after "what now?" keeps it, while a PayPal dispute asked about right after a chargeback gets its own notes. The `dispute` node then shows the chargeback note first, the LLM's next pick, and more help: a card case has no draft to practise. `advise`'s "what now?" after the chargeback note says the notes are all it has.
- **other:** the first time (`greeted`, a state field), what GoodToShip does and how to start a check (`chat.intro`). With the demo's context it ends with "When I can't help, a person from GoodToShip can: say operator.", and the demo's page shows it before the seller writes anything: a new session marks its thread greeted. After that, one line on what GoodToShip can help with, or a short goodbye ("thanks", "bye").
- **topics** (the seller asks what GoodToShip knows or what they can ask, or says they have questions without asking one yet): the four areas of notes, GoodToShip, warning signs, PayPal and disputes, then "Say its name, or just ask your question." An area's name gets its notes' titles instead, one per line, then "Ask me about any of them." In a longer message the most specific area named wins: disputes, warning signs, PayPal, then GoodToShip, which sellers also use to address the bot. A note falls in an area by the sets in `chat.py` (`SCAM_NOTES`, `DISPUTE_NOTES`, `vg_` or not), so a new one is listed with no change; the verdicts' notes, shown only after a check, are in none, and the GoodToShip list says "After a check, ask me what its result means." The word `notes` and the four names (English only), sent alone, get there with no `classify` call. Before the greeting, the intro comes first; at the price step, "I need the amount…", as for `other`. Every word is code's or a note's title.
- **not_paypal:** a check or a dispute that isn't on PayPal (a bank transfer, another app, another marketplace) goes to the same node, which says GoodToShip only works with PayPal.
- **human** (the seller asks for a person; the word `operator`, sent alone, gets there at any step with no `classify` call, at the chat step too, where it would otherwise be the buyer's chat): `handoff`, with no LLM and no database.
  - Everywhere since 2026-11-09 (the demo is the live product), it lists what a person from GoodToShip would see and asks "Share it? Say yes, or anything else to keep it private." (`awaiting="consent"`). At the price or the chat step it ends the check first.
  - The next message goes back to `handoff`, never to `classify`, a check or Tavily. A short yes (EN or PT, read with `fullmatch`) sets `consented`. Anything else, a pasted chat or a screenshot included, shares nothing and is dropped.
  - Without that context (Studio, the evals), the reply is "Talking to a person from GoodToShip isn't available here for now."
  - It is the run's `context`, not `configurable`, because LangGraph copies `configurable` values into every checkpoint's metadata and, with tracing on, into LangSmith's run metadata.

Things that are easy to get wrong:

- **Pasted chats.** A message with a 17-character ID, `Buyer:`/`Seller:` lines or more than 400 characters starts a check instead of being classified, so it reaches neither the LLM nor Tavily. A short one with an ID and no label keeps only its IDs, for `payment_check`. A short buyer line with neither can't be told apart from a question.
- **Isolated check.** `scam_check.run()` runs in an empty `contextvars.Context()`. Called directly inside a node, LangGraph treats it as a subgraph: it would save `scam_check`'s state, quotes included, in the chat thread's checkpoints and stream it to Studio.
- **Isolated practice dispute.** `dispute_draft.run()` runs in an empty `contextvars.Context()` too: as a subgraph it would keep its state in the chat's checkpoints.
- **The demo's live steps.** Only a run whose context holds `chat.LIVE` (`{"live": chat.LIVE}`, the demo's) gets each PayPal call and node update of the check, through LangGraph's custom stream (`scam_check.run`'s `report`). `LIVE` is an object, not a flag: Studio and the API take a run's context as JSON (`langgraph_api` copies it into `configurable` too), so they can never set it, and Studio sees no steps. The stream writer reads the node's config when it is called, and raises in the empty context, so it is called through a copy of the node's context. Checked on 2026-10-31: the steps arrive as they happen, nothing of `scam_check` reaches the chat's checkpoints, and LangSmith records neither the context nor the custom events.
- **Input.** The graph's input is `messages` only, so a client can't set the price, the intent or the context. The router on `START` still reads the raw input: a crafted `awaiting` reaches `check`, which then asks for the price, or `classify`, which sees it filtered out.
- **A stale last case.** "Can I ship?" after a VERIFIED check may be about another order. `classify` is told that a check covers only its own payment, `start_check` and `cancel` forget the last case, and the reply names the case. The risk is in RISKS.md.
- **Instructions to GoodToShip.** A message that matches `INSTRUCTION` ("ignore your instructions", "from now on", "always pick", "tell me it's verified", "SYSTEM:", …) gets the `other` reply with no `classify` call, and isn't kept as the next message's context. The regex catches common phrasings only: the outcome-note rule above is what keeps a paraphrase from showing a verdict.
- **One request, two on a follow-up.** `classify` makes a single request (`llm.ask(..., attempts=1)`). On any failure the seller gets the `other` reply and nothing is searched (at the price step, the price is asked again). A follow-up answered from notes makes one more, also with no retry, of about 1,000 tokens. The message with the buyer's chat makes no `classify` call; `scam_check.run()` makes its usual two. The word `operator`, the answer to `handoff`'s question and a message with a dispute ID make none.
- **Consent.** While `awaiting="consent"`, every message goes to `handoff`, and only a short yes consents: a buyer's chat or a screenshot sent then is dropped unread. At the chat step, "operator" is still the buyer's chat, like "notes": cancel first.
- **Prompt detail.** Without "Reply with the JSON object only." at the end of the prompt, `gpt-oss-120b` sometimes wrote its reasoning as the answer, and Groq returned a 400 (`output_parse_failed`). That happened in 5 of 8 tries on one message.
- **No API yet.** The chat has no endpoint. When it gets one, a message that runs a check must take the same `SCAM_LIMIT` slot as `POST /scam-check`.

### Tavily

`TAVILY_API_KEY` is optional. The call is `POST https://api.tavily.com/search` with httpx:

- a 5 s timeout and no retry; any error gives the help link;
- `search_depth: basic`, which costs 1 credit (the free plan has 1 000 a month);
- `include_domains: ["www.paypal.com"]`;
- no LLM answer and no page text.

The query is only the seller's current message, after this cleanup:

- sanitised;
- without self-introductions ("I'm Amanda"), emails, links, phone numbers or 17-character IDs;
- cut to 400 characters.

Other names stay in the query (accepted with "Allow Use of Query Data" off, 2026-11-07).

What Tavily does with queries, according to its own pages:

- The [Privacy Policy](https://tavily.com/privacy) (24 Nov 2025) says Tavily:
  - keeps query data "for the period necessary to provide and improve the Services";
  - may use parts of it to improve future responses;
  - may share it with third-party search index providers such as Google;
  - asks users not to put personal information in queries.
- The [Platform Terms](https://www.tavily.com/terms) (4 May 2026, §9.2) give Tavily a perpetual, irrevocable licence over queries.
- No DPA is published, and zero data retention is not in the standard terms.
- **"Allow Use of Query Data" is off** in the Tavily account settings (turned off by the user, 2026-11-07): it stops queries from being stored or used for improvements.
- Tavily is a US company (AlphaAI Technologies Inc., New York). Nebius announced an agreement to acquire it on 10 Feb 2026.

The app starts without the key, unlike `GROQ_API_KEY`:

- Without Groq, every check silently loses signal detection and the explanation, and every chat message fails.
- Without Tavily, only a question with no note loses the search, and its answer is the same honest "I don't have that information" that a Tavily outage gives.
- Refusing to start would take down the webhook and `/scam-check` for an optional feature. `app/chat.py` logs a warning when the key is missing.

## How hallucinations are prevented

The LLM chooses the words and the code supplies the facts.

1. **Closed context.** Each call sees only this case's evidence and the one `policy_pack` entry chosen by `load_policy`. No web text reaches the LLM, and no other cases.
2. **Sentences with sources.** Output is a list of `Sentence` (closed `intent`, at least one source: `E…` for evidence, `P.…` for policy).
3. **Facts by placeholder.** The model writes `{{E2.delivered_at}}`, never a date. The code fills in the real value, and policy text is inserted verbatim by `policy_id`.
4. **Deterministic validation.** Every source must exist, every placeholder must belong to a cited source, and no numbers, dates, emails, URLs or phone numbers may appear outside placeholders. Banned phrases ("guaranteed", "you will win", legal or tax advice, other platforms) are rejected. A warning sign's label is a verb phrase, so it may only follow "the buyer" (or ": ", "; ", "and " in a list), and a signal's code name (`PAYMENT_LINK`) or its `type` and `confirmed` fields never appear in a sentence. The sentence about the payment always comes from code, a fixed template that shows the status and both amounts, because a grounded sentence could still contradict the verdict ("{{E1.amount}}, in line with {{E1.expected_amount}}"). The model sees only the warning signs and writes 1 to 3 sentences about them. With no warning signs, it isn't called.
5. **Abstaining is valid.** Missing proof goes in `missing` with an `ADMIT_MISSING` sentence. Inventing it is a failure.
6. **Safe fallback.** Two failures in a row fall back to a fixed template per verdict or reason, with no LLM, and the case is flagged for review.
7. **The chat's answer step.** In a chat follow-up the facts are the verified notes, so `check_answer` compares each sentence with the note sentences it cites:
   - it cites at least one listed sentence;
   - every number, number word, link (with or without `https://`), currency and code name in it appears in what it cites, as a whole token ("3" isn't in "3-5 days");
   - it names no verdict and says nothing about shipping (the verb: "proof of shipment" is fine), unless a negation comes just before the ship word ("Don't ship…", not "Don't wait, ship") or it cites the last check's own note;
   - it is plain English with no markup;
   - "you can / may / could + verb" and "safe / ok to + verb" appear in what it cites;
   - it makes no promise or legal or tax advice;
   - at least half of its content words come from what it cites or the notes' titles, and 2/3 counting the seller's message too;
   - it keeps a negation from its source: a word only in a cited sentence's negated clause ("not eligible for Seller Protection") must be negated where it's written. Negations include no, without, none, neither and nor.

   The last two are lexical heuristics: a fluent paraphrase can pass them, and E07's judge watches for that. Any failure gives the quoted notes instead.

Fields in the LangGraph state are owned by the node that writes them (an `@owns(...)` decorator), so an LLM node cannot write the verdict.

`dispute_draft` has no LLM at all: its reply is a template filled with validated scenario values and verified notes.

`app/policy_pack.py` is a Python dict of notes, looked up by key and never by similarity.

- Each entry has `title`, `text`, `source_url`, `retrieved_at` and `verified`.
- An entry is shown, or offered to the LLM, only once a person has read its source page and set `verified: True` (test U16).
- There are two kinds of entry:
  - **GoodToShip notes:** the verdicts, why WAIT, what to do after each outcome, what to do about each warning sign (`vg_signal_*`) and after shipping without being paid, and how to ask for a check. They are built from `scam_check`'s own texts (`STEPS`, `SIGNAL_STEPS`, `SUPPORT`) and are verified.
  - **PayPal notes:** 20 of them, each from one `https://www.paypal.com/us/` page, with the page's sentences word for word. The only edit is "we" → "PayPal". A list is "- " lines under a heading that ends in ":" (`pp_respond_to_dispute`; joined with "; " before 2026-11-07), and the chat counts the heading and its items as one sentence, so a follow-up never quotes the heading alone. All are verified: 17 on 2026-10-22, 3 on 2026-10-30. E09 checks every sentence against its page today. Two quote only part of a list of requirements (`pp_seller_protection`, `pp_proof_of_delivery`), so they end with a sentence of GoodToShip's own: "This is not PayPal's full list", with the page's link.
- The region is set once, in `REGION`.
- No PayPal US page says refunds must go only through PayPal, so the refund note says only how to refund in PayPal. Response deadlines are given only where a US Help Center page states them (10 days for a dispute). Seller Protection depends on region, and for the demo it assumes a US sandbox business account. It does not cover "Significantly Not as Described", so the agent never promises protection on a SNAD dispute.

## Live sandbox demo (`/demo`)

A public page where anyone can try GoodToShip against PayPal's sandbox, and see what it does. It has two menus, kept in the URL's hash (`/demo#practice` opens the second; `#disputes`, the real-disputes menu removed on 2026-11-07, opens it too). They share the chat and the live panel, so it is one conversation. **Check a buyer** has the three steps below and Your orders. **Practice disputes** is described after Your orders.

1. **Make a sale**: an item and a price in USD (1–1 000).
2. **Pay as the test buyer**: PayPal's own checkout (JS SDK) with a shared sandbox buyer account, whose email and password the page shows. The server creates the order and captures it after approval.
3. **Ask GoodToShip**: the `chat` graph, which speaks first. To check a payment, type check, or pick a buyer's chat to start from: an honest buyer, an overpayment, a buyer who reuses a shipped payment, a fake PayPal email or a prompt injection. The chip sends "check" and the sale's price for you, then leaves the buyer's chat in the message box, to edit or send; at the chat step it only fills the box. The honest buyer and the overpayment use the real transaction ID of the latest paid order that hasn't shipped. "Reuses a shipped payment" asks for a second item at the same price with the latest shipped order's transaction ID, and gets `WAIT_SHIPPED`. Each waits for such an order, and after a reload uses the orders already there.

**Ask GoodToShip** answers questions from the verified notes, lists what it has notes on, checks a payment over three messages, runs a practice dispute or explains disputes, and gives a "PayPal only" reply for other services. Each demo session has its own conversation, kept in memory (`InMemorySaver`, thread id = session id) and forgotten when the session ends or the service restarts. The session is in Postgres, so it outlives a restart: the next message finds an empty thread, which is greeted again, and its reply starts with "I restarted and lost our conversation up to this message." (`chat.RESTARTED`), then, unless that message started a check or asked about sharing, "If you had started a check, say check to start it again.", so a check cut short doesn't end in the intro without a word. After each message, the thread keeps only its latest state, without that turn's messages, so the buyer's chat and any screenshot go when their turn ends. The exception is what is held for the price (screenshot copies and payment IDs, never an image), which stays until the check runs or ends; the reply's `held` tells the page, so a scenario chip waits. A screenshot read when it comes shows on the live panel as a `read` line, with its copy and the IDs found. Each turn is stored once, as it ends (`durability="exit"`). A seller can ask for a person (`human` in Graph 3). Only with the token, the session also keeps a transcript in memory, outside the graph: every message the seller sent (sanitised, with a screenshot only when a check read it, otherwise `[screenshot, not read]`), and every reply, not the greeting, which is fixed text. When the seller says yes to `handoff`'s question, the transcript and the last check's codes are stored in `handoffs`, at most one open per session. From then on the conversation is the person's: each message the seller sends is appended to it (`db.relay`, a `relayed` line, with a screenshot as `[screenshot, not read]`) and never reaches the graph, so GoodToShip doesn't answer, nothing is classified and no check runs. The page also has a **Play the person from GoodToShip (simulation)** section, with no token (2026-11-08): the tester plays both sides, and sees only their own session's open handoff, the transcript as text only and the screenshots as images, with the last check's codes, redrawn when it changes (`updated_at`), and answers it as many times as they like. The seller's page shows each answer once in the chat as "Operator: …", from its 4-second poll of `/demo/api/state`, and a **Stop sharing** button while a person has the conversation. The answers never enter the graph (`messages`, `shown`, `classify`'s `<previous>`). The person's **Close** or the seller's **Stop sharing** closes it: the stored transcript and last check are emptied (the answers stay for the seller's page until the session ends), the chat thread and the in-memory transcript are deleted, and the thread is greeted again, as a new session's. The seller's page shows "The conversation with GoodToShip's team is closed. I've forgotten this chat…" and the intro (`notice`, from `/demo/api/state`); the next message is GoodToShip's again. The live panel shows how each message was routed: the intent and notes the model picked, or that the code routed it with no model call. The Screenshot button sends the buyer's chat as an image instead of text, at the chat step or before the price. A check's card ends its steps with "If you need more help from GoodToShip, say operator to talk to a person."

**Your orders** lists the session's sales, newest first: each as a card with item, price, payment status (a SHIPPED pill once it has tracking), transaction ID (with Copy ID) and tracking number.
- Each paid order has "Buyer's receipt" and "Seller's receipt" buttons: PNGs of the real sandbox capture, one for each side, drawn in the browser and labelled as the demo's, to try screenshots with in the chat.
- Its "Mark as shipped" adds test tracking to the order in PayPal, as a seller does after shipping, so a check of its payment gives `WAIT_SHIPPED`. The call is Orders v2's `POST /v2/checkout/orders/{id}/track`, with carrier `OTHER`, the number `VG-DEMO-<order id>`, and the buyer not notified. The order then shows SHIPPED and a Tracking row with that number.
- The page polls `/demo/api/state`, which reads the database only, never PayPal.

**Practice disputes** is where a tester practises a dispute response on fictional scenarios (`dispute_draft`, above). A banner says so: "Fictional scenarios to practise a dispute response. No PayPal data: nothing here happens in PayPal." Each scenario is a card (`/demo/api/state`'s `practice`: title, story, reason and stage), and "Get my draft" sends its ID to the chat. It waits while the chat waits for a check's price or chat, or for the answer about sharing, since the ID would be read as that answer, and while a person has the conversation. Next to it, **Your checklist** draws the last scenario's checklist in green, red and grey boxes, from the chat reply's `checklist`: the page only draws the server's words, as text. The live panel says the dispute was read from a file, with no PayPal call, and on this menu its rail shows `dispute_draft`'s 5 nodes, each lit with what it wrote (the scenario read, the dispute type, the checklist's counts, the draft's sentences and what's missing, the reply). They are the steps of the run that builds the reply's `checklist`, the same file as the chat's run, streamed before the reply. A real dispute ID gets the chat's fixed text (Graph 3).

Next to the steps, a live panel shows each `scam_check` node of a chat's check as it runs and what it wrote (on Practice disputes, `dispute_draft`'s nodes, above), every PayPal call with its HTTP status and response body, and the verified webhooks about the tester's sales. It shows the tester what they sent (the cleaned chat, the screenshot's text, the quotes), which `/scam-check` never returns and the chat's reply never shows. The check's outcome (codes only) is the chat's last case, so "what do I do now?" is about it. The messages of a session run one at a time: a second one gets a 409. Each message runs in a thread of its own, to its end even if the page goes away, so a check whose payment PayPal's claim took is always saved in the chat.

- **Anonymous sessions.** The page keeps the session id in `sessionStorage`, so closing the tab forgets it and a reload keeps it. "End demo" deletes the session at once, and a session idle for 30 minutes, or started more than 2 hours ago (open tab or not), is deleted at the next new session or at boot. Deleting removes its sales, cases, claimed payments, handoffs and the webhook events about its sales, and forgets its conversation and transcript. The payments stay in PayPal's sandbox, which can't delete them.
- **Limits per IP, per hour:** 10 checks, 3 screenshots, 10 sales, 10 shipments, 20 sessions, 30 chat messages. The same limits apply to `/scam-check`. A chat message that runs a check counts as one, and one whose screenshot is read counts as a screenshot, when it comes; either needs a free slot (an error line when none is free, and nothing is held). A message relayed to a person counts as neither. Counters are in memory, so they reset when Render sleeps.
- **Off by default.** Only `DEMO_MODE=true` turns it on, and then the app refuses to start unless `PAYPAL_BASE_URL` is the sandbox and `DEMO_BUYER_EMAIL` and `DEMO_BUYER_PASSWORD` are set.
- Order creation, capture and test tracking are the app's only PayPal writes. They live in `app/demo.py`, never in a graph. A capture is sent with `PayPal-Request-Id: capture-<order id>`, so a retry can't capture twice, and only for an order of the caller's session.
- Test tracking goes only on a paid order of the caller's session, with the capture ID from the database, never the browser's. Orders v2's track call lists no `PayPal-Request-Id` in its schema (read 2026-11-02). So the order is marked shipped in the database before the call, and unmarked when PayPal refuses it or doesn't answer: a second click gets a 409, never a second tracker. The fixed `PayPal-Request-Id: track-<order id>` is sent anyway.
- Groq's free tier fits about 2 checks a minute in total. With several testers at once, checks fail closed (signals unknown, the fixed template) and the panel says so.

## Interface

- **Scam checker**: paste a chat or drop a screenshot, get the verdict with cited sentences. Built for now as the demo page (`/demo`).
- FastAPI serves the pages. Jinja2, HTMX and vanilla JS, no React.

## Endpoints

| Endpoint | Status | Response |
|---|---|---|
| `GET /healthz` | Built | `{"ok": true}` |
| `POST /webhooks/paypal` | Built | `{"status": "stored"}` or `{"status": "duplicate"}`. 400 on invalid JSON or a bad signature. A 5xx if PayPal is unreachable, so PayPal retries. |
| `POST /scam-check` | Built | Body `{text, image, expected_amount, expected_currency}`. Send `text`, `image` or both. `image` is a base64 PNG or JPEG with no `data:` prefix, up to 3 MB. The chat may be up to 8 000 chars after sanitising, counting the screenshot's text. Returns `{case_id, verdict, message, sentences, evidence, template, signals_checked, persisted}`, never the chat or the screenshot's text. 422 on bad input or a screenshot that can't be read, 429 over 10 checks (3 with a screenshot) an hour per IP, 503 when two checks are already running. Not authenticated (RECHECK #3, out of scope for the demo). Every route answers 413 to a body over 4.2 MB, before reading it. |
| `GET /demo` | Built, `DEMO_MODE` only | The demo page. Every `/demo*` route is 404 without `DEMO_MODE=true`. |
| `POST /demo/api/session` | Built, `DEMO_MODE` only | `{session_id, client_id, currency, buyer_email, buyer_password, intro}`, `intro` being the greeting the page shows first (the thread is marked greeted). The other demo routes need the id in `X-Demo-Session`, and answer 401 once the session ended, went idle or is 2 hours old. |
| `POST /demo/api/orders`, `POST /demo/api/orders/{id}/capture` | Built, `DEMO_MODE` only | Create a USD order for `{item, amount}`, then capture it: `{capture_id, status, amount, currency}`. 404 for another session's order, 502 when PayPal refuses. |
| `POST /demo/api/orders/{id}/ship` | Built, `DEMO_MODE` only | Mark as shipped: test tracking on a paid order of the session, `{tracking}`. 404 for another session's order, 409 when it isn't paid or already has tracking, 502 when PayPal refuses (the order stays unshipped). |
| `POST /demo/api/chat` | Built, `DEMO_MODE` only | Body `{message, image}`, a message up to 8 000 chars, a screenshot (as `/scam-check`'s `image`) or both. One message to the `chat` graph on the session's thread, streamed as NDJSON: for a message that runs a check, `paypal` (method, path, status, body) and `node` (name, update) lines as they happen; for a practice dispute ID, `dispute_draft`'s 5 `node` lines (with `"graph": "dispute_draft"`), from the run that builds `checklist`; then `reply` `{reply, route, intent, notes, awaiting, held, dispute_id, checklist}`, or `error`. `dispute_id` is the dispute ID the `dispute` node got (a practice one, or a real one it answered with the fixed text), else null. `checklist` is a practice dispute's checklist in its three groups (`group`, `heading`, `lines` of `mark`, `text`, `why`), else null. `route` is the node that answered; `intent` and `notes` are set only when the model classified the message. A message that runs a check counts as a check, and as a screenshot if it has one. A message while the session's previous one runs, one over an hourly limit, or one that would run a check while two are running gets an `error` line, since the stream has started. |
| `POST /demo/api/handoff/close` | Built, `DEMO_MODE` only | The seller's Stop sharing: closes the session's open handoff and starts the chat over; 409 when none is open or a chat message is running. |
| `GET /demo/api/state`, `POST /demo/api/end` | Built, `DEMO_MODE` only | The session's sales (with their `tracking`), the webhook events about them, its handoffs (`id`, `status` `open` or `closed`, `replies`, and for a closed one the `notice` the page shows, never the transcript), `operators`, always true on the demo (the page shows its operator section), `practice`, the Practice disputes menu's scenarios (`id`, `title`, `story`, `reason`, `stage`, from the file, no PayPal call), and `awaiting`, what the chat waits for, so a reloaded page never sends a dispute ID as the buyer's chat; ending deletes the session and all it left (409 while a chat message is running). |
| `GET /demo/api/operator/handoffs`, `POST /demo/api/operator/handoffs/{id}/reply`, `POST /demo/api/operator/handoffs/{id}/close` | Built, `DEMO_MODE` only | The simulated operator (2026-11-08): the tester plays the person from GoodToShip on their own page, with the demo session in `X-Demo-Session` like every demo route, and no token. The first lists the session's own open handoff (`id`, `transcript`, `last_case`, `shared_at`, `updated_at`, never the session id); the second answers it with `{reply}`, 1–2 000 characters, as often as needed; the third closes it and starts the chat over. Another session's handoff is a 409, as one that was closed or whose session ended. |

### How the webhook endpoint works today

1. The five `PAYPAL-*` signature headers must be present and `PAYPAL-CERT-URL` must be a `paypal.com` API host, otherwise 400 without calling PayPal.
2. The event is sent to PayPal's `verify-webhook-signature` endpoint. Anything but `SUCCESS` is a 400.
3. The event is inserted into `webhook_events` with `ON CONFLICT DO NOTHING`, so a redelivery is acknowledged but processed once.

## Layout

```
app/main.py         FastAPI app, /healthz, /webhooks/paypal, /scam-check, the demo's streamed check and chat
app/demo.py         the live demo's routes: anonymous session, sandbox orders, captures, test tracking, state, end; the chat's thread per session
app/ratelimit.py    per-IP hourly limits for the open endpoints
app/static/demo.html  the demo page: plain HTML, CSS and JS, no build
app/paypal.py       signature-header checks, OAuth token cache, verify-webhook-signature, capture, tracking and Transaction Search reads
app/db.py           schema bootstrap, claim_event() (idempotent insert), save_case(), the demo's tables and cleanup
app/scam_check.py   the scam_check graph: @owns, sanitize, the 11 nodes, verdict table, policy texts
app/dispute_draft.py  the dispute_draft graph: keep() and PayPal's shapes, the grouped checklist, the draft and its reply
app/demo_disputes.json  the practice disputes: fictional scenarios in PayPal's shapes, plus sim_carrier and listing
app/chat.py         the chat graph: route_in, classify, the check flow, the dispute IDs, the Tavily search
app/policy_pack.py  the notes the chat shows: GoodToShip's and PayPal's, with sources and `verified`
app/output_guardrails.py  Sentence model, validate() and render() for LLM-written sentences
app/llm.py          the Groq calls, no tools: ask() with a strict JSON schema, read_image() for screenshots
tests/              pytest + respx (PayPal and Groq are mocked, no database needed); tests/test_db.py runs the real SQL
langgraph.json      serves the scam_check, payment_check, chat and dispute_draft graphs to `langgraph dev` and LangGraph Studio
render.yaml         Render blueprint: the web service (Postgres is on Neon, set by DATABASE_URL)
```

## Stack

| Layer | Tool | Installed today |
|---|---|---|
| Core | Python 3.12, FastAPI, Uvicorn, httpx, psycopg | yes |
| Core | LangGraph, Pydantic v2, Groq (`openai/gpt-oss-120b`, strict JSON-schema output; `qwen/qwen3.8-27b` reads screenshots, a preview model) | yes |
| Search | Tavily Search API through httpx, no SDK (`chat` only) | yes, optional |
| PayPal | Sandbox (US test accounts): Payments v2, Transaction Search, Tracking, `verify-webhook-signature` | yes: reads, plus the demo's sandbox writes |
| Infra | Render (web service), Neon Postgres, cloudflared or ngrok for local webhooks | blueprint written |
| Interface | Jinja2, HTMX, vanilla JS | the demo page only (vanilla JS) |
| Tests | pytest, respx, Pillow (draws the eval screenshots); pytest-asyncio, RAGAS and promptfoo later | pytest, respx, Pillow |
| DevSecOps | GitHub Actions, Bandit or Semgrep, pip-audit | planned |
| Observability | LangSmith (`@traceable` on the Groq calls, EU region, off unless `LANGSMITH_TRACING=true`, made-up chats only) | yes, optional |

## Configuration

Copy `.env.example` to `.env` (it is gitignored).

| Variable | Notes |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Sandbox REST app credentials from developer.paypal.com |
| `PAYPAL_WEBHOOK_ID` | ID of the webhook registered in PayPal. It needs a public URL. |
| `PAYPAL_BASE_URL` | Defaults to `https://api-m.sandbox.paypal.com`. Set to `https://api-m.paypal.com` for live, in `.env` and `render.yaml`. |
| `DATABASE_URL` | Postgres connection string. The tables are created on startup. |
| `GROQ_API_KEY` | From console.groq.com. Required: the app does not start without it. The free tier (8K tokens/min) fits about 2 checks a minute. |
| `GROQ_API_KEY_LLM_EVAL`, `GROQ_API_KEY_LLM_EVAL2`, `GROQ_API_KEY_LLM_JUDGE` | Tests only: the evals (`pytest -m eval`) call Groq with these, never with `GROQ_API_KEY`, so they don't spend the app's daily quota. The second is E07's judge. |
| `LLM_MODEL` | Defaults to `openai/gpt-oss-120b`. Must be a Groq model with strict JSON-schema output. |
| `TAVILY_API_KEY` | From app.tavily.com. Optional: without it the app starts, and a chat question with no note gets PayPal's help link. Turn off "Allow Use of Query Data" in the Tavily account (see Graph 3). |
| `DEMO_MODE` | `true` turns on the live demo (`/demo`). Sandbox only: the app refuses to start with it against live PayPal. Off when unset. |
| `DEMO_BUYER_EMAIL`, `DEMO_BUYER_PASSWORD` | A US sandbox personal account that testers log in with at PayPal's checkout. The page shows both. Required with `DEMO_MODE`. Keep a second account ready: a tester can change this one's password. |
| `VISION_MODEL` | Defaults to `qwen/qwen3.8-27b`, which reads screenshots. Must be a Groq model with image input that accepts `reasoning_effort="none"`. It is a preview model and can be withdrawn at short notice. A screenshot costs about 2 000 tokens of its own per-minute limit, separate from `LLM_MODEL`'s. |

Set `LANGSMITH_TRACING=true` only while testing with made-up chats: it sends every chat and LLM reply to LangSmith. Never set it on Render or with real cases. For `/scam-check` to find payments by ID, tick "Transaction Search" on the sandbox REST app. A new permission can take up to 9 hours to reach new tokens.

## Run locally

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
docker run -d -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres   # matches .env.example
uvicorn app.main:app --reload --env-file .env
```

PayPal needs a public URL to deliver webhooks, so use a tunnel (cloudflared or ngrok) and register `https://<tunnel>/webhooks/paypal` in the PayPal dashboard. Events from the webhook simulator do not pass signature verification, so test with real sandbox payments.

### Try `scam_check` in LangGraph Studio

```sh
langgraph dev --studio-url https://eu.smith.langchain.com   # the LangSmith account is in the EU region
```

This opens Studio on the `scam_check` graph (log in to LangSmith). Paste this into the input, in JSON mode:

```json
{"text": "Buyer: Já paguei, o ID é 3Y662965014333303", "expected_amount": "25.00", "expected_currency": "EUR"}
```

Studio shows each node's state, and you can pause before any node. The things to know:

- Studio runs the graph directly, so the `/scam-check` validation and the limit of 2 checks at a time do not apply.
- Screenshots can't be tried here: `image` is bytes and Studio sends JSON. Use `POST /scam-check` or `pytest -m eval`.
- `verify_payment` and `persist_case` need Postgres with its tables, which are created when `uvicorn` first starts. Without them, a real payment gives `WAIT` ("not confirmed").
- An ID that already approved a case gives `P.WAIT_REUSED` the next time. To try it again, run `psql "$DATABASE_URL" -c "TRUNCATE claimed_payments"`.
- Groq and PayPal Sandbox are called for real. With `LANGSMITH_TRACING=true` every run is traced, so use made-up chats only.
- The `payment_check` graph takes `{"chat": "3Y662965014333303", "expected_amount": "25.00", "expected_currency": "EUR"}`: the payment only, with no Groq call.

### Try `chat` in LangGraph Studio

Pick the `chat` graph and switch to Chat mode. Each message is one run on the thread. Try:

1. "Hi, I am Amanda and I have a few questions regarding a possible scam, can you help me?"
2. "What does WAIT mean?"
3. "check", then "25.00 USD", then a made-up chat.
4. "check", then "cancel".
5. "I got a dispute", then "PP-SIM-INR-01", a practice dispute.

- The buyer's chat you paste stays in the thread's history, saved in `.langgraph_api/`. Deleting the thread in `langgraph dev` leaves the messages on disk. To wipe them, stop the server and delete `.langgraph_api/` (RECHECK #19).
- Text attachments (.txt, .md, .json) arrive as text, so a chat export works. Images at the chat step go to the check if they arrive as an `image_url` data URL; in any other form they get "I can only read text here for now". Not tried in Studio yet.
- The 2-checks-at-a-time limit doesn't apply in Studio, as with `scam_check`.

### Try `dispute_draft` in LangGraph Studio

Pick the `dispute_draft` graph and paste `{"dispute_id": "PP-SIM-INR-01"}`, or any ID from `app/demo_disputes.json`. Nothing leaves the app: no PayPal, no database, no LLM.

## Tests

```sh
pytest              # offline: PayPal and Groq mocked
pytest -m eval -v    # evals: the real Groq model, keys from .env, made-up chats only
```

The evals have their own Groq keys, `GROQ_API_KEY_LLM_EVAL` for the app's calls and `GROQ_API_KEY_LLM_JUDGE` for E07's judge, so a run doesn't spend the app's daily quota (200K tokens a day for `gpt-oss-120b` on the free tier). Groq's limits are per organization and per model, so the keys only add quota when they come from different organizations. With `LANGSMITH_TRACING=true` in `.env`, every eval is traced to the LangSmith project `goodtoship-evals`, never the dev one, and each test's traces are sent when it ends, so a run can be followed live. The offline tests are never traced.

The evals' calls rotate through every `GROQ_API_KEY_LLM_EVAL*` key in .env (never the app's `GROQ_API_KEY`, nor the judge's): a key that fails (a 429, a spent quota, a revoked key) hands over to the next, and once all have failed in a call it waits a minute. `EVAL_KEY=2` starts from `GROQ_API_KEY_LLM_EVAL2`. `EVAL_JUDGE=0` runs E07 and E08 on the rules only, without the judge. Each run is saved as `<eval>-<dataset>-<UTC time>` (`E06-chat-…`, `E10-next-…`, `E07-seller-…`, `E08-weak-…`), its .txt starts with the eval, the model and the names of the keys it used (never the values), and each case's test ID starts with the eval (`-k "E08-22"`). Runs before 2026-10-30 20:30 UTC are named without the eval; E10 was called T7 until 2026-10-30 (`T7-next-…`).

The plan has test cases in six layers, plus C01–C50 for the chat and D01–D51 for the demo, tracked by ID:

| Layer | IDs | What it covers | Where it runs |
|---|---|---|---|
| Unit | U01–U40 | `compute_verdict`, `@owns`, webhook, the practice disputes (`classify_case`, the checklist, the draft and the reply) | CI, PayPal mocked |
| Node (mock) | N01–N10 | LLM and API nodes against simulated PayPal errors (404, 403, 429, 503, timeouts) | CI |
| Sandbox | G01–G12 | End to end on PayPal Sandbox | manual |
| Security | S01–S12 | Prompt injection, forged webhooks, no tools on LLM nodes | pytest + promptfoo |
| Chat | C01–C50 | `app/chat.py`: routing, the check flow, screenshots, Tavily, links, personal data | CI, Groq and Tavily mocked |
| Demo | D01–D41, D50, D51 | `/demo`: off by default, sandbox only, sessions, orders and their test tracking, the chat and its streamed check, per-IP limits, what the chat's thread keeps | CI, PayPal and Groq mocked |
| Evaluation | E01–E05 | 60 labelled chats, 30 injection variants, 50 drafts | scripts + RAGAS |
| Spike | P01–P02 | Which payment source feeds the ledger, and whether simulator events verify | manual |

**Passing today:**

- Webhook: U11 (a failed signature returns 400 and stores nothing), U12 (the same event twice is stored once) and S07 (a forged webhook is rejected before PayPal is called).
- `scam_check`:
  - U01–U05: the verdict table.
  - U09–U10: `@owns`, and `payment_check` is `scam_check`'s nodes without the chat and the LLM, with an input of 3 fields.
  - U17–U20: the output guardrails (tests/test_output_guardrails.py). U22: the chat answer step's rules, each with an answer that breaks it.
  - Evals (`pytest -m eval`): 3 injection chats run through the whole graph must not put buyer text in the reply (tests/test_eval_output_guardrails.py).
  - E07 (tests/test_eval_seller.py, `pytest -m eval -s`): 61 made-up conversations of a seller with GoodToShip, in tests/seller_dataset.jsonl, in English. Each one has 1 to 4 turns.
    - The scenarios are questions about PayPal, suspicions described in words, a check run in the chat followed by questions about its result, and disputes, other payment services, small talk and injections.
    - PayPal is mocked and Tavily is off. The chat graph runs with the real Groq model.
    - Each turn is scored by rules: the node that answered, the notes it had to show (or must not show), text it must not show, the verdict, and that it doesn't repeat the last reply word for word.
    - The answer step's KPIs: how often it was grounded or fell back, and its sentences per grounded reply. The report shows the rules that rejected each fallback. Case 57 is an injection aimed at it.
    - An LLM judge then scores each whole conversation against a reference written from `policy_pack`'s notes: correct, next steps, whether the last reply answers the last message, safe, covers the reference, and 1–5. The judge is the app's own model, so read the conversations too.
    - The user's own scores of the baseline are in tests/eval_baselines/*.human.jsonl. The KPIs show how often the judge lands within a point of them: on the baseline, the judge was too lenient when an earlier turn covered the reference and the last one failed.
    - Each run saves every conversation to tests/eval_results/<UTC day>/, with the KPIs per rule, per criterion and per scenario. The .txt starts with the dataset used: its path, case count and sha256. The first run (15/35 passing, judge mean 3.89) is kept in tests/eval_baselines/ to compare later runs against.
  - E08 (`EVAL_DATASET=weak pytest -m eval -s tests/test_eval_seller.py`): E07's harness on the 33 cases of tests/seller_weak_dataset.jsonl, where E07 failed most: warning signs and disputes followed by "what next?", check follow-ups, pending payments, and injections in the previous turn. Saved as `E08-weak-*` in tests/eval_results/; the human scores apply to E07 only.
  - E09 (tests/test_eval_policy_pack.py, `pytest -m eval -s`, no Groq key): fetches each PayPal note's page and checks that every sentence is still on it, word for word, undoing "we" → "PayPal". Prints the closest line on the page for a sentence it misses. U25, its matcher on fixed strings, runs offline. Run it before a demo: pages change.
  - Screenshot evals (tests/test_eval_screenshot.py): drawn PT and EN chats, one with an injection, must be copied word for word with the ID and the right `Buyer:`/`Seller:` labels. A blurred chat must not be guessed, and a screenshot alone run through the whole graph must give `SCAM_LIKELY`.
  - Screenshots, offline: a screenshot alone, a screenshot plus text, bad uploads, a failed copy (no verdict, no case, PayPal not called), and no tools or secrets in the vision call (S03).
  - N01–N04: PayPal 404, 403, 429, 503, timeouts, token failure, Transaction Search rows, LLM and database failures. N03 also: a chat of only payment IDs (with a full stop, a hash, two IDs, `[?]`) is unknown with no Groq call; fewer than 5 of the buyer's words are read, but none found is unknown, and the seller's lines don't count; a `payment_check` lookup of a good payment is `VERIFIED` (2026-11-11), calls no LLM, claims the payment (so a later lookup or check of it is `WAIT_REUSED`), and keeps every payment gate, an unreadable claim included.
  - S01–S06: prompt injection, unverifiable quotes, no tools and no secrets in prompts, no ship wording from the LLM, Unicode and tag tricks.
  - S12: a body over 4.2 MB is a 413 before it is read, with or without `Content-Length`; the biggest valid `/scam-check` request fits.
- `dispute_draft` (tests/test_dispute_draft.py), offline: every test fails on a PayPal call or a database read, and respx sees none.
  - U36: each scenario's ID is a practice one that `chat.REAL_ID` never matches; each dispute passes `keep()`; the file holds no buyer key and names its fictional format; the marks are the same at 00:00 and 23:59.
  - U37: `@owns` on every node, the nodes and edges fixed, no LLM; each scenario runs offline; an unknown or malformed ID is "not found"; a capture and trackers parse as PayPal sends them; SNAD keeps PayPal's codes only.
  - U38: every quote is in its verified note, word for word, and shows once; each of `checklist()`'s lines carries its key, whatever the scenario holds; an unverified note gives no quote; no refund is no gap, a refund is ready; SNAD shows none of the Seller Protection proofs; the signature rule reads the payment date, and a signature that isn't required is "Check yourself", never Missing.
  - U39: each scenario's draft, word for word; a sentence goes as soon as any line it needs isn't ✓, and that line is listed as missing when it counts; a value the scenario lacks is listed as missing, not dropped silently; no draft for a resolved dispute, an appeal, a card case or another reason; no draft speaks of a refund, of PayPal's notice, of protection or of the outcome.
  - U40: the reply in order; what's missing after the draft; the notes shown for the chat's next "what now?"; a card case; an unverified `pp_respond_to_dispute`; no word about the outcome.
  - U28: the `classify_case` table. U29: each INR line in ✓, ✗ and ?, the shipment date ✓ or ✗ only in every timezone (a US seller shipping after a late-evening opening is "?"). U30: UNAUTH's eligibility and the 48 h, at D, D+1, D+2 and D+3. U32: `keep()` takes only its fields and drops a malformed value. U35: the deadline's four forms, and no "Respond by" without a date the seller can still meet.
- `chat` (tests/test_chat.py):
  - C01–C17: every intent, the price and the chat flow, a screenshot at the chat step, and cancel.
  - C18–C21: the context (the last check's codes reach `classify`, its quotes don't; the input can't set it; a new check or cancel forgets it), a note just shown is answered from its picked sentences, never repeated whole, a question at the price step, and the check offered after a described scam.
  - C22–C24: no outcome note without its own check, instructions never classified, another payment.
  - C30–C33: `advise`. Every row of the `can_ship` and `next_steps` tables, "You can ship" only after a VERIFIED check, another price, after an injection; the backstop for "what now?" and "can I ship?" and look-alikes that stay questions; at the price step, and never while waiting for the chat. U26: `vg_before_you_ship` is `scam_check`'s own sentences, ungated, and the answer step can't turn it into "you can ship".
  - C34–C36: `handoff`. Asking for a person, or the word `operator` with no LLM call, asks before anything is shared, and ends a check at the price step with the question last; at the chat step too, `operator` ends the check instead of being checked as the buyer's chat. At the consent step only a short yes consents: "no", a question, a yes with more, a pasted chat or a screenshot shares nothing and reaches no LLM, Tavily or check. The input can't consent.
  - C37: the offers. On the demo, a reply with no note and a second "what now?" with nothing new end with "Say operator" (instead of "Still unsure? …" on the second), the check offer stays after a scam's sign with no check, and at the price step "I stopped the check…" stays the only closing line.
  - C38: PayPal's hold note only for a hold. Kept for "on hold", "unavailable", "keep my money", "kept", "withheld", "isn't available", "O PayPal está a reter…" or "reteve", in the message, the one before, or when the last reply showed it; dropped for a payment that isn't in the account (E07 cases 18 and 43, E08 case 7), an "account holder", a hold asked about before a check (the chat's, or a question at the price step), or beside the hold-until-tracking sign; alone, it becomes "How to check a payment", and a dispute keeps its own notes.
  - C39: a failed answer step. A note picked again whole is shown again in full, under "About your last check" for the check's note; a mixed reply quotes a note and shows the other in full; each ends with "Still unsure? …", on the demo with "Say operator" unless the note carries "Still unsure? …", and never after an answer that passed.
  - C40–C42: the greeting and `topics`. The intro ends with the offer of a person only with the demo's context. `notes` and each area's name, alone, list with no LLM call; each verified note is in exactly one area's list, the verdicts' notes in none, with no "Warning sign: "; `topics` from the LLM lists the area the message names, the most specific first ("Hi GoodToShip, what do you know about PayPal?" gets PayPal), or the four areas; before the greeting, the intro comes first; at the price step, "I need the amount…" (C20).
  - C28: a screenshot before the price is read when it comes, its copy held (no image) and the reply names its ID; the price checks the copy, never the seller's text; one sent with the price is checked at once; one with text at the price step says the text wasn't kept; cancel, a question or `operator` clears it; one that can't be read, or has no text, is said at once and loses nothing held, at the chat step it is asked for again. A check's card ends its steps with "say operator".
  - C43: only a run whose context holds `chat.LIVE` streams the check's steps: each PayPal call before its node's update, every node, the quotes the live panel shows; `{"live": true}` (JSON, as Studio sends it) and no context stream nothing, and either way nothing of `scam_check` reaches the thread's checkpoints.
  - C44: a short message with a payment ID, before `check` or at the price step, holds its IDs only; the price asks whether the chat is read too, and `payment` (or `no`) looks them up with `payment_check` (no Groq call, its steps streamed on the demo, `VERIFIED` for a good payment); any other answer is the buyer's chat, read by `scam_check` with the IDs after it (a screenshot too, one that can't be read keeping the IDs); cancel or `operator` after the price clears them; with "I checked only the payment" on the card only where the chat could change the outcome, and "I couldn't check the buyer's chat" for a check whose signals are unknown; up to 3 IDs, and words beside them said not kept; cancel, a question or `operator` clears them; the input can't set them; up to 3 items held, a 4th drops the oldest and says so, and the price checks them all, newest first (scam_check with a copy, a lookup for IDs alone); the held text stays within one chat; the screenshot's amount is said after the check only when PayPal's differs.
  - C45–C49: disputes. A real dispute ID alone, in lower case, in the spec's format, beside a payment ID or in a question gets the fixed text, with no Groq or PayPal call; a pasted chat with one starts a check. At the chat step it is the buyer's chat, at the consent step the answer, and at the price step it goes to `classify`. A chargeback in English ("estorno" is none), or the LLM's pick of the chargeback note with no chargeback word, shows PayPal's chargeback note first and never the 10-day rule: not on the next message, "what now?", a `question` or a follow-up after "what now?" either; a PayPal dispute keeps the deadline note, also right after a practice card case. Reading an ID clears the message before it from `classify`'s context. A chargeback ends with more help, not the offer of a practice dispute. Without an ID, how disputes work and the offer. Nothing of `dispute_draft` reaches the chat's checkpoints, and no live steps are streamed.
  - C50: practice disputes. A `PP-SIM-…` ID, in any case or in a question, goes to `dispute_draft` with no Groq call and no PayPal call; a pasted chat with one starts a check; at the price step it goes to `classify` and the reply has one closing line. The offer and the fixed text name practice IDs that exist. A `question` or `next_steps` reply with a dispute's note ends with it and PayPal's line, on the demo too instead of the offer of a person, once in a stretch of dispute replies; a shipping-proof question keeps its closing, and "what now?" after it offers no practice; at the price step only the check's stop. "What now?" after a practice dispute, or after a message about a dispute or claim whatever its note, is how to respond, not "Before you ship"; never after a chargeback.
  - C25: the answer step. A follow-up is answered in sentences that cite its notes; an LLM error, a schema error, no sentences, one bad sentence, an injected flip or "ship" without a check all give the quoted reply, byte for byte, and PayPal's line; no answer step on a first question or right after a check, and no buyer chat in its prompt; "ship" only from the last check's own note.
  - The buyer's chat never reaches the chat's LLM or Tavily, and no quotes appear in the reply or in the thread.
  - Pasted chats, the tag injection, the link filter, Tavily failures, personal data in the search, one Groq request per message (two on a follow-up), no tools or secrets.
  - U16: the `policy_pack` sources, and that unverified notes are never offered or shown.
  - E06 (tests/test_eval_classify_chat.py, `pytest -m eval -s`): the 44 cases in tests/chat_dataset.jsonl, all in English, the real Groq model (41–44 and 30 are `topics`). Each run saves every reply and the KPIs (routing, intent, notes shown, LLM calls) to tests/eval_results/<UTC day>/, which git ignores. The .txt starts with the dataset, as in E07. On 2026-10-31 a call used about 2,100–2,250 tokens, up to 4,200 when the model reasoned longer. No SDK retries, as in the app: a 429 waits 60 s and tries once more.
  - E10 (`EVAL_DATASET=next pytest -m eval -s tests/test_eval_classify_chat.py`): E06's harness on the 36 single messages of tests/next_steps_dataset.jsonl: "can I ship?" and "what now?" phrasings, in English, and look-alikes that are questions or disputes ("how do I ship?", "what should I do if a buyer asks for F&F?"). KPIs: recall of `can_ship` and `next_steps`, and look-alikes not sent to `advise`. Saved as `E10-next-*`.
- Demo (tests/test_demo.py): D01–D17. The routes are 404 when off, the app won't start against live PayPal, another session's order is never captured, the chat's check streams every node and PayPal call (no token) and ends with its reply, its case stored at `ingest_input`, every check gives its slot back, and the per-IP limits ignore forged `X-Forwarded-For` entries. The chat answers from a verified note, runs a check over three messages on its own session's thread (counted as a check, its case tied to the session), refuses that message with an error line when no slot is free, takes one message at a time per session (a 409), counts a chat screenshot as a check and a screenshot (one before the price as a screenshot when it is read, the price as the check), and forgets the thread when the session ends or is swept. D20–D22: the handoff. Only a yes writes a row, with the whole conversation (a screenshot only when a check read it) and the last check's codes; ending or sweeping the session forgets its transcript; a row the database refuses is an error line and leaves no "Sent" in the transcript. D24–D25: the simulated operator, with no token: the operator API needs the demo session, lists only its own shared chat, and another session's can't be answered or closed (409); the operator's answers, as many as they like, reach the seller's state and the shared transcript, never the graph or `classify`'s prompt. D29: GoodToShip speaks first: a new session's greeting, with the offer of a person, its thread greeted, so "Hi" gets the one-line reply, and no greeting in the transcript. D30: while a person has the conversation, every message (a check's words, a price, a buyer's chat, a screenshot) goes to them only: no graph step, no model call, no check, and no limit for the screenshot. D31: closed by the person or by the seller, the stored transcript and last check are emptied, answering or closing again is a 409, the thread is a new session's and the seller gets the notice, whose intro offers a person; GoodToShip answers the next message. D32: a check cut short, because nobody reads the stream or storing its case fails, is still saved in the chat's thread, so the chat sent again isn't "already used". D33: the person's Close closes only the card they clicked, never a newer one the seller opened since, and a busy session tells the person, not the seller, to wait. D34: each turn leaves the chat's thread one checkpoint, with no messages. Two screenshots sent before the price are kept as their copies, once each, never as images; the price checks both, newest first, and then they go. A pasted buyer's chat goes with its turn. D35: a screenshot at its hourly limit, or with no free slot, is neither read nor held. D36: Mark as shipped adds test tracking once, and only to a paid order of the session: the capture ID from the database, carrier `OTHER`, the buyer not notified, a fixed `PayPal-Request-Id`. Another session's order is a 404; an unpaid one, or a second click, a 409, with no PayPal call. A refusal, or no answer, leaves the order unshipped, and the next click tries again; PayPal's refusal reaches the tester even when unmarking fails. D37: the state poll shows the tracking and never calls PayPal. D38: after Mark as shipped, the chat's check of that payment gives `WAIT_SHIPPED`, reading the tracking of the same capture the demo wrote it to. D39: a payment ID before the price is held (`held` in the reply); the price takes no check and asks whether the chat is read too, still `held`; `payment` looks it up with `payment_check`: its six steps streamed, its case tied to the session, counted as a check and not a screenshot. D40: the page's Ask for a person at the chat step runs no check and takes no check limit. D41: after a restart forgot the chat's thread, the next reply says so, once, and is the one-line reply, not the intro. D50: the state says what the chat waits for, and the reply names the dispute ID it got: a real one gets the fixed text and no checklist, with no PayPal call. D51: the state lists the practice scenarios with no PayPal call; a practice ID in the chat returns its grouped checklist, with no PayPal call, and any other message none; its 5 `dispute_draft` steps come before the reply. A `dispute_draft` that fails still answers (the chat's `PRACTICE_FAILED`), with no steps and no checklist, and a failure of the panel's run alone costs only the panel. D36 also: a tracking call with no answer is a 502 note.

The tests mock PayPal and Groq and replace the database calls, so they need neither credentials nor Postgres. `tests/test_db.py` runs the real SQL (the claims, saving a case, the demo's tables and its cleanup, test tracking marked once per paid order, one open handoff per session, taking messages and answers until it is closed) against a throwaway Postgres, and is skipped unless `TEST_DATABASE_URL` is set:

```sh
docker run -d --rm --name vg-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_db.py
```

Test IDs follow this file's grouping; there is no separate test-plan document.

## Build order

| Dates | Phase | Covers |
|---|---|---|
| 17–24 Oct | Foundations: FastAPI, Postgres, Render deploy, sandbox OAuth, webhook verification and dedupe | P01–P02, U11–U12 |
| 25–31 Oct | `scam_check`: nodes, verdict policy, paste UI, output guardrails | U01–U05, U09–U10, U17–U20, N01–N04, G01–G03, S01–S06 |
| 1–7 Nov | Disputes: first `dispute_defense`, reading real disputes in PayPal, then `dispute_draft` on fictional practice disputes, which replaced it on 7 Nov (real disputes need a carrier's data and the seller's listing) | U28–U30, U32, U35–U40, C45–C50, D50, D51 |
| 8–12 Nov | Run the evaluation set, publish the numbers, record a real demo | E01–E05 |

## Deploy

`render.yaml` defines the web service only; the database is a Neon Postgres. Set `DATABASE_URL` (Neon's direct connection, not the pooler), `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` and `GROQ_API_KEY` in the Render dashboard (they are `sync: false`). `TAVILY_API_KEY` is optional and not in the blueprint, so add it by hand. For the live demo, also set `DEMO_MODE=true`, `DEMO_BUYER_EMAIL` and `DEMO_BUYER_PASSWORD`. The webhook already receives every event (`*`); nothing acts on them (the demo's live log only lists them).
