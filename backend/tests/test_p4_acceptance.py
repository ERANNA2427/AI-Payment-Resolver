"""P4 acceptance tests for audit, circuit breaker, accounting identity, and replay."""

from __future__ import annotations

import tempfile
from pathlib import Path

from backend.audit import append_record, read_records, verify_append_only
from backend.metrics import BatchMetrics, compute_batch_metrics, replay_batch
from backend.models import (
    DecisionRecord,
    Intervention,
    Order,
    ResolutionConfig,
    ResolvedState,
    RuleStep,
)
from backend.resolver import process_order
from backend.safety import CircuitBreaker, IdempotencyStore


def _order(**kw) -> Order:
    return Order(
        order_id=kw.get("order_id", "ORD-1"),
        amount=kw.get("amount", 100000),
        currency=kw.get("currency", "INR"),
        created_at=kw.get("created_at", "2026-08-26T10:00:00+00:00"),
    )


def _record(**kw) -> DecisionRecord:
    return DecisionRecord(
        decision_id=kw.get("decision_id", "d1"),
        order_id=kw.get("order_id", "ORD-1"),
        timestamp="2026-08-26T12:00:00+00:00",
        resolved_state=kw.get("resolved_state", ResolvedState.NORMAL_SUCCESS),
        rule_trace=[RuleStep("R06_CAPTURED_OK", True, "captured")],
        risk_reason=kw.get("risk_reason"),
        intervention=kw.get("intervention", Intervention.NO_ACTION),
        idempotency_key=kw.get("idempotency_key", "key1"),
        inputs_hash="hash1",
        simulated=kw.get("simulated", True),
        revenue_at_risk=kw.get("revenue_at_risk", False),
        safety_results=kw.get("safety_results", {}),
        signals={},
    )


# --- immutable audit ---------------------------------------------------------


def test_audit_append_only_multiple_records():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": "2026-08-26T10:00:00+00:00"},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 100000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]
    rec1 = process_order(order, events, now="2026-08-26T12:00:00+00:00")
    rec2 = process_order(_order(order_id="ORD-2"), events, now="2026-08-26T12:01:00+00:00")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        path = tmp.name

    try:
        append_record(path, rec1)
        append_record(path, rec2)
        records = list(read_records(path))
        assert len(records) == 2
        assert records[0]["order_id"] == "ORD-1"
        assert records[1]["order_id"] == "ORD-2"
        assert verify_append_only(path) is True
    finally:
        Path(path).unlink(missing_ok=True)


def test_audit_immutable_after_write():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": "2026-08-26T10:00:00+00:00"},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 100000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]
    rec = process_order(order, events, now="2026-08-26T12:00:00+00:00")
    original_intervention = rec.intervention

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        path = tmp.name

    try:
        append_record(path, rec)
        rec.intervention = Intervention.ESCALATE_HUMAN_REVIEW
        records = list(read_records(path))
        assert records[0]["intervention"] == original_intervention.value
    finally:
        Path(path).unlink(missing_ok=True)


# --- circuit breaker ----------------------------------------------------------


def test_circuit_breaker_halts_after_threshold():
    breaker = CircuitBreaker(threshold=0.5)
    assert breaker.money_action_allowed() is True

    breaker.record(succeeded=False)
    breaker.record(succeeded=False)
    assert breaker.money_action_allowed() is False


def test_circuit_breaker_does_not_halt_below_threshold():
    breaker = CircuitBreaker(threshold=0.5)
    breaker.record(succeeded=True)
    breaker.record(succeeded=False)
    assert breaker.money_action_allowed() is True


def test_circuit_breaker_integration():
    breaker = CircuitBreaker(threshold=0.3)
    order = _order(created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": "2026-08-26T10:00:00+00:00"},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "INR", "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]

    rec1 = process_order(order, events, execute=True, circuit_breaker=breaker)
    breaker.record(succeeded=False)
    breaker.record(succeeded=False)

    rec2 = process_order(_order(order_id="ORD-2"), events, execute=True, circuit_breaker=breaker)
    assert rec2.safety_results.get("S11_CIRCUIT_BREAKER") is False


# --- accounting identity ------------------------------------------------------


