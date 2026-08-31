"""Focused unit tests for the deterministic safety gate (P4-A).

Tests each safety invariant in isolation against the evaluate_safety function.
"""

from __future__ import annotations

from backend.events import EventStream
from backend.models import (
    Intervention,
    Order,
    ResolutionConfig,
    ResolvedState,
)
from backend.safety import (
    IdempotencyStore,
    evaluate_safety,
)

ORDER_AMOUNT = 100000
ORDER_CURRENCY = "INR"
ORDER_CREATED = "2026-08-26T10:00:00+00:00"


def _order(**kw) -> Order:
    return Order(
        order_id=kw.get("order_id", "ORD-1"),
        amount=kw.get("amount", ORDER_AMOUNT),
        currency=kw.get("currency", ORDER_CURRENCY),
        created_at=kw.get("created_at", ORDER_CREATED),
    )


def _stream(raw_events, order_id="ORD-1"):
    return EventStream.from_raw(raw_events, order_id=order_id)


def _evaluate(order, stream, intervention, **kw):
    return evaluate_safety(
        order=order,
        stream=stream,
        resolved_state=kw.get("resolved_state", ResolvedState.LATE_AUTHORIZATION),
        intervention=intervention,
        target_payment_id=kw.get("target_payment_id"),
        idempotency_key=kw.get("idempotency_key"),
        idempotency_store=kw.get("idempotency_store"),
        order_money_actions=kw.get("order_money_actions", {}),
        order_recovery_links=kw.get("order_recovery_links", {}),
        circuit_breaker=kw.get("circuit_breaker"),
        execute=kw.get("execute", True),
        config=kw.get("config", ResolutionConfig()),
        ai_advisory=kw.get("ai_advisory"),
    )


# --- S01 idempotency ----------------------------------------------------------


def test_s01_idempotency_first_seen_passes():
    store = IdempotencyStore()
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        idempotency_key="key-1", idempotency_store=store,
    )
    assert results["S01_IDEMPOTENCY"] is True
    assert intervention == Intervention.CAPTURE_LATE_AUTH


def test_s01_idempotency_duplicate_blocked():
    store = IdempotencyStore()
    store.record("key-1")
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        idempotency_key="key-1", idempotency_store=store,
    )
    assert results["S01_IDEMPOTENCY"] is False
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- S02 one money action per order ------------------------------------------


def test_s02_first_money_action_passes():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        order_money_actions={},
    )
    assert results["S02_ONE_MONEY_ACTION"] is True


def test_s02_second_money_action_blocked():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        order_money_actions={"ORD-1": 1},
    )
    assert results["S02_ONE_MONEY_ACTION"] is False
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


def test_s02_non_money_action_always_passes():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.ESCALATE_HUMAN_REVIEW,
        order_money_actions={"ORD-1": 5},
    )
    assert results["S02_ONE_MONEY_ACTION"] is True


# --- S03 refund bound ---------------------------------------------------------


def test_s03_refund_within_captured_amount_passes():
    order = _order(amount=100000)
    events = [
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 100000, "currency": "INR"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.REFUND_DUPLICATE,
        target_payment_id="P1",
    )
    assert results["S03_REFUND_BOUND"] is True


def test_s03_refund_exceeds_captured_blocked():
    order = _order(amount=200000)
    events = [
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 100000, "currency": "INR"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.REFUND_DUPLICATE,
        target_payment_id="P1",
    )
    assert results["S03_REFUND_BOUND"] is False


# --- S04 capture bounds -------------------------------------------------------


def test_s04_capture_within_authorized_amount_passes():
    order = _order(amount=100000)
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "INR"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        target_payment_id="P1",
    )
    assert results["S04_CAPTURE_BOUNDS"] is True


def test_s04_capture_exceeds_authorized_blocked():
    order = _order(amount=200000)
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "INR"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        target_payment_id="P1",
    )
    assert results["S04_CAPTURE_BOUNDS"] is False
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


def test_s04_capture_exceeds_order_amount_blocked():
    order = _order(amount=100000)
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 50000, "currency": "INR"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        target_payment_id="P1",
    )
    assert results["S04_CAPTURE_BOUNDS"] is False


# --- S05 currency match -------------------------------------------------------


def test_s05_matching_currency_passes():
    order = _order(currency="INR")
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "INR"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
    )
    assert results["S05_CURRENCY_MATCH"] is True


def test_s05_authorized_currency_mismatch_blocked():
    order = _order(currency="INR")
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "USD"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
    )
    assert results["S05_CURRENCY_MATCH"] is False
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- S06 no blind retry -------------------------------------------------------


def test_s06_first_recovery_link_passes():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.SEND_RECOVERY_LINK,
        order_recovery_links={},
    )
    assert results["S06_NO_BLIND_RETRY"] is True


def test_s06_second_recovery_link_blocked():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.SEND_RECOVERY_LINK,
        order_recovery_links={"ORD-1": 1},
    )
    assert results["S06_NO_BLIND_RETRY"] is False
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- S07 capture window -------------------------------------------------------


def test_s07_authorization_within_window_passes():
    order = _order(created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        target_payment_id="P1",
        config=ResolutionConfig(late_auth_window_seconds=86400),
    )
    assert results["S07_CAPTURE_WINDOW"] is True


def test_s07_authorization_outside_window_blocked():
    order = _order(created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-30T10:00:00+00:00"},
    ]
    stream = _stream(events)
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        target_payment_id="P1",
        config=ResolutionConfig(late_auth_window_seconds=86400),
    )
    assert results["S07_CAPTURE_WINDOW"] is False


# --- S08 high-value ceiling --------------------------------------------------


def test_s08_low_value_passes():
    order = _order(amount=100000)
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        config=ResolutionConfig(high_value_threshold=50_000_000),
    )
    assert results["S08_HIGH_VALUE"] is True


def test_s08_high_value_blocked():
    order = _order(amount=100_000_000)
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        config=ResolutionConfig(high_value_threshold=50_000_000),
    )
    assert results["S08_HIGH_VALUE"] is False
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- S09 dry-run default ------------------------------------------------------


def test_s09_dry_run_records_failure_but_no_downgrade():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        execute=False,
    )
    assert results["S09_DRY_RUN"] is False
    assert intervention == Intervention.CAPTURE_LATE_AUTH


def test_s09_execute_mode_passes():
    order = _order()
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        execute=True,
    )
    assert results["S09_DRY_RUN"] is True


# --- fail-closed behavior -----------------------------------------------------


def test_fail_closed_preserves_resolved_state():
    order = _order(amount=100_000_000)
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        resolved_state=ResolvedState.LATE_AUTHORIZATION,
    )
    assert state == ResolvedState.LATE_AUTHORIZATION
    assert intervention == Intervention.ESCALATE_HUMAN_REVIEW


def test_fail_closed_does_not_mutate_store_on_failure():
    store = IdempotencyStore()
    order = _order(amount=100_000_000)
    stream = _stream([])
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        idempotency_key="key-1", idempotency_store=store,
    )
    assert store.already_executed("key-1") is False


def test_success_mutates_store_and_counters():
    store = IdempotencyStore()
    order = _order()
    stream = _stream([])
    money_actions = {}
    recovery_links = {}
    state, intervention, results = _evaluate(
        order, stream, Intervention.CAPTURE_LATE_AUTH,
        idempotency_key="key-1", idempotency_store=store,
        order_money_actions=money_actions,
        order_recovery_links=recovery_links,
    )
    assert store.already_executed("key-1") is True
    assert money_actions["ORD-1"] == 1
