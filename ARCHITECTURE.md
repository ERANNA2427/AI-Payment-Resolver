# ARCHITECTURE.md — AI-Payment-Resolver

**Buildathon:** Razorpay AI Buildathon — Track 03: AI Revenue Recovery
**Spec:** `PROJECT_SPEC.md`
**Status:** Architecture (no implementation)
**Guiding principle:** simplest reliable design. Money authority is **deterministic**; AI is **advisory-only** and structurally unable to move money.

---

## 1. Design Constraints

- **Runtime deps: zero beyond the standard library.** Python 3.14, stdlib only (`dataclasses`, `enum`, `json`, `hashlib`, `datetime`, `argparse`). Rationale: the venv is Python 3.14 with only pytest installed; avoiding third-party packages removes install/runtime risk during the Buildathon.
- **Money as integer minor units (paise)**, never floats. All arithmetic in integers.
- **Dry-run by default.** Money actions require an explicit `--execute` flag and hit a *simulated* gateway only.
- **Pure resolver.** State resolution has no I/O, no clock, no randomness — time and config are injected. This makes it exhaustively testable.
- **Smallest demonstrable product:** a CLI backend that runs a synthetic batch, prints metrics, and produces an audit trail.

---

## 2. Components

| Component | File (planned) | Responsibility |
|-----------|----------------|----------------|
| Event model | `backend/events.py` | `EventType`, `PaymentEvent`, `EventStream` (dedupe, stable sort, out-of-order flag, validate) |
| Domain model | `backend/models.py` | `Money`, `Order`, `ResolvedState`, `Intervention`, `DecisionRecord`, `ResolutionResult` enums/dataclasses |
| Rule layer | `backend/rules.py` | Ordered rule predicates `R01`–`R09` + `RULE_PRECEDENCE` table |
| Resolver | `backend/resolvers/payment_state_resolver.py` | Pure state-machine driver → `ResolvedState` + `rule_trace` |
| Orchestrator | `backend/resolver.py` | Wires ingest → resolve → risk → policy → safety → execute → audit |
| Risk/reason | `backend/risk.py` | Revenue-at-risk detection + root-cause reason |
| Policy | `backend/policy.py` | `ResolvedState` → bounded `Intervention` mapping |
| Safety gate | `backend/safety.py` | 12 invariants, `IdempotencyStore`, `CircuitBreaker` (veto power) |
| Execution | `backend/execution.py` | `SimulatedGateway`; dry-run vs `--execute` |
| Audit | `backend/audit.py` | Append-only JSONL writer + replay reader |
| AI layer | `backend/ai/advisor.py`, `stub_advisor.py`, `validator.py` | Advisory output + allowlist/confidence validation |
| Metrics | `backend/metrics.py` | Batch scoring, confusion matrix, economics |
| CLI | `backend/cli.py` | `run | report | replay` entrypoint |
| Tests | `backend/tests/` | Per-layer + end-to-end |
| Dataset | `data/scenarios.jsonl` | Labeled synthetic batch |

Only `ARCHITECTURE.md` is written in this step; file paths above are the planned layout from the spec.

---

## 3. Data Flow

```
data/scenarios.jsonl (orders + event streams, labeled)
        │
        ▼
   ┌──────────────┐
   │  INGEST      │  parse JSONL → per-order EventStream
   │  events.py   │
   └──────┬───────┘
          ▼
   ┌──────────────┐
   │  RESOLVE     │  pure: events → ResolvedState + rule_trace
   │  rules.py +  │  (contradiction/mismatch checked BEFORE happy path)
   │  resolver    │
   └──────┬───────┘
          ▼
   ┌──────────────┐      ┌──────────────────────┐
   │  RISK +      │◄─────│  AI ADVISORY (opt)    │
   │  POLICY      │      │  reason normalize,    │
   │  risk/policy │      │  copy draft, summary  │
   └──────┬───────┘      └──────────┬───────────┘
          │                         │ AdvisoryResult
          │                         │ (validated, allowlisted)
          ▼                         ▼
   ┌──────────────┐
   │  SAFETY GATE │  12 invariants. VETO → HUMAN_REVIEW
   │  safety.py   │
   └──────┬───────┘
          ▼
   ┌──────────────┐
   │  EXECUTE     │  SimulatedGateway; dry-run default
   │  execution   │
   └──────┬───────┘
          ▼
   ┌──────────────┐        ┌──────────────────────┐
   │  AUDIT       │──────► │  DecisionRecord JSONL │
   │  audit.py    │        │  (append-only)        │
   └──────────────┘        └──────────────────────┘
          ▼
   ┌──────────────┐
   │  METRICS     │  aggregate across batch → report
   │  metrics.py  │
   └──────────────┘
```

