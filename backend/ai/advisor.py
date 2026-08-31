"""AI advisor interface for AI-Payment-Resolver (spec §7).

AI is advisory-only. It can read events and emit a structured AdvisoryResult;
it CANNOT set ResolvedState or choose an Intervention.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AdvisoryResult:
    """Structured AI advisory output.

    kind: category of advisory (reason_normalize, recovery_copy, human_review_summary)
    text: human-readable advisory text
    confidence: 0.0-1.0 confidence score
    metadata: additional structured data
    """

    kind: str
    text: str
    confidence: float
    metadata: dict = field(default_factory=dict)


class AIAdvisor(ABC):
    """Abstract base for AI advisors.

    All implementations are advisory-only: they produce AdvisoryResult
    but never select or execute money-moving interventions.
    """

    @abstractmethod
    def advise(
        self,
        order_id: str,
        resolved_state: str,
        risk_reason: Optional[str] = None,
        signals: Optional[dict] = None,
    ) -> AdvisoryResult:
        """Generate advisory for a resolved order.

        Must be deterministic and side-effect free.
        """
