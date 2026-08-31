# Security and Safety Notes

## Scope

This repository is a synthetic buildathon demonstration.

It does not connect to production payment infrastructure and does not process real customer payment data.

## Execution

- Dry-run is the default.
- The execution layer uses a simulated gateway.
- `--execute` is required to enter the simulated execution path.
- No production credentials are required.

## Financial safety

The system is designed around:

- deterministic state resolution,
- bounded interventions,
- fail-closed safety checks,
- idempotency,
- append-only audit records,
- integer minor-unit money representation.

## Data

`data/scenarios.jsonl` contains synthetic scenarios only.

Do not add:

- card numbers,
- CVVs,
- authentication secrets,
- production API keys,
- real customer identifiers,
- production payment payloads.

## Reporting a security issue

For the buildathon repository, please avoid posting sensitive details in a public issue. Contact the repository owner privately with a reproducible description.
