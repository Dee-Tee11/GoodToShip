# Recheck

Open findings from the `/code-review` run on 2026-10-19, with their original numbers. The review read the code only. It did not run anything. Points from #15 on came later and say where they came from.

This file is a to-do list: when a point is fixed, delete it and note the fix in `CHANGES.md`. If a point turns out to be wrong or no longer relevant, write the reason next to it instead of deleting it.

Authentication and authorization are still being built, so some of these may no longer apply once that lands. Where I think a point depends on the auth work, I tagged it **(depends on auth)**. That tag is my guess, not the reviewer's.

Points marked with \* are important: do them first.

## From the evals of 2026-11-07

Added 2026-11-07, after the review fixes and the E07/E08 reruns judged by Claude Haiku 5.5 (`E07-seller-20261107T193005Z`, `E08-weak-20261107T193221Z`; see CHANGES.md). First in this file at the user's request.

- [ ] **52. The answer step falls back on every E08 follow-up.** [app/output_guardrails.py:136](app/output_guardrails.py#L136)
  4 of 4 on 2026-11-07 evening, 3 of 3 that morning. The rejections: "a permission not in its sources" (three times), "drops its sources' negation", "a verdict or shipping without the last check's note". The fallback quotes the 1 or 2 sentences `classify` picked, which reads as a truncated repeat: the judge's reason for `answers_last` at 6/10 (E08 3, 12, 13, 16).
  The .txt shows only the rejecting rules' names. The rejected sentences are in LangSmith, project `goodtoship-evals` (EU), in each fallback's answer-step call: E08 3 turn 2 ("How do I protect myself then?"), 10 turn 2 ("What evidence should I send?"), 12 turn 2 ("Who decides the outcome?") and 15 turn 2 ("How do I refund them?").
  *To do:* read the rejected answers and decide whether the permission rule is too strict for these notes, or the prompt. Any change reruns C25, C39, U22 and E07 57 (the injection aimed at the answer step).
  *2026-11-09:* the ship and permission rules relaxed for PayPal's own requirements and a synonym verb (CHANGES.md). E08 3 and 15 now answer. Open: E08 12, "The card issuer decides the outcome." fails "drops its sources' negation" ("PayPal doesn't decide the outcome"), a true sentence the lexical rule can't tell from a flip; the prompt line added that day didn't change the wording. Decide whether that is accepted or the rule learns the negated clause's subject.

- [ ] ~~**53. One judge sample per case.**~~ *Dropped by the user, 2026-11-08: not worth it, the evals keep changing with each iteration.* [tests/test_eval_seller.py](tests/test_eval_seller.py)
  A point up or down between two runs is within the judge's noise (E07 59 and 60, E08 12 on 2026-11-07).
  *To do:* for the published numbers (8–12 Nov), run with `EVAL_REPS=2`: each conversation runs twice and is judged twice, by the same judge, so a score is the mean of two. Keep one judge model for every run that is compared, the baseline's included.

## Security hardening

- [ ] **3. Unauthenticated callers can trigger blocking PayPal calls.** [app/main.py:45](app/main.py#L45) **(depends on auth)**
  There is no body-size limit, rate limit or concurrency cap, and the same 40-thread pool serves `/healthz`. A flood of plausible-looking requests can exhaust it, fail Render's health check and restart the service.
  *Fix:* cap body size, add cheap pre-checks, a concurrency limit and a rate limit.
  *2026-10-20:* `/scam-check` adds a bigger target: each call costs 2 Groq calls and up to 6 PayPal calls. It now has a global limit of 2 concurrent checks (503 above that), and `/healthz` is `async`, so it no longer waits for a pool thread. Body size and per-caller rate limits are still open.
  *2026-10-21:* screenshots make a normal body up to about 4 MB, and a screenshot adds a third Groq call. The 3 MB image cap is checked only after the whole body has been read, so the body-size cap matters more now.
  *2026-10-22:* the image is now capped as base64, before it is decoded. The whole body is still read and parsed as JSON first, whatever its size.
  *2026-10-24:* `/scam-check` and the demo now have per-IP hourly limits (`app/ratelimit.py`), in memory. The body-size cap is still open.
  *2026-10-30:* every route now rejects a body over 4.2 MB with 413 (`MAX_BODY` in `app/main.py`), on `Content-Length` before reading, or on the bytes read when chunked. Still open: `/webhooks/paypal` has no per-IP limit, so a forged webhook with plausible headers still costs one `verify-webhook-signature` call; and per-seller limits wait for auth.

- [ ] **18. Screenshots go to Groq with their metadata.** [app/llm.py](app/llm.py)
  Added 2026-10-21 with the screenshot input, not from the review. The image bytes are sent as uploaded. A phone photo of a screen can carry EXIF data (GPS position, device), and so can a JPEG. With `LANGSMITH_TRACING=true` the image is also stored in the traces.
  *Fix:* before real cases, re-encode the image without metadata (Pillow would then be a runtime dependency), or reject images that carry EXIF.
  *Same deadline, from #14 (removed 2026-10-31):* Neon's Free plan has no IP Allow, so the connection string is the only lock on the database. Move to Scale, or another plan with an allowlist, before real cases.
  *2026-10-30:* with the seller's yes to a handoff, the screenshots a check read are also stored in Postgres (`handoffs`), metadata included, until the conversation is closed or the session ends, for the operator page to show.

- [ ] **23. The per-IP limit trusts the last `X-Forwarded-For` entry.** [app/ratelimit.py](app/ratelimit.py)
  Added 2026-10-24 with the live demo. `client_ip()` takes the last entry, assuming Render's proxy appends the caller's address after anything the caller sent. Not checked on Render. If Render puts another proxy in front, the last entry could be that proxy, and every tester would share one limit. If it doesn't append at all, a caller could forge its address.
  *Fix:* on the first deploy, send `curl -H "X-Forwarded-For: 1.2.3.4" https://<app>/demo/api/session` a few times and log what `client_ip()` sees. Then fix the rule in `client_ip()` only.

- [ ] **40. Tracking is checked only on checkout payments.** [app/scam_check.py](app/scam_check.py)
  Added 2026-11-02, with the tracking read of 2026-11-01 (see CHANGES.md). `verify_payment` reads a payment's tracking before approving it (`GET /v1/shipping/trackers?transaction_id=`). In the sandbox that read was checked only on checkout captures, by the capture ID, with tracking added through the API.
  Not checked:
  - a goods-and-services payment sent from paypal.com (Send money), or an ID that only Transaction Search finds. If the Tracking API refuses such an ID, that real payment always gets `WAIT_NOT_CONFIRMED`: safe, but never `VERIFIED`;
  - tracking the seller adds by hand on paypal.com. If the API doesn't show it, a payment already shipped against can still be approved.
  *Fix:* in the sandbox, send a goods-and-services payment from the test buyer on sandbox.paypal.com, find its ID, and call `paypal.get_trackers(id)` before and after adding tracking to it by hand. If the API refuses the ID, decide how such a payment can be approved.

## LLM output

- [ ] **16. Nobody measures whether `detect_signals` catches real scams.** [app/scam_check.py:183](app/scam_check.py#L183)
  Added 2026-10-21, from a design discussion, not the review. No eval scores `detect_signals` on its own. The signals never decide `VERIFIED` (PayPal does), but they decide `SCAM_LIKELY` and the warnings, and their recall and false-alarm rate are unknown.
  *Fix:* an opt-in eval (`pytest -m eval`) with 30–40 made-up but realistic chats in PT and EN: OLX/Vinted scams, MB WAY, "paguei a mais", own courier, fake PayPal emails, plus honest buyers. Check the expected signal types and `confirmed` per chat, and report recall and false alarms per signal. A second LLM reviewing the first is not the fix: it reads the same buyer text, so the same injection fools both.
  *2026-10-21:* screenshots add a copy step before `detect_signals`. With a screenshot, `confirmed` means "in the model's copy", not "in the image". Run the same chats as screenshots too: `screenshot()` in `tests/test_eval_screenshot.py` draws them. Add real phone screenshots (WhatsApp, OLX, Vinted, dark mode) and photos of a screen, since the drawn ones are clean.
  *2026-10-31, E07 case 26* (`E07-seller-20261031T104530Z`): "Here is my payment from last month, ID …" got the email-or-screenshot-as-proof sign, which the chat doesn't contain. The card then told the seller not to trust emails or screenshots.

- [ ] **17. Consider Jev (TypeSafe AI) for `detect_signals`.**
  Added 2026-10-21, an idea, not a finding. Jev does not generate text: it takes a state and typed questions (yes/no, rubric, multiple choice) and returns probabilities. That matches `detect_signals` (one yes/no per signal type) and would give a tunable threshold instead of a quote or null. It could also judge whether the check's explanation reads well (the open part of #25, removed 2026-10-31: E07 and E08 now count how often the template replaces the LLM, and if it's most of the time, drop the LLM from the explanation). It is reachable from Pydantic AI (`typesafe:jev-latest`), DeepEval and Promptfoo.
  *Before deciding:* run it on the #16 eval and compare it with the current Groq model. Check pricing and data handling in TypeSafe's own docs, because buyer chats would be sent to a new vendor. Pin a version (`jev-1.13.0`-style) if a threshold is tuned. It returns no quote, so the `confirmed` check that stops invented quotes would need a replacement.

## Chat

- [ ] **19. The chat thread keeps the buyer's chat.** [app/chat.py](app/chat.py)
  Added 2026-10-22 with the `chat` graph, not from the review. In the Chat tab every message enters `messages` before any node runs, so the checkpointer stores the buyer's chat (in `langgraph dev`: `.langgraph_api/`). `/scam-check` never stores it. The chat's LLM never reads it again, but it is personal data at rest. `RemoveMessage` only clears the latest state, and deleting a thread in `langgraph dev` leaves the message blobs on disk.
  *Fix:* before the chat gets an API endpoint, take the buyer's chat in a field that is not a message, or run the check on a thread with no checkpointer.
  *2026-10-25:* the chat now has a demo endpoint (`POST /demo/api/chat`). There the thread is in memory and deleted when the demo session ends or the service restarts, so nothing reaches disk. Still open for any endpoint outside the demo.
  *2026-10-27:* the thread also keeps `last_question`, the seller's last message that `classify` read (never the buyer's chat, which never reaches `classify`), and `last_case`, codes only.
  *2026-10-30:* not fixed by the handoff, which adds to it. The demo also keeps (always since 2026-11-08, when the operator became a simulation with no token) each session's whole conversation in memory, outside the graph (`demo._transcripts`), so a person from GoodToShip can read it: the buyer's chat pasted at the chat step and the screenshots a check read included. Since 2026-10-31 the demo has no check panel: every check runs in the chat. With the seller's yes to `handoff`'s question, it is stored in Postgres (`handoffs`) until the session ends, then deleted with it.
  *2026-11-01:* the thread also holds a screenshot sent before the price, in the `screenshot` field, until the price runs the check or the check ends (cancel, a question at the price step, `handoff`).
  *2026-11-03:* instead, up to 3 items in `held` until the price: a screenshot's text as the vision model copied it (the buyer's chat, at rest in the checkpointer as before, but never the image), and the IDs of short messages. Cleared in the same places.
  *2026-11-01, later:* on the demo, the thread now keeps only its latest state, without its messages (`demo._keep_latest`). A buyer's chat pasted at the chat step, or a screenshot a check read, leaves the thread when its turn ends, not when the session does. A screenshot held for the price stays in `screenshot` as before, and `_transcripts` is unchanged. Still open for Studio and `langgraph dev`.

