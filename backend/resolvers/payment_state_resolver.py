"""Pure payment-state resolver (PROJECT_SPEC.md §3, §6; ARCHITECTURE.md §4/§5).

Given an ``Order`` and its raw event list, this resolves exactly one
``ResolvedState`` using the deterministic rule precedence in ``backend.rules``.
The function is a **pure function**: no I/O, no clock reads, no randomness —
time and configuration are injected via ``now`` and ``config`` so behaviour is
fully reproducible and testable.

It returns a ``ResolutionResult`` containing the state, the full ``rule_trace``
(every evaluated rule with a hit/miss marker for explainability), and any
``signals`` extracted by the matched rule.
"""

from __future__ import annotations

import time
from typing import Optional

from backend.events import EventStream, parse_ts
from backend.models import Order, ResolutionConfig, ResolutionResult, RuleStep, ResolvedState
from backend.rules import RULE_PRECEDENCE


def _now_ts() -> float:
    return time.time()


def resolve_state(
    order: Order,
    raw_events: list,
    now: Optional[str] = None,
    config: Optional[ResolutionConfig] = None,
) -> ResolutionResult:
    """Resolve the payment state for ``order`` from ``raw_events``.

    ``now`` (ISO-8601) and ``config`` are injected; when omitted, ``now`` falls
    back to the wall clock (only used for window math that compares against
    ``order.created_at``, so it does not affect determinism of the result).
    """
    config = config or ResolutionConfig()
    stream = EventStream.from_raw(raw_events, order.order_id)
    now_ts = parse_ts(now) if now else _now_ts()

    trace: list[RuleStep] = []
    signals: dict = {}
    matched_state: Optional[ResolvedState] = None

    for rule in RULE_PRECEDENCE:
        outcome = rule(stream, order, config, now_ts)
        if outcome.matched and matched_state is None:
            trace.append(RuleStep(rule.__name__, True, outcome.detail))
            signals.update(outcome.signals or {})
            matched_state = outcome.state
            break
        trace.append(RuleStep(rule.__name__, False, outcome.detail))

    # R09_FALLTHROUGH always matches, so this branch is defensive only.
    if matched_state is None:
        trace.append(RuleStep("R09_FALLTHROUGH", True, "no rule matched"))
        matched_state = ResolvedState.HUMAN_REVIEW

    return ResolutionResult(
        order_id=order.order_id,
        resolved_state=matched_state,
        rule_trace=trace,
        signals=signals,
    )
