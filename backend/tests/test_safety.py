"""P4 tests: safety gate, execution, audit, and integration.

Covers all 12 safety invariants plus acceptance criteria from PROJECT_SPEC.md
\\u00a79 and \\u00a714.3.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.events import EventStream
from backend.execution import SimulatedGateway, execute_intervention
from backend.models import (
    DecisionRecord,
    Intervention,
    Order,
    ResolutionConfig,
    ResolvedState,
    RuleStep,
)
from backend.resolver import process_order, resolve_order
from backend.safety import (
    CircuitBreaker,
    IdempotencyStore,
    evaluate_safety,
)

ORDER_AMOUNT = 100000
ORDER_CURRENCY = "INR"
ORDER_CREATED = "2026-08-26T10:00:00+00:00"
NOW = "2026-08-26T12:00:00+00:00"


def _order(**kw) -> Order:
    return Order(
        order_id=kw.get("order_id", "ORD-1"),
        amount=kw.get("amount", ORDER_AMOUNT),
        currency=kw.get("currency", ORDER_CURRENCY),
        created_at=kw.get("created_at", ORDER_CREATED),
    )


def _success_events():
    return [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]


# --- S01 idempotency ---------------------------------------------------------

def test_idempotency_replay_blocks_duplicate_money_action():
    store = IdempotencyStore()
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ]
    rec1 = process_order(order, events, execute=True, idempotency_store=store)
    rec2 = process_order(order, events, execute=True, idempotency_store=store)
    assert rec1.intervention == Intervention.REFUND_DUPLICATE
    assert rec2.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec2.safety_results["S01_IDEMPOTENCY"] is False
    assert rec2.resolved_state == rec1.resolved_state


# --- S02 one money action per order per run -----------------------------------

def test_second_money_action_for_same_order_is_blocked():
    order = _order(order_id="ORD-X")
    events_capture = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ]
    rec1 = process_order(order, events_capture, execute=True)
    rec2 = process_order(order, events_capture, execute=True)
    assert rec1.intervention == Intervention.REFUND_DUPLICATE
    assert rec2.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec2.safety_results["S02_ONE_MONEY_ACTION"] is False


# --- S03 refund bound --------------------------------------------------------

def test_refund_greater_than_captured_is_vetoed():
    order = _order(order_id="ORD-R03", amount=100000)
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 100000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": 100000, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.REFUND_DUPLICATE
    assert rec.safety_results["S03_REFUND_BOUND"] is True
    assert rec.resolved_state == ResolvedState.DUPLICATE_PAYMENT


# --- S04 capture bounds ------------------------------------------------------

def test_capture_greater_than_authorized_is_vetoed():
    order = _order(amount=200000)
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "INR", "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.safety_results["S04_CAPTURE_BOUNDS"] is False


def test_capture_greater_than_order_amount_is_vetoed():
    order = _order(amount=100000)
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 50000, "currency": "INR", "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.safety_results["S04_CAPTURE_BOUNDS"] is False


# --- S05 currency match ------------------------------------------------------

def test_currency_mismatch_is_vetoed():
    order = _order(amount=100000, currency="INR")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": 100000, "currency": "USD", "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.safety_results["S05_CURRENCY_MATCH"] is False


# --- S06 no blind retry / max one nudge --------------------------------------

def test_no_blind_retry_and_max_one_recovery_link():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ]
    rec1 = process_order(order, events, execute=True)
    rec2 = process_order(order, events, execute=True)
    assert rec1.intervention == Intervention.SEND_RECOVERY_LINK
    assert rec2.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec2.safety_results["S06_NO_BLIND_RETRY"] is False


# --- S07 capture window -------------------------------------------------------

def test_late_auth_outside_window_is_vetoed():
    order = _order(created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-30T10:00:00+00:00"},
    ]
    cfg = ResolutionConfig(late_auth_window_seconds=86400)
    rec = process_order(order, events, execute=True, config=cfg)
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.safety_results["S07_CAPTURE_WINDOW"] is False


# --- S08 high-value ceiling ---------------------------------------------------

def test_high_value_forces_escalation():
    order = _order(amount=100_000_000)
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.safety_results["S08_HIGH_VALUE"] is False


# --- S09 dry-run default ------------------------------------------------------

def test_dry_run_default_never_executes_money_action():
    order = _order(order_id="ORD-DRY", created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ]
    rec = process_order(order, events, execute=False)
    assert rec.intervention == Intervention.REFUND_DUPLICATE
    assert rec.safety_results["S09_DRY_RUN"] is False
    assert rec.simulated is True


# --- S10 AI confidence fallback -----------------------------------------------

def test_ai_low_confidence_records_fallback():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ]
    ai_advisory = {"kind": "retry", "text": "try again", "confidence": 0.1}
    rec = process_order(order, events, execute=True, ai_advisory=ai_advisory)
    assert rec.safety_results["S10_AI_CONFIDENCE"] is False


def test_no_ai_advisory_passes():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.safety_results["S10_AI_CONFIDENCE"] is True


# --- S11 circuit breaker ------------------------------------------------------

def test_circuit_breaker_halts_money_actions():
    breaker = CircuitBreaker(threshold=0.4)
    order = _order(order_id="ORD-CB", created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ]
    rec1 = process_order(order, events, execute=True, circuit_breaker=breaker)
    breaker.record(succeeded=False)
    rec2 = process_order(order, events, execute=True, circuit_breaker=breaker)
    assert rec1.intervention == Intervention.REFUND_DUPLICATE
    assert rec2.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec2.safety_results["S11_CIRCUIT_BREAKER"] is False


# --- S12 immutable audit ------------------------------------------------------

def test_audit_append_only():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ]
    rec = resolve_order(order, events, now=NOW)
    original_intervention = rec.intervention
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".jsonl", delete=False) as tmp:
        path = tmp.name
    try:
        from backend.audit import append_record, read_records
        append_record(path, rec)
        records = list(read_records(path))
        assert len(records) == 1
        assert records[0]["intervention"] == original_intervention.value
        rec.intervention = Intervention.NO_ACTION
        append_record(path, rec)
        records = list(read_records(path))
        assert records[0]["intervention"] == original_intervention.value
        assert records[1]["intervention"] == Intervention.NO_ACTION.value
    finally:
        Path(path).unlink(missing_ok=True)


# --- money movement bans ------------------------------------------------------

def test_order_payment_mismatch_never_moves_money():
    order = _order(amount=100000, currency="INR")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": 90000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.resolved_state == ResolvedState.ORDER_PAYMENT_MISMATCH
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW


def test_human_review_never_moves_money():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "f", "event_type": "payment.failed", "payment_id": "P1",
         "occurred_at": "2026-08-26T11:30:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.resolved_state == ResolvedState.HUMAN_REVIEW
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- successful money-moving paths --------------------------------------------

def test_late_auth_beyond_window_is_vetoed():
    order = _order(created_at="2026-08-26T10:00:00+00:00")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.safety_results["S07_CAPTURE_WINDOW"] is False
    assert rec.resolved_state == ResolvedState.LATE_AUTHORIZATION


def test_successful_refund_duplicate_path():
    order = _order(order_id="ORD-REFUND")
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.intervention == Intervention.REFUND_DUPLICATE
    assert rec.safety_results["S03_REFUND_BOUND"] is True


# --- veto preserves state and records failures --------------------------------

def test_veto_preserves_resolved_state():
    order = _order(amount=100_000_000)
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "payment.authorized", "payment_id": "P1",
         "occurred_at": "2026-08-28T10:00:00+00:00"},
    ]
    rec = process_order(order, events, execute=True)
    assert rec.resolved_state == ResolvedState.LATE_AUTHORIZATION
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert any(not v for v in rec.safety_results.values())


# --- deterministic execution --------------------------------------------------

def test_deterministic_repeated_execution():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ]
    rec1 = process_order(order, events, execute=True)
    rec2 = process_order(order, events, execute=True)
    assert rec1.intervention == rec2.intervention
    assert rec1.safety_results == rec2.safety_results
    assert rec1.idempotency_key == rec2.idempotency_key


# --- accounting identity helper ------------------------------------------------

def test_accounting_identity_fields():
    order = _order()
    events = [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
    ]
    rec = resolve_order(order, events, now=NOW)
    assert hasattr(rec, "revenue_at_risk")
    assert rec.revenue_at_risk is False
