"""Bounded execution layer for AI-Payment-Resolver.

Wires safety results to a deterministic simulated execution path. Money
movement is **disabled by default**; ``execute=True`` is required for any
money action. Only ``CAPTURE_LATE_AUTH`` and ``REFUND_DUPLICATE`` move
money. All other interventions are non-monetary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.models import DecisionRecord, Intervention, Order, RecoveryResult, ResolvedState


@dataclass
class SimulatedGateway:
    """Deterministic simulated payment gateway. No network I/O."""

    def capture(self, payment_id: str, amount: int, currency: str, execute: bool) -> RecoveryResult:
        if not execute:
            return RecoveryResult(
                intervention=Intervention.CAPTURE_LATE_AUTH,
                status="simulated",
                amount=amount,
                currency=currency,
                detail="dry-run capture",
            )
        return RecoveryResult(
            intervention=Intervention.CAPTURE_LATE_AUTH,
            status="executed",
            amount=amount,
            currency=currency,
            detail="simulated capture",
        )

    def refund(self, payment_id: str, amount: int, currency: str, execute: bool) -> RecoveryResult:
        if not execute:
            return RecoveryResult(
                intervention=Intervention.REFUND_DUPLICATE,
                status="simulated",
                amount=amount,
                currency=currency,
                detail="dry-run refund",
            )
        return RecoveryResult(
            intervention=Intervention.REFUND_DUPLICATE,
            status="executed",
            amount=amount,
            currency=currency,
            detail="simulated refund",
        )


_MONEY_MOVING = {
    Intervention.CAPTURE_LATE_AUTH,
    Intervention.REFUND_DUPLICATE,
}


def _get_target_payment_id(signals: dict) -> Optional[str]:
    """Extract target payment ID from signals."""
    if "successful_payment_ids" in signals:
        ids = signals["successful_payment_ids"]
        if ids:
            return ids[-1]
    if "payment_id" in signals:
        return signals["payment_id"]
    return None


def execute_intervention(
    record: DecisionRecord,
    execute: bool,
    order: Optional[Order] = None,
    gateway: Optional[SimulatedGateway] = None,
) -> RecoveryResult:
    """Execute or simulate the intervention for a single decision.

    Returns a ``RecoveryResult``. Money actions only run when ``execute`` is
    True **and** safety has approved the action (i.e. ``record.intervention``
    is still money-moving).
    """
    gateway = gateway or SimulatedGateway()
    intervention = record.intervention

    if intervention not in _MONEY_MOVING:
        return RecoveryResult(
            intervention=intervention,
            status="skipped",
            detail="non-money intervention",
        )

    target = _get_target_payment_id(record.signals)
    if not target:
        return RecoveryResult(
            intervention=intervention,
            status="blocked",
            detail="no target payment id",
        )

    if order is None:
        return RecoveryResult(
            intervention=intervention,
            status="blocked",
            detail="order required for execution",
        )

    if intervention == Intervention.CAPTURE_LATE_AUTH:
        return gateway.capture(target, order.amount, order.currency, execute)

    if intervention == Intervention.REFUND_DUPLICATE:
        refund_amount = _compute_refund_amount(record, order)
        return gateway.refund(target, refund_amount, order.currency, execute)

    return RecoveryResult(
        intervention=intervention,
        status="skipped",
        detail="non-money intervention",
    )


def _compute_refund_amount(record: DecisionRecord, order: Order) -> int:
    """Compute refund amount for duplicate payment.

    Refunds the captured amount of the target (later) duplicate payment,
    capped at the order amount.
    """
    target = _get_target_payment_id(record.signals)
    if not target or not hasattr(record, '_stream'):
        return order.amount

    captured_total = 0
    for e in record._stream.events:
        from backend.events import EventType
        if (
            e.event_type == EventType.PAYMENT_CAPTURED
            and (e.payment_id or e.event_id) == target
            and e.amount is not None
        ):
            captured_total += e.amount

    return min(captured_total, order.amount)
