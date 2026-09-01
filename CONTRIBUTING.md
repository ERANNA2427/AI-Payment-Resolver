# Contributing to AI Payment Resolver

Thank you for your interest in contributing. This repository is a buildathon demonstration of a deterministic-first AI payment resolution engine.

## Scope

This project is intentionally constrained:

- **Stdlib-only runtime.** No third-party runtime dependencies.
- **Deterministic authority.** The resolver, policy, and safety layers must remain deterministic and testable.
- **Synthetic data only.** Do not add real customer, payment, or credential data.

## How to contribute

1. Fork the repository and create a feature branch.
2. Add or update tests for any logic change. The suite must stay green.
3. Run the full test suite before opening a PR:
   ```powershell
   python -m pytest backend/tests/ -q
   ```
4. Verify the demo still produces consistent output:
   ```powershell
   python -m backend.cli run --batch data/scenarios.jsonl --audit runs/demo/audit.jsonl
   python -m backend.cli report --audit runs/demo/audit.jsonl
   python -m backend.cli replay --audit runs/demo/audit.jsonl
   ```

## Things that should not change

These are load-bearing safety and correctness constraints, not preferences:

- Do not change the deterministic resolver logic.
- Do not change safety invariant behavior.
- Do not allow AI to influence state or intervention selection.
- Do not change integer money representation.
- Do not weaken the dry-run default.
- Do not modify test assertions to make failures disappear.
- Do not add real API calls or network dependencies.
