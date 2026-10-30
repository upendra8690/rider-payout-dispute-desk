# Rider Payout Dispute Desk

An agent that settles rider payout complaints over WhatsApp: it talks to the rider, checks trips, payout lines and PaySwift, pays small verified shortfalls itself, and sends everything else to ops with the reasoning attached.

## Run it
```bash
cp .env.example .env          # optional: OPS_API_TOKEN, LLM_PROVIDER + LLM_API_KEY (groq | gemini | openai | anthropic)
docker compose up --build     # app :8000 (GET /health) · PaySwift :8081 · Postgres (volume)
python evals/run.py http://localhost:8000      # run from the repo root
```
Ops console: http://localhost:8000 (token = `OPS_API_TOKEN`, default `change-me-ops-token`). Tests: `pip install -r requirements-dev.txt && pytest` (needs Postgres at `TEST_DATABASE_URL`; tests start the PaySwift sandbox themselves, which needs Python 3.12). CI runs lint, tests and the evals against `docker compose` on every push.

## Design
`POST /messages` → **dedupe** on `message_id` (unique key; a re-delivery replays the stored reply) → **per-rider Postgres lock** → **interpret** (LLM or rule parser) → **security screen** → **reconcile** each claimed IST day against trips, payout lines and PaySwift → **authorize** (Python rules) → **pay** via PaySwift → **reply** from templates. Every step is written to `/trace/{rider}`.

| The model decides (only) | Code decides |
|---|---|
| Language, intent, and the claims: category (surge / incentive / penalty / distance / unpaid / general), date, trip IDs, claimed amount or trip count. Also flags: correction, pushback, status question, accident, wants a human. | Expected pay (Decimal, ₹25 + ₹6/km after 2 km, surge on the whole fare, 0.5 rounds up, ₹150 at 12+ trips per IST day, −₹10 per rider cancellation); what is owed; who pays (auto ≤ ₹200, max 1 per rider per day, else ops); escalations; idempotent payouts and retries; the facts in every reply. |

LLM output is schema-validated. Trip and rider IDs not literally in the message are dropped, as are future dates, and the rule parser's safety flags always apply. If there is no key, or the LLM fails, the deterministic Hinglish/English/Hindi parser takes over. **Agent tools** (as they appear in the trace): `interpret_message`, `payswift.list_payouts`, `lookup_trips`, `compute_expected_vs_actual`, `payswift.create_payout`, `payswift.verify_payment_status`. **Decisions**: `security_screen`, `check_dispute_window`, `verify_claim`, `authorize_payout`, `request_ops_approval`, `escalate_to_ops`, `recheck_after_pushback`, `duplicate_delivery_ignored`. **Memory**: the conversation row (pending question, last disputes) plus message history. **PaySwift**: idempotency key `qd-<dispute>-<mode>`, unique per dispute. PaySwift payout references carry the paid items, so "already paid" and "one auto-pay per day" are reconciled **against PaySwift**. 503s are retried; timeouts and 504s are reconciled before any retry; 429 backs off 61 s. Anything that doesn't finish in the 8 s webhook budget is completed by a background worker. Every attempt is logged.

## Assumptions
- `received_at` is "now": it drives "kal", the 7-day window (day ≥ today − 7) and "per day" (IST).
- Fixed on import and shown in the console: rider IDs `R7`/`r19` → `R007`/`R019`; 10 duplicate trip rows; 222 distances in metres (≥ 100 → ÷ 1000); trips are grouped by IST day, not UTC.
- One dispute = one claimed IST day. A complaint naming no date or order gets a question back (no blind scan). If the stated reason is wrong but that day is short, the real shortfall is paid. The payment is capped at the day's net shortfall, so an overpayment that day offsets it.
- "Repeat" means items the desk already paid. Escalated: prompt injection, impersonation, another rider's trip (not revealed to the sender), accident, a rider disputing our records after a recheck, a request for a human, 3 unclear messages, a payout failed for good, an agent error. Not escalated: out-of-window claims, or claims larger than the verified amount; both get an explanation instead.
- `GET /trace` and `GET /ops/pending` are open because the evals must read them. Every other `/ops/*` route needs the token.

## Eval results
`python evals/run.py http://localhost:8000` against `docker compose`, with PaySwift chaos on: **38/38 conversations passed (24/24 provided + 14 of mine), 367/367 checks.** Reply latency p50 0.09 s, max 6.7 s. Mine cover corrections, repeats, the limits, Hindi, English, a PaySwift outage, pushback and triple delivery. The runner uses only the public interface and PaySwift, resets with `docker compose down -v && up --wait` between rounds, and checks money **in PaySwift**. `pytest`: **113 passed**. These runs used the rule parser (`LLM_PROVIDER=none`).

## Skipped
- Not run against a live LLM (I had no key). All four providers are tested with mocked responses.
- No proactive messages (the vendor only takes replies), so ops decisions reach the rider on their next message.
- A shared ops token, not per-user auth.
- `create_all` instead of Alembic.

More detail: [docs/DESIGN.md](docs/DESIGN.md). Raw AI session logs: `ai-logs/`.