---

## 4. Resolver Pipeline (per order)

1. **Parse** each order's event stream from the dataset.
2. **Normalize** — build `EventStream`: dedupe by `event_id`, stable-sort by `occurred_at` (tie-break `sequence`), record `out_of_order` flag, drop invalid events (but keep evidence for audit).
3. **Resolve** — run `RULE_PRECEDENCE` (first-match-wins) to produce `ResolvedState` + `rule_trace`.
4. **Risk + reason** — `risk.py` sets `revenue_at_risk` flag and a canonical `risk_reason` (AI may normalize the human-readable error text here).
5. **Policy** — `policy.py` maps `ResolvedState` → exactly one `Intervention` (§8 of spec).
6. **Safety** — `safety.py` runs the 12 invariants. Any failure → `Intervention = ESCALATE_HUMAN_REVIEW`.
7. **Execute** — `execution.py` applies the action to `SimulatedGateway`; default dry-run.
8. **Audit** — `audit.py` appends one `DecisionRecord`.
9. **Aggregate** — `metrics.py` accumulates batch totals.

---

## 5. Deterministic Rules Layer

Implementing `PROJECT_SPEC.md` §6. Each rule is a pure predicate over `EventStream`. First match wins; the rest are marked `miss` in `rule_trace` for explainability.

| Order | Rule | Outcome |
|-------|------|---------|
| 1 | `R01_CONTRADICTION` | `HUMAN_REVIEW` |
| 2 | `R02_CURRENCY_MISMATCH` | `ORDER_PAYMENT_MISMATCH` |
| 3 | `R03_AMOUNT_MISMATCH` | `ORDER_PAYMENT_MISMATCH` |
| 4 | `R04_MULTI_SUCCESS` | `DUPLICATE_PAYMENT` |
| 5 | `R05_LATE_AUTH` | `LATE_AUTHORIZATION` |
| 6 | `R06_CAPTURED_OK` | `NORMAL_SUCCESS` |
| 7 | `R07_TERMINAL_FAILURE` | `NORMAL_FAILURE` |
| 8 | `R08_NON_TERMINAL` | `PENDING_PAYMENT` |
| 9 | `R09_FALLTHROUGH` | `HUMAN_REVIEW` |

The ordering is the safety design: a suspicious order can never silently reach `NORMAL_SUCCESS`.

---

## 6. AI Reasoning Layer

**Boundary:** AI is advisory only. It can read events and emit a structured `AdvisoryResult`; it **cannot** set `ResolvedState` or choose an `Intervention`.

Responsibilities (spec §7):
- **Reason normalization** — map gateway/support text to canonical failure-reason taxonomy.
- **Recovery copy drafting** — customer-facing retry/nudge text.
- **Human-review summarization** — concise case summary + suggested next step for escalated orders.

Interface (`backend/ai/advisor.py`):
```
AdvisoryResult = { kind, text, confidence, metadata }
```
- `stub_advisor.py` — deterministic offline implementation (keyword/regex mapping). **Default.** No API key, no network.
- A real LLM is an optional drop-in behind the same interface.
- `validator.py` — enforces allowlist + `confidence_floor`. Invalid/low-confidence → deterministic fallback; pipeline continues.

---

## 7. Recovery Action Layer

`policy.py` maps `ResolvedState` → exactly one `Intervention` (spec §8). Only two move money:

| Resolved State | Intervention | Money |
|----------------|--------------|-------|
| `NORMAL_SUCCESS` | `NO_ACTION` | — |
| `NORMAL_FAILURE` (retriable) | `SEND_RECOVERY_LINK` (max 1) | no |
| `NORMAL_FAILURE` (hard) | `NO_ACTION` | no |
| `PENDING_PAYMENT` | `RECONCILE_PENDING` → `SEND_PENDING_NUDGE` | no |
| `LATE_AUTHORIZATION` (in window) | `CAPTURE_LATE_AUTH` | **capture** |
| `LATE_AUTHORIZATION` (expired) | `VOID_LATE_AUTH` / `HUMAN_REVIEW` | void |
| `DUPLICATE_PAYMENT` | `REFUND_DUPLICATE` (later only, once) | **refund** |
| `ORDER_PAYMENT_MISMATCH` | `ESCALATE_HUMAN_REVIEW` | no |
| `HUMAN_REVIEW` | `ESCALATE_HUMAN_REVIEW` + AI summary | no |