- [ ] **21. The PayPal notes cover only part of PayPal's rules.** [app/policy_pack.py](app/policy_pack.py)
  Added 2026-10-22, when the 17 PayPal notes were verified. A question outside them gets Tavily links only.
  *Still to find and verify, each from a `https://www.paypal.com/us/` page (updated 2026-11-07):*
  - how to respond to a Significantly Not as Described dispute, and what evidence it needs. Until then a practice SNAD shows `pp_snad_not_covered` and the listing, with no advice on how to answer it. The research of 2026-11-04 found SNAD advice only in non-US copies of help1083: the US Help Center and Seller Protection pages returned 403 to its fetcher, so check them in a browser;
  - chargebacks: the seller's deadline to respond, and the chargeback fee (no US Help Center source was found on 2026-10-22). `pp_chargeback` says only what one is and who decides;
  - whether to ship while a payment is on hold (E07 case 2): `pp_payment_hold` says to add tracking and that the hold is released after delivery, which implies yes, but no note says it in PayPal's words;
  - reserves and account limitations;
  - Seller Protection for intangible goods and services;
  - the items `pp_seller_protection` and `pp_proof_of_delivery` skip; then drop their "This is not PayPal's full list" sentence (`policy_pack.py`).
  *Before the demo:* rerun E09 (`tests/test_eval_policy_pack.py`, 20 of 20 notes on their pages on 2026-10-30): pages change. It reads the HTML the server sends: `pp_add_tracking`'s page also embeds a variant ("Go to your Activity. Choose “All Transactions.” Find the purchase you want to update, then click Get your money."), which a browser may show instead.
  *Done since 2026-10-22, verified by the user on 2026-10-30:* how to respond to a dispute and what to send (`pp_respond_to_dispute`, which an Item Not Received dispute uses), unauthorized-payment claims (`pp_unauthorized_claim`), and proof of shipment with signature confirmation (`pp_proof_of_shipment`). The dispute's deadlines were already in `pp_dispute_deadline` and `pp_dispute_vs_claim`.

