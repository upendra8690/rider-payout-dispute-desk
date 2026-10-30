# Design notes and interview guide

## 1. The problem in one paragraph
Riders message QuickDrop on WhatsApp saying their payout is wrong. Today three people look up the trips, recompute what the rider should have earned, compare it with what was paid, and reply. That takes two days. This service does the same job in under a second. It works out what the rider is complaining about, recomputes the earnings from the policy, compares them with the payout lines **and with PaySwift**, and then does one of three things:
- pays small, verified shortfalls itself;
- explains why there is nothing to pay;
- hands bigger, repeat or suspicious cases to ops, with the evidence attached.

## 2. Architecture
```
 WhatsApp vendor ──POST /messages──► FastAPI ──► DisputeAgent
                                                 │ 1 dedupe (messages.external_id UNIQUE)
                                                 │ 2 per-rider advisory lock (Postgres)
                                                 │ 3 Interpreter: LLM (optional) → validated JSON  ┐ cross-checked by
                                                 │                rule parser (always runs)         ┘ the rule parser
                                                 │ 4 security screen (injection / impersonation)
                                                 │ 5 reconcile: trips + payout_lines + PaySwift ledger
                                                 │ 6 authorize: auto-pay | ops approval | escalate   (pure Python)
                                                 │ 7 PaymentService → PaySwift (idempotency key, retries)
                                                 │ 8 reply from templates (en / hinglish / hi)
                                                 └─ every step → trace_steps (GET /trace/{rider})
 Ops console (/) ──► /ops/* (token) ──► OpsService: approve (re-verify, then pay) / reject / resolve
 PaymentWorker (thread) finishes payments that did not complete inside the 8 s webhook budget
 Postgres: riders, trips, payout_lines, conversations, messages, disputes,
           payment_transactions, payment_attempts, ops_items, trace_steps, audit_logs
```

| File | What it does |
|---|---|
| `app/agents/agent.py` | Orchestrates one message: memory, routing, claims → disputes, status replies, pushback, escalations |
| `app/agents/nlu_rules.py` | Deterministic parser for English, Hinglish and Devanagari Hindi (dates like "20 wala" / "kal", trip IDs, categories, flags) |
| `app/agents/llm.py`, `interpreter.py`, `prompts.py` | LLM client (Groq/OpenAI/Gemini/Anthropic over HTTP) and the guardrails that merge its output with the rule parser |
| `app/services/payout_engine.py` | The policy in Decimal arithmetic |
| `app/services/reconciliation.py` | Day ledger (expected vs paid per item), claim scoping, the "never more than owed" cap |
| `app/services/authorization.py` | Finance's rules, as a pure function |
| `app/services/payments.py` | Transactions, idempotency, retry/reconcile loop, worker, the PaySwift-based "desk view" |
| `app/services/ops.py` | Approve / reject / resolve, plus the dashboard read models |
| `app/services/seed.py` | CSV import with the data-quality fixes |
| `evals/run.py` | Implementation-agnostic evaluator (stdlib only) |

## 3. Walkthrough: "20 ko surge nahi mila aur 21 ko penalty do baar kata" (R027, received 23 Sep)
1. The message ID is new, so it is stored with status `processing`.
2. The parser splits on "aur" and produces two claims: (surge, 20 Sep) and (penalty, 21 Sep).
3. `payswift.list_payouts`: the desk has paid R027 nothing so far.
4. **20 Sep.** The day ledger finds T604546: 10 km at 1.2x → (25 + 8×6) × 1.2 = 87.6 → **₹88**. Payout lines show ₹73, which is exactly the fare without surge, so the finding is `surge_missing` and ₹15 is owed. Authorization: ₹15 ≤ ₹200 and no auto-payment yet today → **auto-pay**. The service creates transaction `qd-<dispute>-auto` and POSTs it to PaySwift → 201.
5. **21 Sep.** T936237 was cancelled by the rider, so ₹10 should have been deducted, but there are two penalty lines (−₹20). That is `penalty_duplicate`, so ₹10 is owed. Authorization: one auto-payment has already gone out today → **ops approval** (`daily_auto_limit`).
6. The reply states both facts, both amounts and why ₹10 waits for ops.
7. "10 wala bhi abhi de do na" is read as a payment request referencing ₹10, not as "10 Sep". The reply gives the live status from PaySwift and explains that approval comes first. No new money moves.