`execution.py` performs the action via `SimulatedGateway`. No path performs a blind retry/re-charge.

---

## 8. Audit Trail

`audit.py` appends one JSONL `DecisionRecord` per decision (spec §10):
`decision_id, order_id, timestamp, resolved_state, rule_trace, risk_reason, intervention, safety_results, ai_advisory, idempotency_key, simulated, inputs_hash`.

- **Append-only**; never mutated.
- **Replayable** — re-reading the file reconstructs the full decision history and lets `replay` prove idempotency (zero new money actions on a second pass).
- Stored at a configurable path (default: `data/audit.jsonl` or `runs/<batch>/audit.jsonl`).

---

## 9. Human-Review Escalation

Triggers (any → `ESCALATE_HUMAN_REVIEW`, no money moved):
- `R01_CONTRADICTION` or `R09_FALLTHROUGH` (spec §6).
- `ORDER_PAYMENT_MISMATCH` (never auto-touch).
- Amount > `human_review_threshold` (safety invariant 8).
- Any safety-gate failure (veto).
- `LATE_AUTHORIZATION` past capture window when policy chooses escalate.

Each escalated order is emitted to the **human-review queue** with an AI-generated summary and its `rule_trace`, and counted in `human_review_queue` metrics.

---

## 10. Batch Evaluation

`data/scenarios.jsonl` holds ~50 labeled orders (event stream + `expected_state` + `expected_risk_reason`). The evaluator:
1. Runs the full pipeline over every order.
2. Compares `resolved_state` to `expected_state` (per-state precision/recall/F1, confusion matrix, accuracy).
3. Reports economics: `revenue_at_risk`, `revenue_recovered`, `unrecovered_revenue`, `exceptions`, `intervention_results`, `human_review_queue`.
4. Asserts invariants: accounting identity (`captured + refunded + at_risk = processed`), idempotent replay (0 new money actions), zero blind retries.

CLI: `python -m backend.cli run --batch data/scenarios.jsonl` and `python -m backend.cli replay` for the idempotency demo.

---

## 11. Failure Handling

- **Unrecognized events** — excluded from resolution, recorded as evidence; if no rule matches → `R09_FALLTHROUGH` → `HUMAN_REVIEW`.
- **Contradictory evidence** — `HUMAN_REVIEW`, never guessed.
- **Safety-gate failure** — action vetoed → `HUMAN_REVIEW`, recorded.
- **AI unavailable / low confidence** — deterministic fallback; pipeline continues.
- **Gateway (simulated) error** — action marked failed, counted as exception, order stays at risk; never retried blindly.
- **Circuit breaker** — if batch exception rate exceeds threshold, halt further money actions, finish in reporting mode.

---

## 12. Testing Strategy

Layered, pytest-based (venv already provides pytest 9.1.1). Money as integers keeps assertions exact.

- **Unit — rules (`test_rules.py`)**: each `R01`–`R09` in isolation, including precedence (contradiction beats happy path).
- **Unit — resolver (`test_payment_state_resolver.py`, existing file)**: all 7 states + out-of-order reconstruction.
- **Unit — policy (`test_policy.py`)**: mapping completeness for all 9 states.
- **Unit — safety (`test_safety.py`)**: idempotency, amount/capture bounds, currency veto, high-value ceiling, circuit breaker, no-blind-retry guarantee.
- **Unit — AI validator (`test_ai_validator.py`)**: malicious/invalid/low-confidence advisory rejected → fallback.
- **Unit — metrics (`test_metrics.py`)**: accounting identity holds.
- **Integration — batch (`test_batch_evaluation.py`)**: end-to-end over `data/scenarios.jsonl`; asserts ≥95% F1 on non-adversarial classes and that adversarial classes land in `HUMAN_REVIEW`.
- **Replay test**: running the batch twice yields zero additional money actions.

---

## 13. Trade-offs / Why This Is Enough

- **Stdlib-only** avoids dependency/version risk on Python 3.14 and keeps the demo portable.
- **Simulated gateway** satisfies "execute or safely simulate" without live-money danger or external API coupling.
- **Pure resolver + injected clock/config** makes the hardest, most valuable logic fully deterministic and testable in isolation.
- **AI as a validated, bounded advisory** adds genuine value (reason normalization, copy, summaries) without becoming an unauditable black box making money decisions.
- **Small surface for money movement** (only `CAPTURE_LATE_AUTH`, `REFUND_DUPLICATE`) keeps the safety argument short and defensible for a 5-minute pitch.
