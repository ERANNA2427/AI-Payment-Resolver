"""Deterministic offline AI advisor stub (spec §7, architecture §6).

No network, no API keys, no filesystem side effects.
Pure keyword/regex mapping for deterministic output.
"""

from __future__ import annotations

import re
from typing import Optional

from backend.ai.advisor import AdvisoryResult, AIAdvisor

CANONICAL_CATEGORIES = [
    "insufficient_funds",
    "issuer_decline",
    "bank_timeout",
    "network_error",
    "fraud_suspected",
    "amount_mismatch",
    "currency_mismatch",
    "duplicate",
    "late_auth",
    "unknown",
]

_REASON_MAP = {
    "insufficient": "insufficient_funds",
    "balance": "insufficient_funds",
    "decline": "issuer_decline",
    "declined": "issuer_decline",
    "issuer": "issuer_decline",
    "timeout": "bank_timeout",
    "bank": "bank_timeout",
    "network": "network_error",
    "connection": "network_error",
    "fraud": "fraud_suspected",
    "suspicious": "fraud_suspected",
    "amount": "amount_mismatch",
    "mismatch": "amount_mismatch",
    "currency": "currency_mismatch",
    "duplicate": "duplicate",
    "double": "duplicate",
    "late": "late_auth",
    "delayed": "late_auth",
}


class StubAdvisor(AIAdvisor):
    """Deterministic offline advisor."""

    def advise(
        self,
        order_id: str,
        resolved_state: str,
        risk_reason: Optional[str] = None,
        signals: Optional[dict] = None,
    ) -> AdvisoryResult:
        signals = signals or {}

        if resolved_state == "HUMAN_REVIEW":
            return self._human_review_summary(order_id, risk_reason, signals)
        if resolved_state == "NORMAL_FAILURE":
            return self._recovery_copy(order_id, risk_reason, signals)
        if resolved_state in ("LATE_AUTHORIZATION", "DUPLICATE_PAYMENT"):
            return self._recovery_copy(order_id, risk_reason, signals)
        if resolved_state == "PENDING_PAYMENT":
            return self._pending_nudge(order_id, risk_reason, signals)

        return AdvisoryResult(
            kind="no_action",
            text="No action required.",
            confidence=1.0,
            metadata={"category": "none"},
        )

    def _classify_reason(self, risk_reason: Optional[str]) -> str:
        if not risk_reason:
            return "unknown"
        lowered = risk_reason.lower()
        for pattern, category in _REASON_MAP.items():
            if pattern in lowered:
                return category
        return "unknown"

    def _recovery_copy(
        self,
        order_id: str,
        risk_reason: Optional[str],
        signals: dict,
    ) -> AdvisoryResult:
        category = self._classify_reason(risk_reason)

        _COPY = {
            "insufficient_funds": "Your payment could not be completed due to insufficient funds. Please update your payment method and try again.",
            "issuer_decline": "Your bank declined the payment. Please contact your issuer or try a different card.",
            "bank_timeout": "The payment timed out reaching your bank. Please try again in a few minutes.",
            "network_error": "A network error interrupted your payment. Please check your connection and retry.",
            "fraud_suspected": "This payment was flagged for review. Please contact support to verify your identity.",
            "amount_mismatch": "The payment amount did not match the order. Please retry or contact support.",
            "currency_mismatch": "The payment currency did not match the order. Please retry in the correct currency.",
            "duplicate": "A duplicate payment was detected. The excess amount will be refunded automatically.",
            "late_auth": "Your payment was authorized after the order window. Please contact support to complete.",
            "unknown": "Your payment requires attention. Please retry or contact support.",
        }

        return AdvisoryResult(
            kind="recovery_copy",
            text=_COPY.get(category, _COPY["unknown"]),
            confidence=0.8,
            metadata={"category": category},
        )

    def _pending_nudge(
        self,
        order_id: str,
        risk_reason: Optional[str],
        signals: dict,
    ) -> AdvisoryResult:
        return AdvisoryResult(
            kind="recovery_copy",
            text="Your payment is still pending. Please complete the payment to avoid order cancellation.",
            confidence=0.85,
            metadata={"category": "pending"},
        )

    def _human_review_summary(
        self,
        order_id: str,
        risk_reason: Optional[str],
        signals: dict,
    ) -> AdvisoryResult:
        category = self._classify_reason(risk_reason)
        signal_summary = ", ".join(f"{k}={v}" for k, v in signals.items() if v)

        text = (
            f"Order {order_id} requires manual review. "
            f"Reason: {risk_reason or 'unknown'}. "
            f"Signals: {signal_summary or 'none'}. "
            f"Recommendation: investigate before any action."
        )

        return AdvisoryResult(
            kind="human_review_summary",
            text=text,
            confidence=0.75,
            metadata={"category": category, "signals": signals},
        )
