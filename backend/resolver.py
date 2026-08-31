"""Orchestrator: turns a resolved payment state into an auditable decision.

P3 of the implementation plan (ARCHITECTURE.md §2/§4). This module wires the
pure resolver (backend.resolvers.payment_state_resolver) together with:

  * **Risk** — the canonical `risk_reason` and `revenue_at_risk` flag (spec §3.4, §11),
  * **Policy** — the deterministic `ResolvedState -> Intervention` mapping (spec §8),
  * **Audit** — a complete, audit-ready `DecisionRecord` (spec §10) with a stable
    `idempotency_key` and `inputs_hash`.

Design boundaries (kept deliberately narrow for P3):
  * No I/O: this module returns in-memory `DecisionRecord` objects. Writing to the
    audit trail (JSONL) and executing actions are later phases, so business logic
    stays separate from side effects.
  * No safety gating / execution / AI: those are P4+ and plug in around this record.
  * Fully deterministic given the same ``order``, ``raw_events``, ``now``, and
    ``config``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from backend.events import EventStream
from backend.models import (
    DecisionRecord,
    Intervention,
    Order,
    ResolutionConfig,
    ResolvedState,
)
from backend.resolvers.payment_state_resolver import resolve_state

# Policy: exactly one bounded Intervention per ResolvedState (spec §8).
STATE_INTERVENTION = {
    ResolvedState.NORMAL_SUCCESS: Intervention.NO_ACTION,
    ResolvedState.NORMAL_FAILURE: Intervention.SEND_RECOVERY_LINK,  # default: retriable
    ResolvedState.PENDING_PAYMENT: Intervention.RECONCILE_PENDING,
    ResolvedState.LATE_AUTHORIZATION: Intervention.CAPTURE_LATE_AUTH,
    ResolvedState.DUPLICATE_PAYMENT: Intervention.REFUND_DUPLICATE,
    ResolvedState.ORDER_PAYMENT_MISMATCH: Intervention.ESCALATE_HUMAN_REVIEW,
    ResolvedState.HUMAN_REVIEW: Intervention.ESCALATE_HUMAN_REVIEW,
}

# Canonical, human-readable risk reason per state (spec §3.4, §11).
RISK_REASON = {
    ResolvedState.NORMAL_SUCCESS: None,
    ResolvedState.NORMAL_FAILURE: "terminal payment failure",
    ResolvedState.PENDING_PAYMENT: "payment outcome not yet determined",
    ResolvedState.LATE_AUTHORIZATION: "payment authorized after decision window",
    ResolvedState.DUPLICATE_PAYMENT: "duplicate successful payment detected",
    ResolvedState.ORDER_PAYMENT_MISMATCH: "amount or currency mismatch",
    ResolvedState.HUMAN_REVIEW: "ambiguous or contradictory payment evidence",
}


def recommend_intervention(state: ResolvedState, raw_events: Optional[list] = None) -> Intervention:
    """Map a resolved state to its single bounded intervention (spec §8)."""
    if state is ResolvedState.NORMAL_FAILURE:
        events = raw_events or []
        if any(
            isinstance(e, dict) and e.get("event_type") == "checkout.abandoned"
            for e in events
        ):
            return Intervention.NO_ACTION
        return Intervention.SEND_RECOVERY_LINK
    return STATE_INTERVENTION[state]


def risk_reason_for(state: ResolvedState) -> Optional[str]:
    """Canonical root-cause reason for an escalated/at-risk state."""
    return RISK_REASON[state]


def _canonical_hash(raw_events) -> str:
    """Stable sha256 over the *normalized* event list (spec §10: inputs_hash).

    Events are sorted by ``event_id`` before serialization so the hash is
    independent of raw delivery order — the same logical input always yields the
    same hash.
    """
    normalized_list = sorted(
        raw_events,
        key=lambda e: (e.get("event_id", "") if isinstance(e, dict) else ""),
    )
    normalized = json.dumps(normalized_list, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _idempotency_key(order_id: str, state: ResolvedState, intervention: Intervention,
                      target_payment_id: Optional[str]) -> str:
    """Stable key = hash(order_id | state | intervention | target). Replaying the
    same inputs yields the same key, enabling idempotent execution later (spec §9.1)."""
    basis = f"{order_id}|{state.value}|{intervention.value}|{target_payment_id or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target_payment_id(signals: dict) -> Optional[str]:
    """Pick the payment a money-moving intervention would act on, if any."""
    if "successful_payment_ids" in signals:
        ids = signals["successful_payment_ids"]
        if ids:
            return ids[-1]  # for a duplicate, refund the later payment
    if "conflict_ids" in signals:
        ids = signals["conflict_ids"]
        if ids:
            return ids[0]
    if "payment_id" in signals:
        return signals["payment_id"]
    return None


def resolve_order(
    order: Order,
    raw_events: list,
    now: Optional[str] = None,
    config: Optional[ResolutionConfig] = None,
    simulated: bool = True,
) -> DecisionRecord:
    """Resolve one order end-to-end (resolve -> risk -> policy -> record).

    Returns an audit-ready ``DecisionRecord``; performs no I/O and no money action.
    """
    config = config or ResolutionConfig()
    resolution = resolve_state(order, raw_events, now=now, config=config)

    state = resolution.resolved_state
    intervention = recommend_intervention(state, raw_events)
    reason = risk_reason_for(state)
    revenue_at_risk = state != ResolvedState.NORMAL_SUCCESS
    target = _target_payment_id(resolution.signals)

    return DecisionRecord(
        decision_id=uuid.uuid4().hex,
        order_id=order.order_id,
        timestamp=now or _now_iso(),
        resolved_state=state,
        rule_trace=resolution.rule_trace,
        risk_reason=reason,
        intervention=intervention,
        idempotency_key=_idempotency_key(order.order_id, state, intervention, target),
        inputs_hash=_canonical_hash(raw_events),
        simulated=simulated,
        revenue_at_risk=revenue_at_risk,
        safety_results={},  # populated by the P4 safety gate
        ai_advisory=None,   # populated by the P6 AI layer
        signals=resolution.signals,
    )


def resolve_batch(items, now: Optional[str] = None, config: Optional[ResolutionConfig] = None,
                  simulated: bool = True) -> list[DecisionRecord]:
    """Resolve a batch of ``(Order, raw_events)`` pairs into DecisionRecords."""
    return [
        resolve_order(order, raw_events, now=now, config=config, simulated=simulated)
        for order, raw_events in items
    ]


def process_order(
    order: Order,
    raw_events: list,
    *,
    execute: bool = False,
    audit_path: Optional[str] = None,
    idempotency_store: Optional[object] = None,
    circuit_breaker: Optional[object] = None,
    now: Optional[str] = None,
    config: Optional[ResolutionConfig] = None,
    ai_advisory: Optional[dict] = None,
) -> DecisionRecord:
    """Full P4 pipeline: resolve -> safety -> execute -> audit.

    Adds the safety gate, optional execution, and optional audit persistence
    without changing the P3 ``resolve_order`` API.
    """
    from backend.safety import (
        CircuitBreaker,
        IdempotencyStore,
        evaluate_safety,
    )
    from backend.execution import execute_intervention, _MONEY_MOVING

    config = config or ResolutionConfig()
    store = idempotency_store or IdempotencyStore()
    breaker = circuit_breaker or CircuitBreaker()

    record = resolve_order(order, raw_events, now=now, config=config, simulated=not execute)

    from backend.events import EventStream
    stream = EventStream.from_raw(raw_events, order_id=order.order_id)

    target = _target_payment_id(record.signals)

    resolved_state, intervention, safety_results = evaluate_safety(
        order=order,
        stream=stream,
        resolved_state=record.resolved_state,
        intervention=record.intervention,
        target_payment_id=target,
        idempotency_key=record.idempotency_key,
        idempotency_store=store,
        order_money_actions=process_order._order_money_actions,
        order_recovery_links=process_order._order_recovery_links,
        circuit_breaker=breaker,
        execute=execute,
        config=config,
        ai_advisory=ai_advisory,
    )

    record.resolved_state = resolved_state
    record.intervention = intervention
    record.safety_results = safety_results
    record._stream = stream

    recovery = execute_intervention(record, execute=execute, order=order)

    if breaker is not None:
        is_money = record.intervention in _MONEY_MOVING
        is_success = recovery.status in ("executed", "simulated")
        if is_money:
            breaker.record(is_success)

    if audit_path:
        from backend.audit import append_record
        append_record(audit_path, record)

    return record


process_order._order_money_actions: dict[str, int] = {}
process_order._order_recovery_links: dict[str, int] = {}