- [ ] \* **27. No dataset for multi-turn chats and screenshots.** [tests/chat_dataset.jsonl](tests/chat_dataset.jsonl)
  Added 2026-10-25, after #26. The 39 cases are all one text message each, so they test `classify`, not how the chat behaves over several turns or with images. #26 was found by hand.
  *To do:* about 30–40 multi-turn cases, scored on the reply and the state (`awaiting`, `price`, whether `scam_check` ran and with what):
  - a screenshot with or without text, at each step (before `check`, at the price, at the chat);
  - "check" written in other ways ("can you verify this?", "é seguro enviar?", "is this payment real?");
  - a price and the chat in one message;
  - cancelling, or asking a question, halfway through;
  - an image that isn't PNG or JPEG, and two images;
  - EN and PT.
  Draw the screenshots with Pillow, as `tests/test_eval_screenshot.py` does, or use the demo's "Buyer's receipt" and "Seller's receipt". Run it like the E06 eval, and check #26's fix against it.
  *2026-10-30, partly covered by E07* (`tests/seller_dataset.jsonl`, 57 cases): most are 2–7 turns, each turn scored on the node, the notes and the verdict. Still missing: real images (no E07 case sends one), PT, other ways to say "check", and the screenshot sent before `check` (#26's case, fixed with a reply written for an image: see CHANGES.md, 2026-10-30; since 2026-11-01 it is held and the price runs the check).
  *2026-10-30, later:* E08 (`tests/seller_weak_dataset.jsonl`, 30 cases, run with `EVAL_DATASET=weak`) adds multi-turn cases where E07 failed most: warning signs and disputes followed by "what next?", check follow-ups, pending payments and injections. Still no images and no PT.

- [ ] **29. No dataset to see the outcome and the next steps the chat gives.** [app/scam_check.py:59](app/scam_check.py#L59)
  Added 2026-10-25, after `STEPS` and `SIGNAL_STEPS`. U16 and S02 check that every policy id and signal has its steps, not that a real case gets the right ones. #27 scores the chat's flow and #16 the signals; this one scores what the seller is told to do at the end.
  *To do:* a dataset of cases, each a PayPal answer (mocked, as in `tests/test_scam_check.py`), a price and a buyer chat, with the expected policy id, the steps shown and the follow-up in the chat:
  - every policy id at least once, and the wrong amount both higher and lower than the price;
  - each warning sign alone, and two or three together, so the order and number of extra steps can be seen;
  - after the check, the seller asks "what do I do now?", "can I ship?", "o que faço agora?" or pastes the card's sentence: the reply should give the same steps as the card, through the matching `vg_*` note;
  - EN and PT.
  Save each run's verdict, steps, support line and chat reply to `tests/eval_results/`, so the outcomes can be read side by side. Read the first run by hand before scoring anything: are the steps right for the case, in a sensible order, and not too many?
  *2026-10-30, partly covered by E07:* some cases ask "What do I do now?" or "Can I ship?" after a check, and each run is saved to `tests/eval_results/`. Still missing: every policy id and signal combination on purpose, and PT.

- [ ] **47. The chat remembers only its latest check.** [app/chat.py](app/chat.py)
  Added 2026-11-07, the open part of #30 (closed the same day: see CHANGES.md) and of #36 (removed 2026-10-31). `last_case` holds one check, so a second check in the same chat replaces the first:
  - E07 51 (`E07-seller-20261031T104530Z`, judged 2): after two checks, "Can I ship the first order, the 25 dollar one?" gets "Your last check (… 40.00 USD) covered only that payment. To check the other payment, say check.", but the 25 USD order was already VERIFIED in this chat, and a re-check of it would say "already used".
  - E07 44: a re-check of a payment this chat already VERIFIED says "already used to approve an order", which is right, but doesn't name the earlier case as this chat's, and its steps don't fit a re-check of the same order.
  *Fix:* keep the chat's earlier checks (case, price, verdict) for `advise` to match by price, and name the earlier case on a re-check. A graph change: plan it first. Any change that puts a check's content in `classify`'s context must rerun E07 37 and 51 (on 2026-10-30 the case note's sentences, framed as history, broke both).

- [ ] \* **35. "What now?" gets steps that don't fit what the seller just said.** [app/chat.py:607](app/chat.py#L607)
  Added 2026-10-31, from E07 and E08 judged by Claude. `advise` answers from the last check or the last sign shown, else "Before you ship":
  - E07 53: after a WAIT, "But I already shipped it. What now?" gets the WAIT steps again, still "before you ship". The reference: contact PayPal now, keep the tracking and the proof of shipment.
  - E08 2: "I posted the item yesterday…" didn't get `vg_already_shipped`, so "anything else?" got "Before you ship".
  - E08 1 and 7: "what do I do with the order?" after an overpayment, and "wait or cancel?" after a fake email, get the shipping checklist; neither says to cancel or refund the whole payment.
  - E07 54: after a VERIFIED check, "the buyer wants me to ship to their cousin's address, that's fine, right?" gets the VERIFIED recap ("You can ship", "no warning signs"), not a no; it also fails the `no_notes` rule.
  *To do:* read "already shipped" in the message itself, not only in the notes shown, and plan what a ship question that brings a new warning sign gets. A graph change: plan it first.
  *2026-11-10:* done in code (CHANGES.md): `SHIPPED`, `NEXT_AFTER_SIGN` and a new sign for `can_ship`. E08 1 and 2 checked on E08; E07 53 and 54 not rerun yet. Still open: a follow-up `classify` sends to `question` instead of `next_steps` (E07 43) never reaches `advise`; no note says "refund the whole payment and ask for the exact price" (E08 1, 21).

- [ ] \* **37. A question at the chat step is checked as the buyer's chat.** [app/chat.py:485](app/chat.py#L485)
  Added 2026-10-31, from E07 case 50, judged 2. "Wait, what does SCAM_LIKELY mean?", sent when GoodToShip asked for the buyer's chat, ran `scam_check` and got a WAIT verdict with a case. The reference: no verdict, say no payment ID was found, ask for the chat again, and say cancel lets the seller ask.
  *To do:* decide in code what at the chat step is not a buyer's chat (no transaction ID, a question mark, no `Buyer:` label?). A buyer's chat with no ID is valid input today and gets WAIT, so this needs a plan first.

- [ ] **38. `classify` picks notes that don't fit the message.** [app/chat.py:407](app/chat.py#L407)
  Added 2026-10-31, from E06, E07 and E08 judged by Claude:
  - E07 17: "Can a card buyer take the money back after I ship?" got the refund note, not `pp_chargeback`.
  - E07 24: "Does that mean the buyer is a scammer?", after a WAIT for an ID PayPal can't find, got `vg_verdicts`, not `pp_different_ids`.
  - E07 26: "What should I ask the buyer?" after a too-old payment got the email warning, not `vg_wait_too_old`.
  - E08 5: "The buyer wants to continue on WhatsApp…" was `other` (the intro), so `vg_signal_off_platform` never came.
  - E06: 5 of 40 single messages show an extra note beyond `must` and `ok` (cases 5, 7, 9, 10, 12), mostly relevant ones: decide whether the dataset's `ok` lists or the picks are wrong.
  *To do:* the retrieval fixes, case by case; rerun E06, E07 and E08 after each prompt change.

## Disputes

GoodToShip practises disputes on fictional scenarios only (`dispute_draft`) since 2026-11-07: the points left out of the real-dispute graph went with it (CHANGES.md).

- [ ] **46. "What now?" after a mistyped practice ID gets "Before you ship".** [app/chat.py](app/chat.py)
  Added 2026-11-06, from the review of `dispute_defense`; accepted for now by the user. A reply that leaves `shown` empty (a practice ID that doesn't exist, or a real dispute ID's fixed text) lets `advise` answer "what now?" from the last check's note, or with "Before you ship": shipping advice for a seller with an open dispute. A practice dispute leaves `pp_respond_to_dispute` in `shown` (a card case `pp_chargeback`), so it is fine. A reply with a dispute's note ends with the offer of a practice dispute and PayPal's line, once in a stretch of dispute replies (2026-11-08), so "what now?" right after a practice dispute no longer offers it again.
  *Fix:* a new state field, for example `last_dispute` (the kind and the ID), that `advise` reads. A graph change: plan it first.
