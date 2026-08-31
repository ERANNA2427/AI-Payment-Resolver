# 5-Minute Demo Runbook

## Objective

Demonstrate a complete revenue-recovery loop in under five minutes:

**ambiguous payment → resolved state → bounded intervention → safety decision → simulated execution → metrics → human review → idempotent replay**

## Before recording

Run:

```powershell
python -m pytest backend/tests/ -q
```

Then:

```powershell
python -m backend.cli run `
  --batch data/scenarios.jsonl `
  --audit runs/demo/final-audit.jsonl

python -m backend.cli report `
  --audit runs/demo/final-audit.jsonl

python -m backend.cli replay `
  --audit runs/demo/final-audit.jsonl
```

Keep the terminal output ready.

## 0:00–0:35 — Hook

Say:

> "A payment system doesn't fail cleanly. A webhook can arrive late, twice, with the wrong amount, or contradict another event. The dangerous part is not detecting the failure — it's deciding what to do next without making the financial situation worse."

Then:

> "I built AI Payment Resolver: an AI-assisted revenue recovery system where AI can explain, but it cannot authorize money movement."

## 0:35–1:20 — Problem and insight

Show the architecture diagram.

Say:

> "The core design is a separation of authority. The deterministic resolver decides the payment state. Policy maps that state to a bounded intervention. The safety gate has veto power. AI sits beside this path as an advisor."

Point to:

- resolver,
- AI advisor,
- validator,
- safety gate,
- execution boundary,
- audit trail.

## 1:20–2:15 — Ambiguous case

Show a synthetic scenario with:

- duplicate/late/contradictory payment evidence,
- the resolved state,
- the reason,
- the bounded intervention.

Say:

> "Notice that the AI is not deciding the action. The action is already constrained by deterministic policy."

For a risky case:

> "When the evidence violates a safety invariant, the system fails closed and escalates to human review."

## 2:15–3:10 — Safety

Show `backend/safety.py` or the safety section of the architecture.

Say:

> "This is the part I would trust with payments. We test twelve safety invariants. Amount and currency bounds, late authorization windows, duplicate handling, idempotency, recovery limits, dry-run enforcement, and other execution constraints."

Then emphasize:

> "If a check fails, the system does not try to be clever. It vetoes the action."

## 3:10–4:05 — Batch evidence

Run/show the batch metrics.

Say:

> "This is not a cherry-picked single example. The dataset contains 50 synthetic orders across twelve scenario families."

Show:

- total value,
- captured,
- refunded,
- revenue at risk,
- exceptions,
- human-review count,
- accounting identity.

Then:

> "The accounting identity holds: captured plus refunded plus revenue at risk equals total value."

## 4:05–4:35 — AI boundary

Show `docs/AI_SAFETY.md`.

Say:

> "AI is useful for messy language, explanations, recovery copy and human-review summaries. But the validator enforces an allowlist and confidence floor, and invalid output falls back deterministically."

## 4:35–5:00 — Replay / closing

Run:

```powershell
python -m backend.cli replay `
  --audit runs/demo/final-audit.jsonl
```

Say:

> "Finally, I replay the same audit trail. Fifty records are blocked and zero new actions are created."

Close with:

> "The goal isn't to make AI control payments. The goal is to make recovery smarter while making financial authority harder to misuse."