def test_accounting_identity_holds():
    orders = {
        "ORD-1": _order(order_id="ORD-1", amount=100000),
        "ORD-2": _order(order_id="ORD-2", amount=200000),
        "ORD-3": _order(order_id="ORD-3", amount=150000),
    }
    records = [
        _record(order_id="ORD-1", resolved_state=ResolvedState.NORMAL_SUCCESS,
                intervention=Intervention.NO_ACTION, revenue_at_risk=False),
        _record(order_id="ORD-2", resolved_state=ResolvedState.DUPLICATE_PAYMENT,
                intervention=Intervention.REFUND_DUPLICATE, revenue_at_risk=True),
        _record(order_id="ORD-3", resolved_state=ResolvedState.NORMAL_FAILURE,
                intervention=Intervention.SEND_RECOVERY_LINK, revenue_at_risk=True),
    ]
    metrics = compute_batch_metrics(records, orders)
    assert metrics.accounting_identity_holds() is True
    assert metrics.captured == 100000
    assert metrics.refunded == 200000
    assert metrics.at_risk == 150000
    assert metrics.total_value == 450000


def test_accounting_identity_with_human_review():
    orders = {
        "ORD-1": _order(order_id="ORD-1", amount=100000),
    }
    records = [
        _record(order_id="ORD-1", resolved_state=ResolvedState.HUMAN_REVIEW,
                intervention=Intervention.ESCALATE_HUMAN_REVIEW, revenue_at_risk=True),
    ]
    metrics = compute_batch_metrics(records, orders)
    assert metrics.at_risk == 100000
    assert metrics.human_review_count == 1
    assert metrics.human_review_value == 100000


# --- idempotent replay --------------------------------------------------------


def test_replay_blocks_duplicate_keys():
    store = IdempotencyStore()
    store.record("key1")

    records = [
        _record(order_id="ORD-1", idempotency_key="key1"),
        _record(order_id="ORD-2", idempotency_key="key2"),
    ]
    new_records, blocked = replay_batch(records, store)
    assert blocked == 1
    assert len(new_records) == 1
    assert new_records[0].order_id == "ORD-2"


def test_replay_allows_new_keys():
    store = IdempotencyStore()

    records = [
        _record(order_id="ORD-1", idempotency_key="key1"),
        _record(order_id="ORD-2", idempotency_key="key2"),
    ]
    new_records, blocked = replay_batch(records, store)
    assert blocked == 0
    assert len(new_records) == 2


# --- batch metrics ------------------------------------------------------------


def test_batch_metrics_intervention_counts():
    orders = {
        "ORD-1": _order(order_id="ORD-1", amount=100000),
        "ORD-2": _order(order_id="ORD-2", amount=100000),
    }
    records = [
        _record(order_id="ORD-1", intervention=Intervention.NO_ACTION),
        _record(order_id="ORD-2", intervention=Intervention.SEND_RECOVERY_LINK),
    ]
    metrics = compute_batch_metrics(records, orders)
    assert metrics.intervention_counts["NO_ACTION"] == 1
    assert metrics.intervention_counts["SEND_RECOVERY_LINK"] == 1


def test_batch_metrics_exceptions():
    orders = {
        "ORD-1": _order(order_id="ORD-1", amount=100000),
    }
    records = [
        _record(order_id="ORD-1", safety_results={"S01_IDEMPOTENCY": False}),
    ]
    metrics = compute_batch_metrics(records, orders)
    assert metrics.exceptions == 1
    assert metrics.safety_violations == 1
    assert metrics.intentional_dry_run_blocks == 0


def test_batch_metrics_distinguishes_dry_run_block_from_safety_violation():
    orders = {
        "ORD-1": _order(order_id="ORD-1", amount=100000),
    }
    records = [
        _record(
            order_id="ORD-1",
            intervention=Intervention.REFUND_DUPLICATE,
            safety_results={"S09_DRY_RUN": False, "S08_HIGH_VALUE": True},
            revenue_at_risk=True,
            resolved_state=ResolvedState.DUPLICATE_PAYMENT,
        ),
    ]
    metrics = compute_batch_metrics(records, orders)
    assert metrics.intentional_dry_run_blocks == 1
    assert metrics.safety_violations == 0
    assert metrics.exceptions == 0
