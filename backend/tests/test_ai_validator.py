"""Tests for AI advisory validator (P5-A)."""

from __future__ import annotations

import pytest

from backend.ai.advisor import AdvisoryResult, AIAdvisor
from backend.ai.stub_advisor import StubAdvisor, CANONICAL_CATEGORIES
from backend.ai.validator import (
    AdvisoryValidator,
    validate_advisory,
    ALLOWED_KINDS,
    DEFAULT_CONFIDENCE_FLOOR,
)


# --- AdvisoryResult -----------------------------------------------------------


def test_advisory_result_creation():
    result = AdvisoryResult(
        kind="recovery_copy",
        text="Try again.",
        confidence=0.8,
        metadata={"category": "unknown"},
    )
    assert result.kind == "recovery_copy"
    assert result.text == "Try again."
    assert result.confidence == 0.8
    assert result.metadata == {"category": "unknown"}


def test_advisory_result_default_metadata():
    result = AdvisoryResult(kind="no_action", text="OK", confidence=1.0)
    assert result.metadata == {}


# --- StubAdvisor -------------------------------------------------------------


def test_stub_advisor_is_advisor():
    advisor = StubAdvisor()
    assert isinstance(advisor, AIAdvisor)


def test_stub_advisor_normal_failure():
    advisor = StubAdvisor()
    result = advisor.advise("ORD-1", "NORMAL_FAILURE", risk_reason="insufficient balance")
    assert result.kind == "recovery_copy"
    assert "insufficient funds" in result.text.lower() or "funds" in result.text.lower()
    assert result.confidence >= DEFAULT_CONFIDENCE_FLOOR
    assert result.metadata["category"] == "insufficient_funds"


def test_stub_advisor_human_review():
    advisor = StubAdvisor()
    result = advisor.advise("ORD-2", "HUMAN_REVIEW", risk_reason="contradictory evidence")
    assert result.kind == "human_review_summary"
    assert "ORD-2" in result.text
    assert result.confidence >= DEFAULT_CONFIDENCE_FLOOR


def test_stub_advisor_pending():
    advisor = StubAdvisor()
    result = advisor.advise("ORD-3", "PENDING_PAYMENT")
    assert result.kind == "recovery_copy"
    assert "pending" in result.text.lower()
    assert result.confidence >= DEFAULT_CONFIDENCE_FLOOR


def test_stub_advisor_normal_success():
    advisor = StubAdvisor()
    result = advisor.advise("ORD-4", "NORMAL_SUCCESS")
    assert result.kind == "no_action"
    assert result.confidence == 1.0


def test_stub_advisor_deterministic():
    advisor = StubAdvisor()
    r1 = advisor.advise("ORD-5", "NORMAL_FAILURE", risk_reason="bank timeout")
    r2 = advisor.advise("ORD-5", "NORMAL_FAILURE", risk_reason="bank timeout")
    assert r1.kind == r2.kind
    assert r1.text == r2.text
    assert r1.confidence == r2.confidence


def test_stub_advisor_canonical_categories():
    assert "insufficient_funds" in CANONICAL_CATEGORIES
    assert "unknown" in CANONICAL_CATEGORIES
    assert len(CANONICAL_CATEGORIES) == 10


# --- AdvisoryValidator --------------------------------------------------------


def test_validator_valid_advisory():
    validator = AdvisoryValidator()
    advisory = AdvisoryResult(kind="recovery_copy", text="OK", confidence=0.8)
    is_valid, reason = validator.validate(advisory)
    assert is_valid is True
    assert reason == "valid"


def test_validator_rejects_unknown_kind():
    validator = AdvisoryValidator()
    advisory = AdvisoryResult(kind="invalid_kind", text="OK", confidence=0.8)
    is_valid, reason = validator.validate(advisory)
    assert is_valid is False
    assert "not in allowlist" in reason


def test_validator_rejects_low_confidence():
    validator = AdvisoryValidator()
    advisory = AdvisoryResult(kind="recovery_copy", text="OK", confidence=0.5)
    is_valid, reason = validator.validate(advisory)
    assert is_valid is False
    assert "below floor" in reason


def test_validator_rejects_invalid_confidence_range():
    validator = AdvisoryValidator()
    advisory = AdvisoryResult(kind="recovery_copy", text="OK", confidence=1.5)
    is_valid, reason = validator.validate(advisory)
    assert is_valid is False


def test_validator_accepts_none():
    validator = AdvisoryValidator()
    is_valid, reason = validator.validate(None)
    assert is_valid is True


def test_validator_fallback():
    validator = AdvisoryValidator()
    fallback = validator.fallback("test_reason")
    assert fallback.kind == "no_action"
    assert fallback.confidence == 1.0
    assert fallback.metadata["fallback"] is True
    assert fallback.metadata["reason"] == "test_reason"


# --- validate_advisory helper -------------------------------------------------


def test_validate_advisory_returns_valid():
    advisory = AdvisoryResult(kind="recovery_copy", text="OK", confidence=0.9)
    result = validate_advisory(advisory)
    assert result is advisory


def test_validate_advisory_fallback_on_invalid():
    advisory = AdvisoryResult(kind="invalid", text="OK", confidence=0.9)
    result = validate_advisory(advisory)
    assert result.kind == "no_action"
    assert result.metadata["fallback"] is True


def test_validate_advisory_fallback_on_low_confidence():
    advisory = AdvisoryResult(kind="recovery_copy", text="OK", confidence=0.3)
    result = validate_advisory(advisory)
    assert result.kind == "no_action"
    assert result.metadata["fallback"] is True


# --- Allowed kinds ------------------------------------------------------------


def test_allowed_kinds_complete():
    assert "reason_normalize" in ALLOWED_KINDS
    assert "recovery_copy" in ALLOWED_KINDS
    assert "human_review_summary" in ALLOWED_KINDS
    assert "no_action" in ALLOWED_KINDS


def test_default_confidence_floor():
    assert DEFAULT_CONFIDENCE_FLOOR == 0.7
