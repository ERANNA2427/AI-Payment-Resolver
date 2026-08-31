"""AI advisory validator (spec §7, architecture §6).

Enforces allowlist + confidence floor.
Invalid/low-confidence output falls back to deterministic defaults.
"""

from __future__ import annotations

from typing import Optional

from backend.ai.advisor import AdvisoryResult, AIAdvisor

ALLOWED_KINDS = {
    "reason_normalize",
    "recovery_copy",
    "human_review_summary",
    "no_action",
}

DEFAULT_CONFIDENCE_FLOOR = 0.7


class AdvisoryValidator:
    """Validates AI advisory output."""

    def __init__(self, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR):
        self.confidence_floor = confidence_floor

    def validate(self, advisory: Optional[AdvisoryResult]) -> tuple[bool, str]:
        """Validate advisory. Returns (is_valid, reason)."""
        if advisory is None:
            return True, "no-advisory"

        if not isinstance(advisory, AdvisoryResult):
            return False, "invalid advisory type"

        if advisory.kind not in ALLOWED_KINDS:
            return False, f"kind '{advisory.kind}' not in allowlist"

        if not isinstance(advisory.confidence, (int, float)):
            return False, "confidence must be numeric"

        if advisory.confidence < 0 or advisory.confidence > 1:
            return False, "confidence must be 0-1"

        if advisory.confidence < self.confidence_floor:
            return False, f"confidence {advisory.confidence} below floor {self.confidence_floor}"

        return True, "valid"

    def fallback(self, reason: str = "validation_failed") -> AdvisoryResult:
        """Deterministic fallback advisory."""
        return AdvisoryResult(
            kind="no_action",
            text="AI advisory unavailable. Using deterministic fallback.",
            confidence=1.0,
            metadata={"fallback": True, "reason": reason},
        )


def validate_advisory(
    advisory: Optional[AdvisoryResult],
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> AdvisoryResult:
    """Validate advisory, returning it if valid or a fallback if not."""
    validator = AdvisoryValidator(confidence_floor)
    is_valid, reason = validator.validate(advisory)
    if is_valid:
        return advisory
    return validator.fallback(reason)
