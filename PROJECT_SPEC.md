# PROJECT_SPEC.md — AI-Payment-Resolver

**Buildathon:** Razorpay AI Buildathon — Track 03: AI Revenue Recovery
**Project:** AI-Payment-Resolver
**Status:** Specification (no implementation)
**Stack constraint:** Python 3.14, stdlib-only at runtime, pytest for tests, simulated gateway (no live API calls).

---

## 1. Problem Statement

Businesses lose recoverable revenue not because payments *fail*, but because the **true state of a payment is ambiguous** at the moment a decision must be made. Payment and checkout systems emit event streams — order created, payment authorized, captured, failed, pending, refunded — that arrive **out of order, duplicated, or contradictory**. Operating on a guessed state is dangerous: re-charging a customer who already paid, double-capturing, or double-refunding causes direct financial loss and compliance exposure.

Today this is handled manually by ops teams after the money is already gone. AI-Payment-Resolver closes that gap by **deterministically resolving payment state from raw events, detecting revenue at risk, selecting a bounded recovery action, and executing or safely simulating it within hard guardrails — with a full audit trail.**

The core thesis: **revenue recovery must be earned by correct state resolution first; money actions are a small, strictly-bounded consequence of that resolution, never a blind retry.**

---

## 2. Target Users

- **Primary — Revenue Operations / Payments Ops teams** at mid-market and enterprise merchants who manage failed, pending, and ambiguous payments across thousands of daily orders.
- **Secondary — Engineering / Platform teams** who need a deterministic, auditable recovery layer they can trust in front of real money movement.
- **Tertiary — Finance / Compliance reviewers** who consume the audit trail and human-review queue.

The product is a **backend decision engine + batch evaluator**, demonstrated via CLI. A web/API layer is explicitly out of core scope.

---

## 3. Core Workflow

For each order, the engine runs an ordered, deterministic pipeline:

1. **Ingest** — accept a stream of payment/checkout events for one order.
2. **Normalize & order** — deduplicate by event id, stable-sort by occurrence time, flag out-of-order arrival.
3. **Resolve state** — apply the deterministic rule table (§6) to produce exactly one `ResolvedState` plus a `rule_trace`.
4. **Detect risk & reason** — classify whether revenue is at risk and the likely root cause.
5. **Decide intervention** — map `ResolvedState` to exactly one bounded `Intervention` (§8).
6. **Safety gate** — run the invariant checks (§9); any failure **vetoes** the action and downgrades to `HUMAN_REVIEW`.
7. **Execute or simulate** — perform the action against a simulated gateway in dry-run by default; `--execute` required for any money movement.
8. **Record** — append one immutable `DecisionRecord` to the audit trail.
9. **Aggregate** — accumulate metrics across the batch (§11).

State resolution (steps 2–3) is a **pure function** with no I/O, no clock, no randomness — all time/config injected — so it is exhaustively testable.

---

## 4. Payment / Recovery Scenarios

The engine must correctly handle the following scenario classes (each is a labeled case in the synthetic batch, §12):

| ID | Scenario | Trigger events | Resolution |
|----|----------|----------------|------------|
| S1 | Clean success | `payment.captured` (amount/currency agree) | `NORMAL_SUCCESS` |
| S2 | Clean terminal failure | `payment.failed` (insufficient funds / issuer declined) | `NORMAL_FAILURE` |
| S3 | Abandoned checkout | `checkout.abandoned`, no payment | `NORMAL_FAILURE` (unrecoverable) |
| S4 | Pending / in-flight | `payment.pending` or `payment.authorized` in-window, no capture | `PENDING_PAYMENT` |
| S5 | Late authorization | `payment.authorized` after decision window, uncaptured | `LATE_AUTHORIZATION` |
| S6 | Duplicate payment | ≥2 distinct `payment.captured` for one order | `DUPLICATE_PAYMENT` |
| S7 | Amount mismatch | captured amount ≠ order amount | `ORDER_PAYMENT_MISMATCH` |
| S8 | Currency mismatch | payment currency ≠ order currency | `ORDER_PAYMENT_MISMATCH` |
| S9 | Contradiction | same payment both `captured` and `failed`; or `order.paid` with no success; or refund with no capture | `HUMAN_REVIEW` |
| S10 | Out-of-order arrival | events delivered late/early (e.g., capture seen before authorize) | resolved correctly despite order |
| S11 | High-value | any scenario above with amount > review threshold | `HUMAN_REVIEW` |
| S12 | Adversarial/garbage | malformed, unrecognized, or conflicting events | `HUMAN_REVIEW` (safe fallthrough) |

---

## 5. Payment State Model (ResolvedState)

Seven terminal classifications. Exactly one is produced per order:

