"""Focused tests for the bounded execution layer (P4-B).

Tests dry-run default, execute flag, money movement rules, veto handling,
and status accuracy.
"""

from __future__ import annotations

from backend.execution import SimulatedGateway, execute_intervention
from backend.models import (
    DecisionRecord,
    Intervention,
    Order,
    RecoveryResult,
    ResolvedState,
    RuleStep,
)


def _record(intervention=Intervention.CAPTURE_LATE_AUTH, signals=None, safety_results=None):
    return DecisionRecord(
        decision_id="d1",
        order_id="ORD-1",
        timestamp="2026-08-26T12:00:00+00:00",
        resolved_state=ResolvedState.LATE_AUTHORIZATION,
        rule_trace=[RuleStep("R05_LATE_AUTH", True, "late auth")],
        risk_reason="late auth",
        intervention=intervention,
        idempotency_key="key1",
        inputs_hash="hash1",
        simulated=True,
        revenue_at_risk=True,
        safety_results=safety_results or {},
        signals=signals if signals is not None else {"payment_id": "P1"},
    )


_order = Order(order_id="ORD-1", amount=100000, currency="INR")


# --- dry-run default ----------------------------------------------------------


def test_dry_run_returns_simulated_status():
    record = _record()
    result = execute_intervention(record, execute=False, order=_order)
    assert result.status == "simulated"
    assert result.amount == 100000
    assert result.currency == "INR"


def test_execute_returns_executed_status():
    record = _record()
    result = execute_intervention(record, execute=True, order=_order)
    assert result.status == "executed"


# --- only money-moving interventions -----------------------------------------


def test_capture_is_money_moving():
    record = _record(Intervention.CAPTURE_LATE_AUTH)
    result = execute_intervention(record, execute=False, order=_order)
    assert result.status == "simulated"


def test_refund_is_money_moving():
    record = _record(Intervention.REFUND_DUPLICATE, signals={"successful_payment_ids": ["P1", "P2"]})
    result = execute_intervention(record, execute=False, order=_order)
    assert result.status == "simulated"


def test_human_review_skipped():
    record = _record(Intervention.ESCALATE_HUMAN_REVIEW)
    result = execute_intervention(record, execute=True, order=_order)
    assert result.status == "skipped"


def test_no_action_skipped():
    record = _record(Intervention.NO_ACTION)
    result = execute_intervention(record, execute=True, order=_order)
    assert result.status == "skipped"


def test_recovery_link_skipped():
    record = _record(Intervention.SEND_RECOVERY_LINK)
    result = execute_intervention(record, execute=True, order=_order)
    assert result.status == "skipped"


# --- veto handling ------------------------------------------------------------


def test_vetoed_intervention_blocked():
    record = _record(Intervention.ESCALATE_HUMAN_REVIEW, signals={"payment_id": "P1"})
    result = execute_intervention(record, execute=True, order=_order)
    assert result.status == "skipped"
    assert result.intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- target payment id -------------------------------------------------------


def test_no_target_blocked():
    record = _record(Intervention.CAPTURE_LATE_AUTH, signals={})
    result = execute_intervention(record, execute=True, order=_order)
    assert result.status == "blocked"


# --- gateway -----------------------------------------------------------------


def test_gateway_capture_simulated():
    gw = SimulatedGateway()
    result = gw.capture("P1", 100000, "INR", execute=False)
    assert result.status == "simulated"
    assert result.amount == 100000


def test_gateway_capture_executed():
    gw = SimulatedGateway()
    result = gw.capture("P1", 100000, "INR", execute=True)
    assert result.status == "executed"


def test_gateway_refund_simulated():
    gw = SimulatedGateway()
    result = gw.refund("P2", 100000, "INR", execute=False)
    assert result.status == "simulated"


def test_gateway_refund_executed():
    gw = SimulatedGateway()
    result = gw.refund("P2", 100000, "INR", execute=True)
    assert result.status == "executed"
