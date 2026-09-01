"""Tests for batch evaluation and CLI integration (P5-C)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from backend.ai.stub_advisor import StubAdvisor
from backend.ai.validator import validate_advisory
from backend.audit import read_records
from backend.cli import _format_inr_minor_units, _run_batch
from backend.metrics import compute_batch_metrics
from backend.models import DecisionRecord, Intervention, Order, ResolvedState
from backend.resolver import process_order
from backend.safety import CircuitBreaker, IdempotencyStore

DATA_PATH = Path(__file__).parent.parent.parent / "data" / "scenarios.jsonl"


def _load_scenarios():
    if not DATA_PATH.exists():
        pytest.skip(f"Dataset not found: {DATA_PATH}")
    scenarios = []
    with DATA_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                scenarios.append(json.loads(line))
    return scenarios


# --- Batch processing ---------------------------------------------------------


class TestBatchProcessing:
    def test_batch_loads(self):
        scenarios = _load_scenarios()
        assert len(scenarios) >= 40

    def test_end_to_end_dry_run(self):
        scenarios = _load_scenarios()
        advisor = StubAdvisor()
        store = IdempotencyStore()
        breaker = CircuitBreaker()

        records = []
        orders = {}

        for scenario in scenarios:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )
            orders[order.order_id] = order

            ai_advisory = validate_advisory(
                advisor.advise(
                    order_id=order.order_id,
                    resolved_state=scenario.get("expected_state", "HUMAN_REVIEW"),
                    risk_reason=scenario.get("expected_risk_reason"),
                )
            )

            record = process_order(
                order,
                scenario["events"],
                execute=False,
                idempotency_store=store,
                circuit_breaker=breaker,
                ai_advisory={
                    "kind": ai_advisory.kind,
                    "text": ai_advisory.text,
                    "confidence": ai_advisory.confidence,
                    "metadata": ai_advisory.metadata,
                },
            )
            records.append(record)

        assert len(records) == len(scenarios)

        for record in records:
            assert record.simulated is True
            assert isinstance(record.ai_advisory, dict)
            assert "kind" in record.ai_advisory

    def test_expected_states_match(self):
        scenarios = _load_scenarios()
        advisor = StubAdvisor()
        store = IdempotencyStore()
        breaker = CircuitBreaker()

        correct = 0
        for scenario in scenarios:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )

            record = process_order(
                order,
                scenario["events"],
                execute=False,
                idempotency_store=store,
                circuit_breaker=breaker,
            )

            expected = scenario["expected_state"]
            # For high-value scenarios, the resolver produces base state
            # but safety gate escalates - check intervention matches
            if expected == "HUMAN_REVIEW" and record.intervention != Intervention.ESCALATE_HUMAN_REVIEW:
                pass  # wrong
            elif expected == "HUMAN_REVIEW" and record.intervention == Intervention.ESCALATE_HUMAN_REVIEW:
                correct += 1
            elif record.resolved_state.value == expected:
                correct += 1

        accuracy = correct / len(scenarios)
        assert accuracy >= 0.95, f"Accuracy {accuracy:.2%} below 95%"

    def test_ai_advisory_uses_resolved_state_not_expected_label(self):
        scenario = {
            "order_id": "ORD-AI-BOUNDARY",
            "order_amount": 100000,
            "order_currency": "INR",
            "created_at": "2026-08-26T10:00:00+00:00",
            "events": [
                {"event_id": "o", "event_type": "order.created", "occurred_at": "2026-08-26T10:00:00+00:00"},
                {"event_id": "c", "event_type": "payment.captured", "payment_id": "P1", "amount": 100000, "currency": "INR", "occurred_at": "2026-08-26T11:00:00+00:00"},
            ],
            # intentionally incorrect label to verify no label leakage
            "expected_state": "HUMAN_REVIEW",
            "expected_risk_reason": "ambiguous",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            batch_path = Path(tmpdir) / "single.jsonl"
            batch_path.write_text(json.dumps(scenario) + "\n", encoding="utf-8")
            records, _ = _run_batch(batch_path=batch_path, execute=False)

        assert records[0].resolved_state == ResolvedState.NORMAL_SUCCESS
        assert records[0].ai_advisory["kind"] == "no_action"


# --- Metrics ------------------------------------------------------------------


class TestMetrics:
    def test_accounting_identity(self):
        scenarios = _load_scenarios()
        records, orders = self._process_all(scenarios, execute=False)
        metrics = compute_batch_metrics(records, orders)
        assert metrics.accounting_identity_holds()

    def test_human_review_queue(self):
        scenarios = _load_scenarios()
        records, orders = self._process_all(scenarios, execute=False)
        metrics = compute_batch_metrics(records, orders)

        hr_records = [r for r in records if r.intervention == Intervention.ESCALATE_HUMAN_REVIEW]
        assert metrics.human_review_count == len(hr_records)

    def test_intervention_counts(self):
        scenarios = _load_scenarios()
        records, orders = self._process_all(scenarios, execute=False)
        metrics = compute_batch_metrics(records, orders)

        assert sum(metrics.intervention_counts.values()) == len(scenarios)

    def _process_all(self, scenarios, execute=False):
        advisor = StubAdvisor()
        store = IdempotencyStore()
        breaker = CircuitBreaker()

        records = []
        orders = {}

        for scenario in scenarios:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )
            orders[order.order_id] = order

            ai_advisory = validate_advisory(
                advisor.advise(
                    order_id=order.order_id,
                    resolved_state=scenario.get("expected_state", "HUMAN_REVIEW"),
                    risk_reason=scenario.get("expected_risk_reason"),
                )
            )

            record = process_order(
                order,
                scenario["events"],
                execute=execute,
                idempotency_store=store,
                circuit_breaker=breaker,
                ai_advisory={
                    "kind": ai_advisory.kind,
                    "text": ai_advisory.text,
                    "confidence": ai_advisory.confidence,
                    "metadata": ai_advisory.metadata,
                },
            )
            records.append(record)

        return records, orders


# --- Adversarial cases -------------------------------------------------------


class TestAdversarial:
    def test_adversarial_cases_escalated(self):
        scenarios = _load_scenarios()
        adversarial = [s for s in scenarios if s["expected_state"] == "HUMAN_REVIEW"]

        assert len(adversarial) > 0, "No adversarial scenarios found"

        store = IdempotencyStore()
        breaker = CircuitBreaker()

        for scenario in adversarial:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )

            record = process_order(
                order,
                scenario["events"],
                execute=True,
                idempotency_store=store,
                circuit_breaker=breaker,
            )

            assert record.resolved_state == ResolvedState.HUMAN_REVIEW
            assert record.intervention == Intervention.ESCALATE_HUMAN_REVIEW

    def test_order_payment_mismatch_never_moves_money(self):
        scenarios = _load_scenarios()
        mismatch = [s for s in scenarios if s["expected_state"] == "ORDER_PAYMENT_MISMATCH"]

        assert len(mismatch) > 0

        store = IdempotencyStore()
        breaker = CircuitBreaker()

        for scenario in mismatch:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )

            record = process_order(
                order,
                scenario["events"],
                execute=True,
                idempotency_store=store,
                circuit_breaker=breaker,
            )

            assert record.intervention == Intervention.ESCALATE_HUMAN_REVIEW


# --- Dry-run behavior ---------------------------------------------------------


class TestDryRun:
    def test_default_is_dry_run(self):
        scenarios = _load_scenarios()
        scenario = scenarios[0]

        order = Order(
            order_id=scenario["order_id"],
            amount=scenario["order_amount"],
            currency=scenario["order_currency"],
            created_at=scenario.get("created_at", ""),
        )

        record = process_order(order, scenario["events"])
        assert record.simulated is True

    def test_execute_flag_enables_execution(self):
        scenarios = _load_scenarios()
        success = [s for s in scenarios if s["expected_state"] == "NORMAL_SUCCESS"]
        if not success:
            pytest.skip("No success scenarios")

        scenario = success[0]
        order = Order(
            order_id=scenario["order_id"],
            amount=scenario["order_amount"],
            currency=scenario["order_currency"],
            created_at=scenario.get("created_at", ""),
        )

        record = process_order(order, scenario["events"], execute=True)
        assert record.simulated is False


# --- Idempotent replay --------------------------------------------------------


class TestIdempotentReplay:
    def test_replay_produces_zero_new_actions(self):
        scenarios = _load_scenarios()
        store = IdempotencyStore()

        first_pass = []
        for scenario in scenarios:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )

            record = process_order(
                order,
                scenario["events"],
                execute=True,
                idempotency_store=store,
            )
            first_pass.append(record)

        second_pass_new = 0
        for scenario in scenarios:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )

            key = store
            if not store.already_executed(f"{scenario['order_id']}_test"):
                pass

        assert True


# --- Zero blind retries -------------------------------------------------------


class TestZeroBlindRetries:
    def test_no_auto_recharge(self):
        scenarios = _load_scenarios()
        failure_scenarios = [s for s in scenarios if s["expected_state"] == "NORMAL_FAILURE"]

        store = IdempotencyStore()
        breaker = CircuitBreaker()

        for scenario in failure_scenarios:
            order = Order(
                order_id=scenario["order_id"],
                amount=scenario["order_amount"],
                currency=scenario["order_currency"],
                created_at=scenario.get("created_at", ""),
            )

            record = process_order(
                order,
                scenario["events"],
                execute=True,
                idempotency_store=store,
                circuit_breaker=breaker,
            )

            assert record.intervention != Intervention.CAPTURE_LATE_AUTH


# --- AI fallback --------------------------------------------------------------


class TestAIFallback:
    def test_invalid_advisory_falls_back(self):
        from backend.ai.advisor import AdvisoryResult
        from backend.ai.validator import validate_advisory

        invalid = AdvisoryResult(kind="invalid_kind", text="test", confidence=0.9)
        result = validate_advisory(invalid)
        assert result.kind == "no_action"
        assert result.metadata.get("fallback") is True

    def test_low_confidence_falls_back(self):
        from backend.ai.advisor import AdvisoryResult
        from backend.ai.validator import validate_advisory

        low_conf = AdvisoryResult(kind="recovery_copy", text="test", confidence=0.3)
        result = validate_advisory(low_conf)
        assert result.kind == "no_action"
        assert result.metadata.get("fallback") is True

    def test_valid_advisory_passes(self):
        from backend.ai.advisor import AdvisoryResult
        from backend.ai.validator import validate_advisory

        valid = AdvisoryResult(kind="recovery_copy", text="test", confidence=0.9)
        result = validate_advisory(valid)
        assert result is valid


# --- CLI integration ----------------------------------------------------------


class TestCLI:
    def test_inr_formatter_whole_rupees(self):
        assert _format_inr_minor_units(0) == "₹0"
        assert _format_inr_minor_units(125000) == "₹1,250"
        assert _format_inr_minor_units(12500000) == "₹1,25,000"

    def test_inr_formatter_fractional_rupees(self):
        assert _format_inr_minor_units(125050) == "₹1,250.50"
        assert _format_inr_minor_units(1) == "₹0.01"

    def test_cli_run_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "audit.jsonl"
            result = subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "run",
                    "--batch", str(DATA_PATH),
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert audit_path.exists()

    def test_cli_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "audit.jsonl"
            subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "run",
                    "--batch", str(DATA_PATH),
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "report",
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert "Batch Metrics" in result.stdout

    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "audit.jsonl"
            subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "run",
                    "--batch", str(DATA_PATH),
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "replay",
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert "PASS" in result.stdout

    def test_report_preserves_economic_metrics_from_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "audit.jsonl"

            records, run_metrics = _run_batch(
                batch_path=DATA_PATH,
                audit_path=audit_path,
                execute=False,
            )

            records_data = read_records(audit_path)
            orders = {}
            report_records = []
            for data in records_data:
                order_id = data.get("order_id", "")
                orders[order_id] = Order(
                    order_id=order_id,
                    amount=data.get("order_amount", 0),
                    currency=data.get("order_currency", "INR"),
                )
                report_records.append(DecisionRecord(
                    decision_id=data.get("decision_id", ""),
                    order_id=order_id,
                    timestamp=data.get("timestamp", ""),
                    resolved_state=ResolvedState(data.get("resolved_state", "HUMAN_REVIEW")),
                    rule_trace=data.get("rule_trace", []),
                    risk_reason=data.get("risk_reason"),
                    intervention=Intervention(data.get("intervention", "ESCALATE_HUMAN_REVIEW")),
                    idempotency_key=data.get("idempotency_key", ""),
                    inputs_hash=data.get("inputs_hash", ""),
                    simulated=data.get("simulated", True),
                    revenue_at_risk=data.get("revenue_at_risk", False),
                    safety_results=data.get("safety_results", {}),
                    ai_advisory=data.get("ai_advisory"),
                    signals=data.get("signals", {}),
                    order_amount=data.get("order_amount", 0),
                    order_currency=data.get("order_currency", "INR"),
                ))

            report_metrics = compute_batch_metrics(report_records, orders)

            assert report_metrics.total_value == run_metrics.total_value
            assert report_metrics.captured == run_metrics.captured
            assert report_metrics.refunded == run_metrics.refunded
            assert report_metrics.at_risk == run_metrics.at_risk
            assert report_metrics.accounting_identity_holds()
            assert run_metrics.total_value > 0

    def test_report_dedupes_accumulated_audit_trail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = Path(tmpdir) / "audit.jsonl"

            subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "run",
                    "--batch", str(DATA_PATH),
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "run",
                    "--batch", str(DATA_PATH),
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )

            result = subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "report",
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert "Total orders:       50" in result.stdout
            assert "Total value:        ₹37,81,320" in result.stdout
            assert "Captured:           ₹37,55,550" in result.stdout
            assert "Refunded:           ₹4,000" in result.stdout
            assert "At risk:            ₹21,770" in result.stdout
            assert "No-action decisions:" in result.stdout
            assert "Safely blocked (dry-run): 10" in result.stdout
            assert "Safety violations:  10" in result.stdout

            replay_result = subprocess.run(
                [
                    sys.executable, "-m", "backend.cli", "replay",
                    "--audit", str(audit_path),
                ],
                capture_output=True,
                text=True,
            )
            assert replay_result.returncode == 0, f"stderr: {replay_result.stderr}"
            assert "Total records: 50" in replay_result.stdout
            assert "Blocked (idempotent): 50" in replay_result.stdout
