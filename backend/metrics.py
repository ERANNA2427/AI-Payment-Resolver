"""Metrics and accounting identity for AI-Payment-Resolver (spec §11).

Provides batch evaluation, accounting identity verification, and
idempotent replay support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.models import DecisionRecord, Intervention, Order, ResolvedState


@dataclass
class BatchMetrics:
    """Aggregated metrics for a batch of decisions."""

    total_orders: int = 0
    total_value: int = 0
    captured: int = 0
    refunded: int = 0
    at_risk: int = 0
    # Legacy alias retained for compatibility; equals safety_violations.
    exceptions: int = 0
    safety_violations: int = 0
    intentional_dry_run_blocks: int = 0
    intervention_counts: dict = field(default_factory=dict)
    human_review_count: int = 0
    human_review_value: int = 0
    money_actions_halted: int = 0

    def accounting_identity_holds(self) -> bool:
        """Verify: captured + refunded + at_risk = total value processed."""
        return (self.captured + self.refunded + self.at_risk) == self.total_value


def compute_batch_metrics(records: list[DecisionRecord], orders: dict[str, Order]) -> BatchMetrics:
    """Compute aggregated metrics from a batch of decision records."""
    metrics = BatchMetrics()
    metrics.total_orders = len(records)

    for record in records:
        order = orders.get(record.order_id)
        if not order:
            continue

        value = order.amount
        metrics.total_value += value

        intervention = record.intervention
        metrics.intervention_counts[intervention.value] = (
            metrics.intervention_counts.get(intervention.value, 0) + 1
        )

        if record.resolved_state == ResolvedState.NORMAL_SUCCESS:
            metrics.captured += value
        elif intervention == Intervention.REFUND_DUPLICATE and record.resolved_state == ResolvedState.DUPLICATE_PAYMENT:
            metrics.refunded += value
            metrics.at_risk += 0
        elif record.revenue_at_risk:
            metrics.at_risk += value

        safety_results = record.safety_results or {}
        if safety_results.get("S09_DRY_RUN") is False:
            metrics.intentional_dry_run_blocks += 1

        non_dry_run_failure = any(
            (passed is False)
            for check_id, passed in safety_results.items()
            if check_id != "S09_DRY_RUN"
        )
        if non_dry_run_failure:
            metrics.safety_violations += 1
            metrics.exceptions += 1

        if intervention == Intervention.ESCALATE_HUMAN_REVIEW:
            metrics.human_review_count += 1
            metrics.human_review_value += value

        if record.safety_results.get("S11_CIRCUIT_BREAKER") is False:
            metrics.money_actions_halted += 1

    return metrics


def replay_batch(
    records: list[DecisionRecord],
    idempotency_store: Optional[object] = None,
) -> tuple[list[DecisionRecord], int]:
    """Replay a batch of decisions, verifying idempotency.

    Returns (new_records, blocked_count) where blocked_count is the number
    of decisions that were blocked due to idempotency.
    """
    if idempotency_store is None:
        from backend.safety import IdempotencyStore
        idempotency_store = IdempotencyStore()

    new_records = []
    blocked = 0

    for record in records:
        key = record.idempotency_key
        if idempotency_store.already_executed(key):
            blocked += 1
            continue

        idempotency_store.record(key)
        new_records.append(record)

    return new_records, blocked