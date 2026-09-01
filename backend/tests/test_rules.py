"""Tests for deterministic resolution rules (backend/rules.py).

Each rule is a pure predicate over a normalized EventStream. These tests
verify that the rules fire (or don't) on representative inputs, and that
RULE_PRECEDENCE ordering is respected.
"""

from backend.events import EventStream
from backend.models import Order, ResolutionConfig, ResolvedState
from backend.rules import (
    R01_CONTRADICTION,
    R02_CURRENCY_MISMATCH,
    R03_AMOUNT_MISMATCH,
    R04_MULTI_SUCCESS,
    R05_LATE_AUTH,
    R06_CAPTURED_OK,
    R07_TERMINAL_FAILURE,
    R08_NON_TERMINAL,
    R09_FALLTHROUGH,
    RULE_PRECEDENCE,
)

ORDER_AMOUNT = 100000
ORDER_CURRENCY = "INR"
ORDER_CREATED = "2026-08-26T10:00:00+00:00"
NOW = "2026-08-26T12:00:00+00:00"
CONFIG = ResolutionConfig()


def _order(**kw) -> Order:
    return Order(
        order_id=kw.get("order_id", "ORD-1"),
        amount=kw.get("amount", ORDER_AMOUNT),
        currency=kw.get("currency", ORDER_CURRENCY),
        created_at=kw.get("created_at", ORDER_CREATED),
    )


def _stream(raw_events) -> EventStream:
    return EventStream.from_raw(raw_events, order_id="ORD-1")


# --- R01_CONTRADICTION ---------------------------------------------------------


def test_r01_same_payment_captured_and_failed():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:05:00+00:00"},
    ])
    outcome = R01_CONTRADICTION(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.HUMAN_REVIEW


def test_r01_order_paid_with_no_capture():
    stream = _stream([
        {"event_id": "p", "event_type": "order.paid", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R01_CONTRADICTION(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.HUMAN_REVIEW


def test_r01_refund_with_no_capture():
    stream = _stream([
        {"event_id": "r", "event_type": "refund.created", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R01_CONTRADICTION(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.HUMAN_REVIEW


def test_r01_no_contradiction_on_clean_capture():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R01_CONTRADICTION(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R02_CURRENCY_MISMATCH ----------------------------------------------------


def test_r02_currency_mismatch_fires():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "USD", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R02_CURRENCY_MISMATCH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.ORDER_PAYMENT_MISMATCH


def test_r02_matching_currency_passes():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R02_CURRENCY_MISMATCH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R03_AMOUNT_MISMATCH ------------------------------------------------------


def test_r03_amount_mismatch_fires():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 200000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R03_AMOUNT_MISMATCH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.ORDER_PAYMENT_MISMATCH


def test_r03_matching_amount_passes():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R03_AMOUNT_MISMATCH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R04_MULTI_SUCCESS --------------------------------------------------------


def test_r04_two_distinct_captures_fires():
    stream = _stream([
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ])
    outcome = R04_MULTI_SUCCESS(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.DUPLICATE_PAYMENT


def test_r04_single_capture_passes():
    stream = _stream([
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R04_MULTI_SUCCESS(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R05_LATE_AUTH -----------------------------------------------------------


def test_r05_late_authorization_fires():
    late_time = "2026-08-27T10:00:01+00:00"
    stream = _stream([
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": late_time},
    ])
    outcome = R05_LATE_AUTH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.LATE_AUTHORIZATION


def test_r05_in_window_authorization_passes():
    stream = _stream([
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R05_LATE_AUTH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R06_CAPTURED_OK ---------------------------------------------------------


def test_r06_exactly_one_capture_fires():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R06_CAPTURED_OK(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.NORMAL_SUCCESS


def test_r06_no_capture_passes():
    stream = _stream([
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R06_CAPTURED_OK(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R07_TERMINAL_FAILURE ----------------------------------------------------


def test_r07_terminal_failure_fires():
    stream = _stream([
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R07_TERMINAL_FAILURE(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.NORMAL_FAILURE


def test_r07_with_capture_passes():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P2",
         "occurred_at": "2026-08-26T11:05:00+00:00"},
    ])
    outcome = R07_TERMINAL_FAILURE(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R08_NON_TERMINAL --------------------------------------------------------


def test_r08_pending_payment_fires():
    stream = _stream([
        {"event_id": "i", "event_type": "payment.initiated", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
    ])
    outcome = R08_NON_TERMINAL(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.PENDING_PAYMENT


def test_r08_with_failure_passes():
    stream = _stream([
        {"event_id": "i", "event_type": "payment.initiated", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:05:00+00:00"},
    ])
    outcome = R08_NON_TERMINAL(stream, _order(), CONFIG, NOW)
    assert outcome.matched is False


# --- R09_FALLTHROUGH ---------------------------------------------------------


def test_r09_fallthrough_always_fires():
    stream = _stream([])
    outcome = R09_FALLTHROUGH(stream, _order(), CONFIG, NOW)
    assert outcome.matched is True
    assert outcome.state == ResolvedState.HUMAN_REVIEW


# --- RULE_PRECEDENCE ---------------------------------------------------------


def test_rule_precedence_has_all_nine_rules():
    assert len(RULE_PRECEDENCE) == 9
    assert RULE_PRECEDENCE[0] == R01_CONTRADICTION
    assert RULE_PRECEDENCE[-1] == R09_FALLTHROUGH


def test_precedence_contradiction_before_happy_path():
    stream = _stream([
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "USD", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:05:00+00:00"},
    ])
    matched_rule = None
    for rule in RULE_PRECEDENCE:
        outcome = rule(stream, _order(), CONFIG, NOW)
        if outcome.matched:
            matched_rule = rule
            break
    assert matched_rule == R01_CONTRADICTION