- `NORMAL_SUCCESS` — captured, amount + currency agree.
- `NORMAL_FAILURE` — clean terminal failure / abandonment; no recovery by money action.
- `PENDING_PAYMENT` — non-terminal; outcome not yet determined.
- `LATE_AUTHORIZATION` — authorized after the decision window, still uncaptured.
- `DUPLICATE_PAYMENT` — more than one successful payment for one order (refund liability).
- `ORDER_PAYMENT_MISMATCH` — amount or currency disagreement.
- `HUMAN_REVIEW` — contradictory, high-value, or unmatched evidence.

---

## 6. Deterministic Rules

Resolution is **first-match-wins** against this ordered precedence. Contradiction and mismatch checks run **before** the happy path so a suspicious order can never fall through into `NORMAL_SUCCESS`.

| # | Rule ID | Condition | Resolved State |
|---|---------|-----------|----------------|
| 1 | `R01_CONTRADICTION` | Same payment both captured & failed; refund with no capture; `order.paid` with no success | `HUMAN_REVIEW` |
| 2 | `R02_CURRENCY_MISMATCH` | Payment currency ≠ order currency | `ORDER_PAYMENT_MISMATCH` |
| 3 | `R03_AMOUNT_MISMATCH` | Successful amount ≠ order amount (beyond zero tolerance) | `ORDER_PAYMENT_MISMATCH` |
| 4 | `R04_MULTI_SUCCESS` | ≥2 distinct successful payments on one order | `DUPLICATE_PAYMENT` |
| 5 | `R05_LATE_AUTH` | `authorized` after `late_auth_window`; uncaptured | `LATE_AUTHORIZATION` |
| 6 | `R06_CAPTURED_OK` | Exactly one capture, amount + currency agree | `NORMAL_SUCCESS` |
| 7 | `R07_TERMINAL_FAILURE` | Only terminal failure/abandonment, no success | `NORMAL_FAILURE` |
| 8 | `R08_NON_TERMINAL` | Latest state non-terminal (initiated/pending/authorized in-window) | `PENDING_PAYMENT` |
| 9 | `R09_FALLTHROUGH` | No rule matched (unrecognized/garbage) | `HUMAN_REVIEW` |

Each matched rule is recorded in the `rule_trace` with a `hit`/`miss` marker for explainability.

---

## 7. AI Responsibilities (Advisory Only)

AI is used **only where it adds defensible value**, and is **structurally incapable of moving money**.

- **Error-reason normalization** — map messy gateway or support text (e.g., "bank timeout", "network error", "insufficient balance") into the canonical failure-reason taxonomy.
- **Recovery copy drafting** — generate customer-facing retry / nudge message text.
- **Human-review case summarization** — produce a concise summary + recommended next step for each escalated case.

Guardrails:
- AI output passes through a **validator** that restricts it to an allowlisted enum/format and enforces a **confidence floor**.
- Invalid, out-of-allowlist, or low-confidence output **falls back to deterministic defaults**.
- AI never selects an `Intervention` and never sets `ResolvedState`; those are always deterministic.

Default provider is an **offline deterministic stub** (no API key, no network) so the demo runs anywhere. A real LLM is an optional swap-in behind the same interface.

---

## 8. Bounded Actions (Intervention)

Exactly one intervention is selected per order. **Only two interventions may move money, and both pass the full safety gate (§9).**

| Resolved State | Intervention | Moves money? |
|----------------|--------------|--------------|
| `NORMAL_SUCCESS` | `NO_ACTION` | No |
| `NORMAL_FAILURE` (retriable) | `SEND_RECOVERY_LINK` — customer-initiated retry, max 1 | No |
| `NORMAL_FAILURE` (hard/fraud) | `NO_ACTION` (mark unrecoverable) | No |
| `PENDING_PAYMENT` | `RECONCILE_PENDING` → `SEND_PENDING_NUDGE` | No |
| `LATE_AUTHORIZATION` (in window, agree) | `CAPTURE_LATE_AUTH` | **Yes — capture** |
| `LATE_AUTHORIZATION` (expired/cancelled) | `VOID_LATE_AUTH` or `HUMAN_REVIEW` | Void only |
| `DUPLICATE_PAYMENT` | `REFUND_DUPLICATE` — refunds the *later* duplicate, once | **Yes — refund** |
| `ORDER_PAYMENT_MISMATCH` | `ESCALATE_HUMAN_REVIEW` (**never auto-move**) | No |
| `HUMAN_REVIEW` | `ESCALATE_HUMAN_REVIEW` + AI summary | No |

**There is no blind retry path:** no code path silently re-charges a customer. Retries are customer-initiated via a link, capped at one.

---

## 9. Stopping / Escalation Rules (Safety Gate)

The safety gate has **veto power** over the policy decision. Any failure downgrades the action to `HUMAN_REVIEW`.

