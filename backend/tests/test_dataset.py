"""Tests for synthetic evaluation dataset (P5-B)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DATA_PATH = Path(__file__).parent.parent.parent / "data" / "scenarios.jsonl"

REQUIRED_STATES = {
    "NORMAL_SUCCESS",
    "NORMAL_FAILURE",
    "PENDING_PAYMENT",
    "LATE_AUTHORIZATION",
    "DUPLICATE_PAYMENT",
    "ORDER_PAYMENT_MISMATCH",
    "HUMAN_REVIEW",
}


def _load_scenarios():
    if not DATA_PATH.exists():
        pytest.fail(f"Dataset not found: {DATA_PATH}")
    scenarios = []
    with DATA_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            scenarios.append(json.loads(line))
    return scenarios


def test_dataset_exists():
    assert DATA_PATH.exists(), f"Dataset file missing: {DATA_PATH}"


def test_dataset_has_scenarios():
    scenarios = _load_scenarios()
    assert len(scenarios) >= 40, f"Expected at least 40 scenarios, got {len(scenarios)}"


def test_each_scenario_has_required_fields():
    scenarios = _load_scenarios()
    required_fields = {"order_id", "order_amount", "order_currency", "created_at", "events", "expected_state", "expected_risk_reason"}
    for s in scenarios:
        missing = required_fields - set(s.keys())
        assert not missing, f"Scenario {s.get('order_id', '?')} missing fields: {missing}"


def test_each_scenario_has_valid_events():
    scenarios = _load_scenarios()
    for s in scenarios:
        assert isinstance(s["events"], list), f"{s['order_id']}: events must be a list"
        assert len(s["events"]) > 0, f"{s['order_id']}: events must not be empty"
        for e in s["events"]:
            assert "event_id" in e, f"{s['order_id']}: event missing event_id"
            assert "event_type" in e, f"{s['order_id']}: event missing event_type"


def test_expected_states_are_valid():
    scenarios = _load_scenarios()
    for s in scenarios:
        assert s["expected_state"] in REQUIRED_STATES, f"{s['order_id']}: invalid state {s['expected_state']}"


def test_all_represented_states_present():
    scenarios = _load_scenarios()
    found_states = {s["expected_state"] for s in scenarios}
    missing = REQUIRED_STATES - found_states
    assert not missing, f"Missing states in dataset: {missing}"


def test_scenario_ids_are_unique():
    scenarios = _load_scenarios()
    ids = [s["order_id"] for s in scenarios]
    assert len(ids) == len(set(ids)), "Duplicate order_ids found"


def test_order_amounts_are_positive():
    scenarios = _load_scenarios()
    for s in scenarios:
        assert isinstance(s["order_amount"], int), f"{s['order_id']}: amount must be int"
        assert s["order_amount"] > 0, f"{s['order_id']}: amount must be positive"


def test_currencies_are_valid():
    scenarios = _load_scenarios()
    valid_currencies = {"INR", "USD", "EUR", "GBP"}
    for s in scenarios:
        assert s["order_currency"] in valid_currencies, f"{s['order_id']}: invalid currency {s['order_currency']}"