## 4. Why money is safe (the questions interviewers ask)
- **Duplicate webhooks.** `messages.external_id` is UNIQUE. A re-delivery returns the stored reply. If the first delivery is still running, the duplicate waits for it (bounded) instead of processing again.
- **Concurrent messages from one rider.** A Postgres advisory lock per rider makes the "1 auto-pay per day" check and the payment one atomic step.
- **Duplicate payouts.** The idempotency key is deterministic per dispute and mode, and UNIQUE in our DB. Every retry re-sends the same key, so PaySwift replays the original payout. Before retrying after an *unknown* outcome (timeout, 504, 409 in-progress) we read PaySwift and look for our transaction ID in the payout reference.
- **"We reconcile against PaySwift, not against your database."** Each payout reference encodes the transaction, the dispute, the mode, the business day and the items paid (`trip:T926334=25`). "Already paid", "repeat dispute" and "auto-payments today" are all computed from the PaySwift ledger. If our DB says a payout succeeded but PaySwift doesn't show it, we treat it as paid (never pay twice) and flag the anomaly. If PaySwift can't be read, nothing is auto-paid; it goes to ops.
- **Never more than owed.** We pay the verified shortfall of the items in scope, capped at the whole day's net shortfall, and minus anything already in flight or waiting for approval. When ops approves, the amount is re-verified against PaySwift first, so an approval can never pay twice.
- **PaySwift behaviour** (read from the sandbox): 15% 503s (retried), 10% 8-second responses, 5% lost responses (created, then 504), rate limit of more than 30 POSTs in 10 s blocks the account for 60 s (we back off 61 s), and outages. The eval includes a 12-second outage while paying, and the money still arrives exactly once.
- **Vendor 10-second timeout.** The webhook work has an 8 s budget. A slow payment is handed to the background worker and the rider is told it is being processed. A later "kab aayega?" checks PaySwift live.

## 5. What the model is and isn't allowed to do
The LLM turns text into a JSON interpretation (`app/schemas/interpretation.py`). It never sees policy amounts and never decides money. Guardrails: schema validation; trip and rider IDs must appear literally in the text; dates can't be in the future; injection, impersonation and accident flags are OR-ed with the rule parser; any failure falls back to the rule parser (recorded in the trace as `engine: rules, llm_error`). Replies are templates filled with computed numbers, so a model can never put a wrong amount in front of a rider. *Trade-off:* the replies are less varied than LLM-written ones, but they are auditable and exact. You could add an optional LLM "rephrase" step that is forbidden from changing numbers, verified by checking that every number survives.

## 6. Data problems found in the exports
- 64 trip rows used `R7` / `r19`. Without the fix, R007's T840677 looks like another rider's trip.
- 10 trip rows appear twice. R009 really did **11** trips on 17 Sep, not 12, so no incentive is due.
- 222 distances were in metres (R031/32/38/39/40). T312538 "5000 km" is really 5 km, so the expected fare is ₹65 and ₹22 is owed.
- `started_at` is UTC, but the policy day is IST. R024 has 12 trips in IST on 18 Sep (9 in UTC) → ₹150 due. R025 has 11 in IST on 19 Sep (13 in UTC) → nothing due.
- Overpayments exist too (R036 received an incentive on an 11-trip day). The day-net cap prevents paying out on top of them.

## 7. Evals
`evals/run.py` reads `data/conversations.json` and `evals/conversations_extra.json`. It packs conversations into rounds where each rider appears once and resets the system before each round (`docker compose down -v && up --wait` by default, or `--reset "<cmd>"`). It then sends each rider turn with the documented body and measures the conversation's effect:
- new PaySwift payouts, per rider (and checks that no other rider was paid);
- new `/ops/pending` items (approval amount, escalation required/no/optional);
- `one_of` alternatives, and `reply_mentions` (number-aware);
- `/trace` and `/ops/pending` shapes, HTTP 200, and latency under the vendor's 10 s.

It waits for background payouts to settle before checking. Expected values in my extra conversations were computed from the CSVs, not from the service. Without Docker, `--reset "bash scripts/local_reset.sh"` recreates the database and restarts local processes.

## 9. Ops console
Disputes with expected vs paid per item and the decision ([screenshot](screenshots/disputes.png)), payments with every PaySwift attempt next to the live ledger ([screenshot](screenshots/payments.png)), and the full agent trace ([screenshot](screenshots/trace.png)). Approve / reject / resolve buttons write to the audit trail with the operator's name.

## 8. Likely questions
- *Why not let the LLM call tools and decide?* Money needs determinism and an audit trail. The model is good at messy Hinglish and bad at arithmetic and policy. So the model is confined to interpretation, and both its output and the rule parser's go into the trace.
- *Why group by day?* Policy items (the incentive, the per-trip fares) live inside a day, Finance's "once per day" is per day, and the sample expectations match (2 trips on one day = one ₹176 auto-pay; two days = two disputes).
- *What if the rider is wrong about the reason?* If the day really is short we pay the real shortfall and say so (R001: "surge" was fine, but the penalty was charged twice).
- *Scaling to 5,000 riders?* Stateless app replicas: advisory locks and transaction leases already make multiple processes safe. Add indexes per rider and day (already there), a real queue for the payment worker, and per-user ops auth.
- *What would you do next?* Run against a live LLM with a labelled set of real messages, add an Alembic migration history, add outbound notifications when ops decides, give ops SSO, and add metrics (auto-resolution rate, time to resolution).