1. **Idempotency** — key = `hash(order_id + intervention + target_payment_id)`; replay returns the cached result, never re-executes.
2. **One money action per order per run** — ledger-enforced counter.
3. **Refund ≤ captured amount.**
4. **Capture ≤ authorized amount and ≤ order amount.**
5. **Exact currency match** — any mismatch escalates; no FX inference.
6. **No blind retry** — zero auto re-charge paths exist; max 1 customer nudge.
7. **Capture window** — late auth beyond window escalates, never auto-captures.
8. **High-value ceiling** — amount > `human_review_threshold` → `HUMAN_REVIEW` regardless of state.
9. **Dry-run default** — `--execute` required for any money movement.
10. **AI confidence floor** — below threshold/invalid → deterministic fallback.
11. **Circuit breaker** — batch exception rate > threshold halts all further money actions.
12. **Immutable audit** — every decision recorded with rule trace, input hash, and safety results.

---

## 10. Audit Trail Requirements

One append-only `DecisionRecord` per decision (JSONL). Fields:

- `decision_id`, `order_id`, `timestamp`
- `resolved_state`, `rule_trace` (ordered hit/miss)
- `risk_reason`, `intervention`
- `safety_results` (per-invariant pass/fail)
- `ai_advisory` (provider, output, confidence) or `"none"`
- `idempotency_key`, `simulated` (bool)
- `inputs_hash` (sha256 of normalized events)

Requirements: append-only, never mutated; replayable to reconstruct the full decision history; suitable for compliance review.

---

## 11. Evaluation Metrics

**Classification quality (vs. labeled batch):**
- Per-state precision / recall / F1
- Confusion matrix across the 7 `ResolvedState` values
- Overall accuracy

**Economic / recovery metrics (the Buildathon story):**
- `revenue_at_risk` — sum of order value in non-success states
- `revenue_recovered` — value of successful recoveries (`CAPTURE_LATE_AUTH` success, duplicate refunded without loss)
- `unrecovered_revenue` — value still at risk after the run
- `exceptions` — count of safety-gate vetoes / errors
- `intervention_results` — counts per `Intervention` and success/failure
- `human_review_queue` — count and total value escalated

**Invariants to assert in tests:**
- The money-movement accounting identity holds: captured + refunded + at_risk = total order value processed.
- Idempotency: replaying the batch produces zero new money actions.
- Zero blind retries: no test path triggers an auto re-charge.

---

## 12. Synthetic Batch Evaluation

A labeled dataset `data/scenarios.jsonl` contains ~50 synthetic orders, each with:
- a realistic, sometimes out-of-order/duplicated event stream,
- ground-truth `expected_state` and `expected_risk_reason`.

The evaluator runs the full pipeline over the batch, compares outputs to labels, and emits the metrics in §11 plus a per-case report. Adversarial cases (S9–S12) are included to prove safe escalation. This batch is both the **demo** and the **regression suite**.

---

## 13. Failure Handling

- **Unrecognized events** → ignored for resolution but surfaced in the record; if they cause no match → `R09_FALLTHROUGH` → `HUMAN_REVIEW`.
- **Contradictory evidence** → `HUMAN_REVIEW` (never guessed).
- **Safety-gate failure** → action vetoed → `HUMAN_REVIEW`, recorded.
- **AI unavailable / low confidence** → deterministic fallback; pipeline continues.
- **Gateway (simulated) error** → action marked failed, counted as exception, order remains at risk; never retried blindly.
- **Batch circuit breaker** → halts further money actions, finishes in reporting mode.

---

## 14. Acceptance Criteria

The project is complete when:

1. Every scenario S1–S12 resolves to the correct `ResolvedState` on the labeled batch (target ≥ 95% per-state F1 on non-adversarial classes; adversarial classes must land in `HUMAN_REVIEW`).
2. The full pipeline runs end-to-end on `data/scenarios.jsonl` and emits the §11 metrics + audit trail.
3. All 12 safety invariants are covered by tests, including idempotency replay (zero duplicate money actions) and the no-blind-retry guarantee.
4. `ORDER_PAYMENT_MISMATCH` and `HUMAN_REVIEW` never trigger any money movement.
5. The audit trail is append-only and replayable.
6. Default run is dry-run (no `--execute` ⇒ no money actions); AI stub works offline with no network/keys.
7. The 5-minute demo can show: ambiguous order → resolved state + reason → bounded action → simulated execution → metrics, plus the escalated queue with AI summaries, plus idempotent replay.
8. The repo layout matches the agreed architecture (no application logic in spec/doc files; all Python in `backend/`).

---

## 15. Out of Scope (Core)

- Live Razorpay API integration (gateway is simulated).
- Web/API server (CLI-only for the demo; an API is an optional stretch).
- Real customer notification delivery (links/copy are generated, not sent).
- Fraud detection modeling beyond the bounded mismatch/threshold rules.
