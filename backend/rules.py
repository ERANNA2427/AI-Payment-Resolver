"""Deterministic payment-state resolution rules (PROJECT_SPEC.md §6).

Each rule is a pure predicate over a normalized ``EventStream``. The rules are
evaluated in the fixed precedence ``RULE_PRECEDENCE``; the **first** rule that
matches determines the single ``ResolvedState`` for the order. Contradiction and
mismatch checks run *before* the happy path so a suspicious order can never
silently become ``NORMAL_SUCCESS``.

This module is deterministic and side-effect free. It contains no recovery,
safety, or AI logic (those are later phases).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.events import EventStream, EventType, parse_ts
from backend.models import Order, ResolutionConfig, ResolvedState


@dataclass
class RuleOutcome:
    """Result of evaluating one rule against a stream."""

    matched: bool
    state: Optional[ResolvedState] = None
    detail: str = ""
    signals: dict = field(default_factory=dict)


# --- stream helpers (capture = a successful money capture only) ---------------

def _captured_payments(stream: EventStream):
    return [e for e in stream.events if e.event_type == EventType.PAYMENT_CAPTURED]


def _captured_ids(stream: EventStream) -> set:
    ids: set = set()
    for e in _captured_payments(stream):
        ids.add(e.payment_id or e.event_id)
    return ids


def _failed_ids(stream: EventStream) -> set:
    return {
        e.payment_id
        for e in stream.events
        if e.event_type == EventType.PAYMENT_FAILED and e.payment_id
    }


def _authorized(stream: EventStream):
    return [e for e in stream.events if e.event_type == EventType.PAYMENT_AUTHORIZED]


def _order_paid(stream: EventStream):
    return [e for e in stream.events if e.event_type == EventType.ORDER_PAID]


def _refunds(stream: EventStream):
    return [e for e in stream.events if e.event_type == EventType.REFUND_CREATED]


def _failures(stream: EventStream):
    return [
        e
        for e in stream.events
        if e.event_type in (EventType.PAYMENT_FAILED, EventType.CHECKOUT_ABANDONED)
    ]


def _pending_like(stream: EventStream):
    return [
        e
        for e in stream.events
        if e.event_type
        in (EventType.PAYMENT_INITIATED, EventType.PAYMENT_PENDING, EventType.PAYMENT_AUTHORIZED)
    ]


# --- rules -------------------------------------------------------------------


def R01_CONTRADICTION(stream, order, config, now_ts) -> RuleOutcome:
    """Same payment captured & failed; refund with no capture; order.paid with
    no success -> HUMAN_REVIEW."""
    captured = _captured_ids(stream)
    failed = _failed_ids(stream)
    conflict = sorted(captured & failed)
    if conflict:
        return RuleOutcome(
            True, ResolvedState.HUMAN_REVIEW,
            "same payment both captured and failed",
            {"conflict_ids": conflict},
        )
    if _order_paid(stream) and not _captured_payments(stream):
        return RuleOutcome(
            True, ResolvedState.HUMAN_REVIEW, "order.paid with no successful capture"
        )
    if _refunds(stream) and not _captured_payments(stream):
        return RuleOutcome(
            True, ResolvedState.HUMAN_REVIEW, "refund created with no prior capture"
        )
    return RuleOutcome(False)


def R02_CURRENCY_MISMATCH(stream, order, config, now_ts) -> RuleOutcome:
    """Captured payment currency != order currency -> ORDER_PAYMENT_MISMATCH."""
    for e in _captured_payments(stream):
        cur = e.currency or order.currency
        if cur != order.currency:
            return RuleOutcome(
                True, ResolvedState.ORDER_PAYMENT_MISMATCH,
                f"currency {cur} != order currency {order.currency}",
                {"payment_id": e.payment_id, "currency": cur},
            )
    return RuleOutcome(False)


def R03_AMOUNT_MISMATCH(stream, order, config, now_ts) -> RuleOutcome:
    """Captured amount != order amount -> ORDER_PAYMENT_MISMATCH."""
    for e in _captured_payments(stream):
        amt = e.amount if e.amount is not None else order.amount
        if amt != order.amount:
            return RuleOutcome(
                True, ResolvedState.ORDER_PAYMENT_MISMATCH,
                f"amount {amt} != order amount {order.amount}",
                {"payment_id": e.payment_id, "amount": amt},
            )
    return RuleOutcome(False)


def R04_MULTI_SUCCESS(stream, order, config, now_ts) -> RuleOutcome:
    """Two or more distinct successful payments -> DUPLICATE_PAYMENT."""
    ids = _captured_ids(stream)
    if len(ids) >= 2:
        return RuleOutcome(
            True, ResolvedState.DUPLICATE_PAYMENT,
            f"{len(ids)} distinct successful payments for one order",
            {"successful_payment_ids": sorted(ids)},
        )
    return RuleOutcome(False)


def R05_LATE_AUTH(stream, order, config, now_ts) -> RuleOutcome:
    """Authorized after the decision window, uncaptured -> LATE_AUTHORIZATION."""
    if _captured_payments(stream):
        return RuleOutcome(False)
    created_ts = parse_ts(order.created_at)
    for e in _authorized(stream):
        if e.occurred_ts() > created_ts + config.late_auth_window_seconds:
            return RuleOutcome(
                True, ResolvedState.LATE_AUTHORIZATION,
                "payment authorized after decision window",
                {"payment_id": e.payment_id},
            )
    return RuleOutcome(False)


def R06_CAPTURED_OK(stream, order, config, now_ts) -> RuleOutcome:
    """Exactly one successful capture (currency/amount already validated) ->
    NORMAL_SUCCESS."""
    if len(_captured_payments(stream)) == 1:
        return RuleOutcome(True, ResolvedState.NORMAL_SUCCESS, "exactly one successful capture")
    return RuleOutcome(False)


def R07_TERMINAL_FAILURE(stream, order, config, now_ts) -> RuleOutcome:
    """Only terminal failure/abandonment, no success -> NORMAL_FAILURE."""
    if _failures(stream) and not _captured_payments(stream):
        return RuleOutcome(
            True, ResolvedState.NORMAL_FAILURE, "terminal failure with no successful capture"
        )
    return RuleOutcome(False)


def R08_NON_TERMINAL(stream, order, config, now_ts) -> RuleOutcome:
    """Latest state non-terminal (initiated/pending/authorized in-window), no
    capture, no failure -> PENDING_PAYMENT."""
    if _pending_like(stream) and not _captured_payments(stream) and not _failures(stream):
        return RuleOutcome(True, ResolvedState.PENDING_PAYMENT, "non-terminal event, outcome pending")
    return RuleOutcome(False)


def R09_FALLTHROUGH(stream, order, config, now_ts) -> RuleOutcome:
    """No rule matched (unrecognized/garbage) -> HUMAN_REVIEW (safe default)."""
    return RuleOutcome(True, ResolvedState.HUMAN_REVIEW, "no deterministic rule matched")


RULE_PRECEDENCE = [
    R01_CONTRADICTION,
    R02_CURRENCY_MISMATCH,
    R03_AMOUNT_MISMATCH,
    R04_MULTI_SUCCESS,
    R05_LATE_AUTH,
    R06_CAPTURED_OK,
    R07_TERMINAL_FAILURE,
    R08_NON_TERMINAL,
    R09_FALLTHROUGH,
]
