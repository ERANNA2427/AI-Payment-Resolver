# AI + Safety Boundary

## Design objective

The system deliberately separates **intelligence** from **financial authority**.

The AI layer is advisory-only. It cannot select a payment state, choose a money-moving intervention, or call the execution gateway.

## AI responsibilities

`backend/ai/` supports three classes of assistance:

1. **Reason normalization** — turn noisy gateway errors into canonical categories.
2. **Recovery copy drafting** — produce bounded customer-facing recovery text.
3. **Human-review summarization** — explain why a case was escalated and suggest the next review step.

## AI output contract

```text
AdvisoryResult
├── kind
├── text
├── confidence
└── metadata
```

The validator enforces:

- allowed advisory kinds,
- allowed metadata,
- confidence floor,
- deterministic fallback on invalid output.

Default confidence floor: `0.7`.

## What AI cannot do

The AI layer cannot:

- choose `ResolvedState`,
- choose `Intervention`,
- bypass the safety gate,
- create an idempotency key,
- call the simulated gateway,
- authorize a refund/capture,
- override amount/currency constraints.

## Authority chain

```text
raw events
   │
   ▼
deterministic normalization
   │
   ▼
deterministic resolver
   │
   ├──────────────► AI advisory
   │                    │
   │                    ▼
   │              validator/fallback
   │                    │
   └──────────────┬─────┘
                  ▼
             safety gate
            /           \
         PASS            VETO
          │               │
          ▼               ▼
      execution       HUMAN_REVIEW
```

## Safety philosophy

The safety gate is fail-closed.

If any required invariant fails:

```text
money-moving intervention
          ↓
       vetoed
          ↓
 ESCALATE_HUMAN_REVIEW
```

The original resolved state can remain visible for diagnosis, while the intervention is prevented from executing.

## Why this is important for payments

A generative model is useful for interpreting messy text and communicating with humans.

It is not the correct authority for:

- monetary bounds,
- currency equality,
- duplicate-payment handling,
- idempotency,
- execution authorization,
- circuit-breaker limits.

Those are deterministic policy and safety concerns.

## Demo claim

The strongest claim is not "AI makes payments."

It is:

> **AI helps understand the case; deterministic controls decide what is safe to do.**

That is the boundary this repository demonstrates.
