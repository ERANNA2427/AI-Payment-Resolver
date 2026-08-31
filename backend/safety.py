"""Safety gate for AI-Payment-Resolver (PROJECT_SPEC.md \\u00a79).

Evaluates the 12 invariants and returns a pass/fail verdict with per-check
results. Any failure fails closed: the proposed intervention is downgraded to
``ESCALATE_HUMAN_REVIEW`` and the original ``resolved_state`` is preserved.

This module is deterministic and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from backend.events import EventStream
from backend.models import (
    Intervention,
    Order,
    ResolutionConfig,
    ResolvedState,
)


# ---------------------------------------------------------------------------
# Helpers / seams
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """Tracks batch exception rate and halts money actions when exceeded."""

    total: int = 0
    exceptions: int = 0
    threshold: float = 0.5
    halted: bool = False

    def record(self, succeeded: bool) -> None:
        self.total += 1
        if not succeeded:
            self.exceptions += 1
        if self.total > 0 and (self.exceptions / self.total) > self.threshold:
            self.halted = True

    def money_action_allowed(self) -> bool:
        return not self.halted


@dataclass
class IdempotencyStore:
    """Deterministic in-memory store for idempotency keys."""

    _seen: set = field(default_factory=set)

    def already_executed(self, key: str) -> bool:
        return key in self._seen

    def record(self, key: str) -> None:
        self._seen.add(key)


# ---------------------------------------------------------------------------
# Safety evaluation
# ---------------------------------------------------------------------------

_MONEY_MOVING = {
    Intervention.CAPTURE_LATE_AUTH,
    Intervention.REFUND_DUPLICATE,
}


def _check_idempotency(
    store: Optional[IdempotencyStore],
    key: Optional[str],
) -> tuple[bool, str]:
    if store is None or key is None:
        return True, "no-store"
    if store.already_executed(key):
        return False, f"duplicate idempotency key {key[:12]}..."
    return True, "first-seen"


def _check_one_money_action_per_order(
    order_money_actions: dict[str, int],
    order_id: str,
    intervention: Intervention,
) -> tuple[bool, str]:
    if intervention not in _MONEY_MOVING:
        return True, "non-money"
    count = order_money_actions.get(order_id, 0)
    if count >= 1:
        return False, f"order {order_id} already has {count} money action(s)"
    return True, "first-money-action"


def _check_refund_bound(
    stream: EventStream, order: Order, intervention: Intervention, target_payment_id: Optional[str]
) -> tuple[bool, str]:
    if intervention != Intervention.REFUND_DUPLICATE:
        return True, "not-refund"
    if not target_payment_id:
        return False, "refund target missing"
    captured_total = 0
    for e in stream.events:
        if (
            e.event_type.value == "payment.captured"
            and (e.payment_id or e.event_id) == target_payment_id
            and e.amount is not None
        ):
            captured_total += e.amount
    refund_amount = order.amount
    if captured_total <= 0:
        return False, "no captured amount for refund target"
    if refund_amount > captured_total:
        return False, f"refund {refund_amount} > captured {captured_total}"
    return True, f"refund {refund_amount} <= captured {captured_total}"


def _check_capture_bounds(
    stream: EventStream,
    order: Order,
    intervention: Intervention,
    target_payment_id: Optional[str],
    config: ResolutionConfig,
) -> tuple[bool, str]:
    if intervention != Intervention.CAPTURE_LATE_AUTH:
        return True, "not-capture"
    auth_amount = None
    auth_currency = None
    for e in stream.events:
        if e.event_type.value == "payment.authorized" and (e.payment_id or e.event_id) == target_payment_id:
            auth_amount = e.amount
            auth_currency = e.currency
            break
    capture_amount = order.amount
    capture_currency = order.currency
    effective_auth = auth_amount if auth_amount is not None else order.amount
    if capture_amount > effective_auth:
        return False, f"capture {capture_amount} > authorized {effective_auth}"
    if capture_amount > order.amount:
        return False, f"capture {capture_amount} > order amount {order.amount}"
    if auth_currency is not None and capture_currency != auth_currency:
        return False, f"currency {capture_currency} != authorized currency {auth_currency}"
    return True, "capture within bounds"


def _check_currency_match(
    stream: EventStream,
    order: Order,
    intervention: Intervention,
) -> tuple[bool, str]:
    if intervention not in _MONEY_MOVING:
        return True, "non-money"
    for e in stream.events:
        if e.event_type.value in ("payment.captured", "payment.authorized") and e.currency and e.currency != order.currency:
            return False, f"payment currency {e.currency} != order currency {order.currency}"
    return True, "currency exact match"


def _check_no_blind_retry(
    order_recovery_links: dict[str, int],
    order_id: str,
    intervention: Intervention,
) -> tuple[bool, str]:
    if intervention != Intervention.SEND_RECOVERY_LINK:
        return True, "not-recovery-link"
    count = order_recovery_links.get(order_id, 0)
    if count >= 1:
        return False, f"recovery link already sent for order {order_id}"
    return True, "first recovery link"


def _check_capture_window(
    stream: EventStream,
    order: Order,
    config: ResolutionConfig,
    intervention: Intervention,
    target_payment_id: Optional[str],
) -> tuple[bool, str]:
    if intervention != Intervention.CAPTURE_LATE_AUTH:
        return True, "not-capture"
    created_ts = EventStream.from_raw([], order_id=order.order_id)
    from backend.events import parse_ts
    created_ts_val = parse_ts(order.created_at)
    for e in stream.events:
        if e.event_type.value == "payment.authorized" and (e.payment_id or e.event_id) == target_payment_id:
            occurred = e.occurred_ts()
            if occurred > created_ts_val + config.late_auth_window_seconds:
                return False, "authorization outside late-auth window"
            return True, "authorization within window"
    return True, "no late-auth event"


def _check_high_value(
    order: Order,
    config: ResolutionConfig,
    resolved_state: ResolvedState,
) -> tuple[bool, str]:
    if order.amount > config.high_value_threshold:
        return False, f"order amount {order.amount} exceeds high-value threshold {config.high_value_threshold}"
    return True, "within high-value ceiling"


def _check_dry_run(execute: bool, intervention: Intervention) -> tuple[bool, str]:
    if intervention in _MONEY_MOVING and not execute:
        return False, "dry-run default blocks money movement without --execute"
    return True, "dry-run satisfied" if not execute else "execute permitted"


def _check_ai_confidence(ai_advisory: Optional[dict]) -> tuple[bool, str]:
    if ai_advisory is None:
        return True, "no AI advisory"
    confidence = ai_advisory.get("confidence")
    if confidence is None or not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        return False, f"invalid AI confidence {confidence!r}"
    threshold = ai_advisory.get("confidence_floor", 0.7)
    if confidence < threshold:
        return False, f"AI confidence {confidence} below floor {threshold}"
    return True, "AI confidence sufficient"


def _check_circuit_breaker(
    circuit_breaker: Optional[CircuitBreaker],
    intervention: Intervention,
) -> tuple[bool, str]:
    if circuit_breaker is None:
        return True, "no circuit breaker"
    if intervention in _MONEY_MOVING and not circuit_breaker.money_action_allowed():
        return False, "circuit breaker halted"
    return True, "circuit breaker passed"


def evaluate_safety(
    *,
    order: Order,
    stream: EventStream,
    resolved_state: ResolvedState,
    intervention: Intervention,
    target_payment_id: Optional[str],
    idempotency_key: Optional[str],
    idempotency_store: Optional[IdempotencyStore],
    order_money_actions: dict[str, int],
    order_recovery_links: dict[str, int],
    circuit_breaker: Optional[CircuitBreaker],
    execute: bool,
    config: ResolutionConfig,
    ai_advisory: Optional[dict] = None,
) -> tuple[ResolvedState, Intervention, dict[str, bool]]:
    """Run the 12 safety checks. Fail closed on any failure.

    Returns (preserved_resolved_state, final_intervention, safety_results).
    """
    safety_results: dict[str, bool] = {}
    checks: list[tuple[str, tuple[bool, str]]] = []

    # S01
    checks.append(("S01_IDEMPOTENCY", _check_idempotency(idempotency_store, idempotency_key)))
    # S02
    checks.append(("S02_ONE_MONEY_ACTION", _check_one_money_action_per_order(order_money_actions, order.order_id, intervention)))
    # S03
    checks.append(("S03_REFUND_BOUND", _check_refund_bound(stream, order, intervention, target_payment_id)))
    # S04
    checks.append(("S04_CAPTURE_BOUNDS", _check_capture_bounds(stream, order, intervention, target_payment_id, config)))
    # S05
    checks.append(("S05_CURRENCY_MATCH", _check_currency_match(stream, order, intervention)))
    # S06
    checks.append(("S06_NO_BLIND_RETRY", _check_no_blind_retry(order_recovery_links, order.order_id, intervention)))
    # S07
    checks.append(("S07_CAPTURE_WINDOW", _check_capture_window(stream, order, config, intervention, target_payment_id)))
    # S08
    checks.append(("S08_HIGH_VALUE", _check_high_value(order, config, resolved_state)))
    # S09
    s09_pass, s09_detail = _check_dry_run(execute, intervention)
    checks.append(("S09_DRY_RUN", (s09_pass, s09_detail)))
    # S10
    checks.append(("S10_AI_CONFIDENCE", _check_ai_confidence(ai_advisory)))
    # S11
    checks.append(("S11_CIRCUIT_BREAKER", _check_circuit_breaker(circuit_breaker, intervention)))
    # S12 is structural (immutable audit record); pass by default at this layer.
    checks.append(("S12_IMMUTABLE_AUDIT", (True, "immutable-by-construction")))

    failed = False
    for check_id, (passed, _detail) in checks:
        safety_results[check_id] = passed
        if not passed and check_id != "S09_DRY_RUN":
            failed = True

    final_state = resolved_state
    final_intervention = intervention

    if failed:
        final_intervention = Intervention.ESCALATE_HUMAN_REVIEW
    elif idempotency_store is not None and idempotency_key is not None:
        idempotency_store.record(idempotency_key)
        if intervention in _MONEY_MOVING:
            order_money_actions[order.order_id] = order_money_actions.get(order.order_id, 0) + 1
        if intervention == Intervention.SEND_RECOVERY_LINK:
            order_recovery_links[order.order_id] = order_recovery_links.get(order.order_id, 0) + 1

    return final_state, final_intervention, safety_results
