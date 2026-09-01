"""Tests for batch metrics and accounting identity (backend/metrics.py).

Verifies that BatchMetrics aggregates correctly, the accounting identity
(captured + refunded + at_risk == total_value) holds, and the new dry_run_blocks
counter is separate from genuine safety exceptions.
"""

from backend.metrics import BatchMetrics, compute_batch_metrics, replay_batch
from backend.models import DecisionRecord, Intervention, Order, ResolvedState, RuleStep
from backend.safety import IdempotencyStore


def _order(order_id="ORD-1", amount=100000) -> Order:
    return Order(order_id=order_id, amount=amount, currency="INR")


def _record(
    order_id="ORD-1",
    resolved_state=ResolvedState.NORMAL_SUCCESS,
    intervention=Intervention.NO_ACTION,
    safety_results=None,
    revenue_at_risk=False,
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=f"DEC-{order_id}",
        order_id=order_id,
        timestamp="2026-08-26T12:00:00+00:00",
        resolved_state=resolved_state,
        rule_trace=[RuleStep(rule_id="R06_CAPTURED_OK", matched=True)],
        risk_reason=None,
        intervention=intervention,
        idempotency_key=f"key-{order_id}",
        inputs_hash="hash",
        safety_results=safety_results or {},
        revenue_at_risk=revenue_at_risk,
    )


# --- basic aggregation --------------------------------------------------------


def test_total_orders_count():
    records = [_record("A"), _record("B"), _record("C")]
    orders = {"A": _order("A"), "B": _order("B"), "C": _order("C")}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.total_orders == 3


def test_total_value_sums_order_amounts():
    records = [_record("A"), _record("B")]
    orders = {"A": _order("A", 100000), "B": _order("B", 200000)}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.total_value == 300000


def test_captured_counts_normal_success():
    records = [
        _record("A", ResolvedState.NORMAL_SUCCESS, Intervention.NO_ACTION),
        _record("B", ResolvedState.NORMAL_SUCCESS, Intervention.NO_ACTION),
    ]
    orders = {"A": _order("A", 100000), "B": _order("B", 50000)}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.captured == 150000


def test_refunded_counts_duplicate_payment():
    records = [_record("A", ResolvedState.DUPLICATE_PAYMENT, Intervention.REFUND_DUPLICATE)]
    orders = {"A": _order("A", 400000)}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.refunded == 400000


def test_at_risk_counts_revenue_at_risk():
    records = [_record("A", ResolvedState.NORMAL_FAILURE, Intervention.SEND_RECOVERY_LINK, revenue_at_risk=True)]
    orders = {"A": _order("A", 75000)}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.at_risk == 75000


# --- accounting identity ------------------------------------------------------


def test_accounting_identity_holds_for_clean_batch():
    records = [
        _record("A", ResolvedState.NORMAL_SUCCESS, Intervention.NO_ACTION),
        _record("B", ResolvedState.DUPLICATE_PAYMENT, Intervention.REFUND_DUPLICATE),
        _record("C", ResolvedState.NORMAL_FAILURE, Intervention.SEND_RECOVERY_LINK, revenue_at_risk=True),
    ]
    orders = {
        "A": _order("A", 100000),
        "B": _order("B", 200000),
        "C": _order("C", 50000),
    }
    metrics = compute_batch_metrics(records, orders)
    assert metrics.accounting_identity_holds() is True
    assert metrics.captured + metrics.refunded + metrics.at_risk == metrics.total_value


def test_accounting_identity_fails_when_inconsistent():
    metrics = BatchMetrics()
    metrics.total_value = 100000
    metrics.captured = 50000
    metrics.refunded = 0
    metrics.at_risk = 0
    assert metrics.accounting_identity_holds() is False


# --- exception vs dry-run separation -----------------------------------------


def test_dry_run_blocks_counted_separately():
    records = [
        _record("A", safety_results={"S09_DRY_RUN": False, "S01_IDEMPOTENCY": True}),
        _record("B", safety_results={"S09_DRY_RUN": True, "S01_IDEMPOTENCY": False}),
    ]
    orders = {"A": _order("A"), "B": _order("B")}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.dry_run_blocks == 1
    assert metrics.exceptions == 1


def test_all_safe_records_have_zero_exceptions():
    records = [_record("A"), _record("B")]
    orders = {"A": _order("A"), "B": _order("B")}
    metrics = compute_batch_metrics(records, orders)
    assert metrics.exceptions == 0
    assert metrics.dry_run_blocks == 0


# --- human review -------------------------------------------------------------


def test_human_review_count_and_value():
    records = [
        _record("A", ResolvedState.HUMAN_REVIEW, Intervention.ESCALATE_HUMAN_REVIEW),
        _record("B", ResolvedState.HUMAN_REVIEW, Intervention.ESCALATE_HUMAN_REVIEW),
        _record("C", ResolvedState.NORMAL_SUCCESS, Intervention.NO_ACTION),
    ]
    orders = {
        "A": _order("A", 100000),
        "B": _order("B", 200000),
        "C": _order("C", 50000),
    }
    metrics = compute_batch_metrics(records, orders)
    assert metrics.human_review_count == 2
    assert metrics.human_review_value == 300000


# --- replay -------------------------------------------------------------------


def test_replay_blocks_duplicate_keys():
    records = [_record("A"), _record("A"), _record("B")]
    store = IdempotencyStore()
    new_records, blocked = replay_batch(records, store)
    assert blocked == 1
    assert len(new_records) == 2


def test_replay_with_no_store_blocks_all_duplicates():
    records = [_record("A"), _record("A")]
    new_records, blocked = replay_batch(records, None)
    assert blocked == 1
    assert len(new_records) == 1
