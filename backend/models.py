"""Domain model for AI-Payment-Resolver.

Defines the value types, enumerations, and record shapes used across the
resolver. Money is always represented as integer minor units (paise) with an
ISO-4217-style 3-letter currency code, never as a float, so all arithmetic is
exact and deterministic.

See PROJECT_SPEC.md (§5, §8, §10) and ARCHITECTURE.md (§2, §D) for the
canonical definitions. This module is pure data + lightweight validation; it
contains no resolution, recovery, or AI logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResolvedState(str, Enum):
    """The single terminal classification produced for each order (spec §5)."""

    NORMAL_SUCCESS = "NORMAL_SUCCESS"
    NORMAL_FAILURE = "NORMAL_FAILURE"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    LATE_AUTHORIZATION = "LATE_AUTHORIZATION"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    ORDER_PAYMENT_MISMATCH = "ORDER_PAYMENT_MISMATCH"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class Intervention(str, Enum):
    """Bounded recovery actions. Only CAPTURE_LATE_AUTH and REFUND_DUPLICATE
    may move money, and both pass the full safety gate (spec §8, §9)."""

    NO_ACTION = "NO_ACTION"
    SEND_RECOVERY_LINK = "SEND_RECOVERY_LINK"
    SEND_PENDING_NUDGE = "SEND_PENDING_NUDGE"
    RECONCILE_PENDING = "RECONCILE_PENDING"
    CAPTURE_LATE_AUTH = "CAPTURE_LATE_AUTH"
    VOID_LATE_AUTH = "VOID_LATE_AUTH"
    REFUND_DUPLICATE = "REFUND_DUPLICATE"
    ESCALATE_HUMAN_REVIEW = "ESCALATE_HUMAN_REVIEW"


@dataclass(frozen=True)
class Money:
    """Integer minor units + 3-letter currency. Never a float."""

    amount: int  # minor units (e.g. paise)
    currency: str  # 3-letter ISO-4217 code

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise TypeError("amount must be an integer (minor units)")
        if self.amount < 0:
            raise ValueError("amount must be >= 0")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a 3-letter code")
        # Normalize to uppercase so the later "exact currency match" check is
        # case-insensitive and deterministic.
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass(frozen=True)
class Order:
    """The order a payment lifecycle belongs to."""

    order_id: str
    amount: int  # expected order value in minor units
    currency: str  # 3-letter ISO-4217 code
    created_at: str = ""  # ISO-8601; used for late-authorization windowing

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id:
            raise ValueError("order_id must be a non-empty string")
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise TypeError("amount must be an integer (minor units)")
        if self.amount < 0:
            raise ValueError("amount must be >= 0")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a 3-letter code")
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass
class RuleStep:
    """One entry in the explainable resolution trace (spec §6)."""

    rule_id: str
    matched: bool
    detail: str = ""


@dataclass
class ResolutionResult:
    """Output of the pure resolver: exactly one ResolvedState + its trace."""

    order_id: str
    resolved_state: ResolvedState
    rule_trace: list[RuleStep] = field(default_factory=list)
    signals: dict = field(default_factory=dict)


@dataclass
class RecoveryResult:
    """Outcome of executing an Intervention via the simulated gateway."""

    intervention: Intervention
    status: str  # "simulated" | "executed" | "blocked" | "skipped"
    amount: Optional[int] = None
    currency: Optional[str] = None
    idempotency_key: Optional[str] = None
    detail: str = ""


@dataclass
class DecisionRecord:
    """Append-only audit record written per decision (spec §10)."""

    decision_id: str
    order_id: str
    timestamp: str
    resolved_state: ResolvedState
    rule_trace: list[RuleStep]
    risk_reason: Optional[str]
    intervention: Intervention
    idempotency_key: str
    inputs_hash: str
    simulated: bool = True
    revenue_at_risk: bool = False
    safety_results: dict = field(default_factory=dict)
    ai_advisory: Optional[dict] = None
    signals: dict = field(default_factory=dict)
    order_amount: int = 0
    order_currency: str = "INR"

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "order_id": self.order_id,
            "timestamp": self.timestamp,
            "resolved_state": self.resolved_state.value,
            "rule_trace": [
                {"rule_id": s.rule_id, "matched": s.matched, "detail": s.detail}
                for s in self.rule_trace
            ],
            "risk_reason": self.risk_reason,
            "intervention": self.intervention.value,
            "idempotency_key": self.idempotency_key,
            "inputs_hash": self.inputs_hash,
            "simulated": self.simulated,
            "revenue_at_risk": self.revenue_at_risk,
            "safety_results": self.safety_results,
            "ai_advisory": self.ai_advisory,
            "signals": self.signals,
            "order_amount": self.order_amount,
            "order_currency": self.order_currency,
        }


@dataclass
class ResolutionConfig:
    """Injected configuration for the resolver (no hidden global state)."""

    late_auth_window_seconds: int = 86400  # 1 day
    high_value_threshold: int = 50_000_000  # 500,000.00 INR in paise
