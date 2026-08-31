"""P3 tests: orchestrator (resolve -> risk -> policy -> DecisionRecord).

Verifies the policy mapping, risk reasons, audit-record construction, and the
deterministic idempotency/input-hash guarantees. No I/O is performed by the
orchestrator (audit writing / execution are later phases).
"""

from backend.models import DecisionRecord, Intervention, Order, ResolvedState
from backend.resolver import (
    _canonical_hash,
    _idempotency_key,
    recommend_intervention,
    resolve_batch,
    resolve_order,
    risk_reason_for,
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


# --- policy mapping (spec §8) --------------------------------------------------

def test_policy_mapping_covers_all_states():
    expected = {
        ResolvedState.NORMAL_SUCCESS: Intervention.NO_ACTION,
        ResolvedState.NORMAL_FAILURE: Intervention.SEND_RECOVERY_LINK,
        ResolvedState.PENDING_PAYMENT: Intervention.RECONCILE_PENDING,
        ResolvedState.LATE_AUTHORIZATION: Intervention.CAPTURE_LATE_AUTH,
        ResolvedState.DUPLICATE_PAYMENT: Intervention.REFUND_DUPLICATE,
        ResolvedState.ORDER_PAYMENT_MISMATCH: Intervention.ESCALATE_HUMAN_REVIEW,
        ResolvedState.HUMAN_REVIEW: Intervention.ESCALATE_HUMAN_REVIEW,
    }
    for state, intervention in expected.items():
        assert recommend_intervention(state) == intervention


def test_only_two_interventions_move_money():
    money_movers = {
        recommend_intervention(ResolvedState.LATE_AUTHORIZATION),
        recommend_intervention(ResolvedState.DUPLICATE_PAYMENT),
    }
    assert money_movers == {Intervention.CAPTURE_LATE_AUTH, Intervention.REFUND_DUPLICATE}


def test_mismatch_and_review_never_move_money():
    for state in (ResolvedState.ORDER_PAYMENT_MISMATCH, ResolvedState.HUMAN_REVIEW):
        assert recommend_intervention(state) == Intervention.ESCALATE_HUMAN_REVIEW


def test_payment_failed_maps_to_send_recovery_link():
    assert recommend_intervention(ResolvedState.NORMAL_FAILURE, [
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ]) == Intervention.SEND_RECOVERY_LINK


def test_checkout_abandoned_maps_to_no_action():
    assert recommend_intervention(ResolvedState.NORMAL_FAILURE, [
        {"event_id": "a", "event_type": "checkout.abandoned", "occurred_at": NOW},
    ]) == Intervention.NO_ACTION


def test_resolve_order_abandoned_checkout_maps_to_no_action():
    rec = resolve_order(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "a", "event_type": "checkout.abandoned", "occurred_at": NOW},
    ], now=NOW)
    assert rec.resolved_state == ResolvedState.NORMAL_FAILURE
    assert rec.intervention == Intervention.NO_ACTION
    assert rec.revenue_at_risk is True


# --- risk reasons (spec §3.4, §11) --------------------------------------------

def test_risk_reason_for_success_is_none():
    assert risk_reason_for(ResolvedState.NORMAL_SUCCESS) is None


def test_risk_reason_for_failure_states():
    assert risk_reason_for(ResolvedState.NORMAL_FAILURE) == "terminal payment failure"
    assert risk_reason_for(ResolvedState.DUPLICATE_PAYMENT) == "duplicate successful payment detected"


# --- orchestrator / DecisionRecord -------------------------------------------

def test_resolve_order_builds_audit_record():
    rec = resolve_order(_order(), _success_events(), now=NOW)
    assert isinstance(rec, DecisionRecord)
    assert rec.resolved_state == ResolvedState.NORMAL_SUCCESS
    assert rec.intervention == Intervention.NO_ACTION
    assert rec.risk_reason is None
    assert rec.revenue_at_risk is False
    assert rec.simulated is True
    assert rec.safety_results == {}        # filled by P4 safety gate
    assert rec.ai_advisory is None          # filled by P6 AI layer
    assert rec.idempotency_key
    assert rec.inputs_hash
    # The explainable trace from the resolver is preserved.
    assert any(s.rule_id == "R06_CAPTURED_OK" and s.matched for s in rec.rule_trace)


def test_revenue_at_risk_flag_set_for_failures():
    rec = resolve_order(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
    ], now=NOW)
    assert rec.resolved_state == ResolvedState.NORMAL_FAILURE
    assert rec.revenue_at_risk is True
    assert rec.intervention == Intervention.SEND_RECOVERY_LINK


def test_human_review_maps_to_escalation():
    rec = resolve_order(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "r", "event_type": "refund.created", "payment_id": "P1", "occurred_at": NOW},
    ], now=NOW)
    assert rec.resolved_state == ResolvedState.HUMAN_REVIEW
    assert rec.intervention == Intervention.ESCALATE_HUMAN_REVIEW
    assert rec.revenue_at_risk is True


def test_duplicate_target_payment_selected():
    rec = resolve_order(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ], now=NOW)
    assert rec.resolved_state == ResolvedState.DUPLICATE_PAYMENT
    # Idempotency key encodes the later (duplicate) payment as the refund target.
    assert rec.idempotency_key == _idempotency_key(
        "ORD-1", ResolvedState.DUPLICATE_PAYMENT, Intervention.REFUND_DUPLICATE, "P2"
    )


# --- determinism / idempotency ------------------------------------------------

def test_idempotency_key_is_deterministic():
    ev = _success_events()
    k1 = resolve_order(_order(), ev, now=NOW).idempotency_key
    k2 = resolve_order(_order(), ev, now=NOW).idempotency_key
    assert k1 == k2


def test_idempotency_key_changes_with_intervention():
    success_key = resolve_order(_order(), _success_events(), now=NOW).idempotency_key
    dup_key = resolve_order(_order(), [
        {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
        {"event_id": "c1", "event_type": "payment.captured", "payment_id": "P1",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
        {"event_id": "c2", "event_type": "payment.captured", "payment_id": "P2",
         "amount": ORDER_AMOUNT, "currency": "INR", "occurred_at": "2026-08-26T11:05:00+00:00"},
    ], now=NOW).idempotency_key
    assert success_key != dup_key


def test_inputs_hash_is_order_independent():
    ev = _success_events()
    assert _canonical_hash(ev) == _canonical_hash(list(reversed(ev)))


def test_resolve_batch_returns_one_record_per_order():
    items = [
        (_order(order_id="A"), _success_events()),
        (_order(order_id="B", created_at="2026-08-26T10:00:00+00:00"), [
            {"event_id": "o", "event_type": "order.created", "occurred_at": ORDER_CREATED},
            {"event_id": "f", "event_type": "payment.failed", "occurred_at": NOW},
        ]),
    ]
    records = resolve_batch(items, now=NOW)
    assert len(records) == 2
    assert {r.order_id for r in records} == {"A", "B"}
    assert records[0].resolved_state == ResolvedState.NORMAL_SUCCESS
    assert records[1].resolved_state == ResolvedState.NORMAL_FAILURE
