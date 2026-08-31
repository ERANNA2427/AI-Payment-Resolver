"""P2 tests: deterministic payment-state rules + resolver.

Covers every resolved state in PROJECT_SPEC.md §5/§6, conflicting/out-of-order
events, and the explainable rule_trace. No recovery/AI/metrics logic is tested
here (later phases).
"""

from backend.events import EventStream
from backend.models import Order, ResolvedState
from backend.resolvers.payment_state_resolver import resolve_state

ORDER_AMOUNT = 100000  # 1000.00 INR in paise
ORDER_CURRENCY = "INR"
ORDER_CREATED = "2026-08-26T10:00:00+00:00"


def _order(**kw) -> Order:
    return Order(
        order_id="ORD-1",
        amount=kw.get("amount", ORDER_AMOUNT),
        currency=kw.get("currency", ORDER_CURRENCY),
        created_at=kw.get("created_at", ORDER_CREATED),
    )


def _resolve(events, **kw) -> ResolvedState:
    return resolve_state(_order(**kw), events).resolved_state


# --- normal cases -------------------------------------------------------------

def test_normal_success():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.NORMAL_SUCCESS


def test_normal_failure():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "reason": "insufficient_funds",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.NORMAL_FAILURE


def test_abandoned_checkout():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "checkout.abandoned",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.NORMAL_FAILURE


def test_pending_payment():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.PENDING_PAYMENT


def test_late_authorization():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]) == ResolvedState.LATE_AUTHORIZATION


def test_duplicate_payment():
    res = resolve_state(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ])
    assert res.resolved_state == ResolvedState.DUPLICATE_PAYMENT
    assert res.signals.get("successful_payment_ids") == ["P1", "P2"]


def test_amount_mismatch():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 90000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.ORDER_PAYMENT_MISMATCH


def test_currency_mismatch():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "USD", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.ORDER_PAYMENT_MISMATCH


# --- conflicting / adversarial cases ------------------------------------------

def test_contradiction_captured_and_failed():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:30:00+00:00"},
    ]) == ResolvedState.HUMAN_REVIEW


def test_order_paid_without_capture():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "p", "event_type": "order.paid", "amount": ORDER_AMOUNT, "currency": "INR",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.HUMAN_REVIEW


def test_refund_without_capture():
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "r", "event_type": "refund.created", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.HUMAN_REVIEW


def test_only_order_created_falls_through():
    # Incomplete input has no matching rule -> safe HUMAN_REVIEW (R09).
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
    ]) == ResolvedState.HUMAN_REVIEW


def test_garbage_event_still_resolves_safely():
    # An unrecognized event is dropped during normalization; resolution stays safe.
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "x", "event_type": "payment.teleported", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]) == ResolvedState.HUMAN_REVIEW


# --- precedence / ordering ----------------------------------------------------

def test_contradiction_beats_happy_path():
    # Even with a valid capture present, a contradiction forces HUMAN_REVIEW
    # because R01 precedes R06.
    assert _resolve([
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:30:00+00:00"},
    ]) == ResolvedState.HUMAN_REVIEW


def test_out_of_order_delivery_is_order_independent():
    # Deliver capture before authorize; sort by occurred_at makes it NORMAL_SUCCESS.
    events = [
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
    ]
    assert resolve_state(_order(), events).resolved_state == ResolvedState.NORMAL_SUCCESS


def test_deterministic_repeated_resolution():
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]
    r1 = resolve_state(_order(), events)
    r2 = resolve_state(_order(), events)
    assert r1.resolved_state == r2.resolved_state
    assert [s.rule_id for s in r1.rule_trace] == [s.rule_id for s in r2.rule_trace]


# --- explainability -----------------------------------------------------------

def test_rule_trace_has_single_hit_and_full_explanation():
    res = resolve_state(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    hits = [s for s in res.rule_trace if s.matched]
    assert len(hits) == 1
    assert hits[0].rule_id == "R06_CAPTURED_OK"
    # Every evaluated rule is represented with an explicit hit/miss marker.
    assert all(isinstance(s.matched, bool) for s in res.rule_trace)
    assert res.rule_trace[-1].matched is True
