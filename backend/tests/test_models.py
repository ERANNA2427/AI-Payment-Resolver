"""Focused tests for backend/models.py (domain model, P1)."""

import pytest

from backend.models import (
    DecisionRecord,
    Intervention,
    Money,
    Order,
    ResolutionConfig,
    ResolutionResult,
    ResolvedState,
    RuleStep,
)


def test_resolved_state_values_match_spec():
    expected = {
        "NORMAL_SUCCESS",
        "NORMAL_FAILURE",
        "PENDING_PAYMENT",
        "LATE_AUTHORIZATION",
        "DUPLICATE_PAYMENT",
        "ORDER_PAYMENT_MISMATCH",
        "HUMAN_REVIEW",
    }
    assert {s.value for s in ResolvedState} == expected


def test_intervention_values_match_spec():
    expected = {
        "NO_ACTION",
        "SEND_RECOVERY_LINK",
        "SEND_PENDING_NUDGE",
        "RECONCILE_PENDING",
        "CAPTURE_LATE_AUTH",
        "VOID_LATE_AUTH",
        "REFUND_DUPLICATE",
        "ESCALATE_HUMAN_REVIEW",
    }
    assert {i.value for i in Intervention} == expected


def test_money_rejects_unsafe_values():
    m = Money(amount=100, currency="inr")
    assert m.currency == "INR"  # normalized to uppercase
    with pytest.raises(ValueError):
        Money(amount=-1, currency="INR")
    with pytest.raises(ValueError):
        Money(amount=100, currency="US1")  # not alphabetic
    with pytest.raises(TypeError):
        Money(amount=1.5, currency="INR")


def test_order_rejects_unsafe_values():
    o = Order(order_id="ORD-1", amount=100000, currency="inr")
    assert o.currency == "INR"  # normalized to uppercase
    with pytest.raises(ValueError):
        Order(order_id="ORD-1", amount=-5, currency="INR")
    with pytest.raises(ValueError):
        Order(order_id="", amount=100, currency="INR")
    with pytest.raises(ValueError):
        Order(order_id="ORD-1", amount=100, currency="US1")


def test_resolution_result_defaults():
    rr = ResolutionResult(order_id="ORD-1", resolved_state=ResolvedState.NORMAL_SUCCESS)
    assert rr.rule_trace == []
    assert rr.signals == {}


def test_decision_record_serializes_cleanly():
    rec = DecisionRecord(
        decision_id="D1",
        order_id="ORD-1",
        timestamp="2026-01-01T00:00:00+00:00",
        resolved_state=ResolvedState.HUMAN_REVIEW,
        rule_trace=[RuleStep("R01_CONTRADICTION", True, "conflict")],
        risk_reason="ambiguous",
        intervention=Intervention.ESCALATE_HUMAN_REVIEW,
        idempotency_key="k",
        inputs_hash="h",
    )
    d = rec.to_dict()
    assert d["resolved_state"] == "HUMAN_REVIEW"
    assert d["intervention"] == "ESCALATE_HUMAN_REVIEW"
    assert d["rule_trace"][0]["rule_id"] == "R01_CONTRADICTION"
    assert d["simulated"] is True


def test_resolution_config_defaults():
    cfg = ResolutionConfig()
    assert cfg.late_auth_window_seconds == 86400
    assert cfg.high_value_threshold == 50_000_000
