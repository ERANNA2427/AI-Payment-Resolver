"""Event model for AI-Payment-Resolver.

Defines the payment/checkout event types, the PaymentEvent value object, and the
EventStream container that normalizes a raw event list into a stable, ordered,
deduplicated stream used by the resolver.

Normalization rules (spec §3 step 2, ARCHITECTURE.md §2/§4):
  * deduplicate by ``event_id`` (keep first occurrence),
  * stable-sort by ``(occurred_at, sequence, event_id)``,
  * flag ``out_of_order`` when an event's ``received_at`` precedes its
    ``occurred_at``,
  * drop malformed events (missing/invalid ``event_type`` or missing
    ``event_id``) while recording them in ``dropped`` for audit.

This module is pure data + normalization; it contains no resolution or recovery
logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union


class EventType(str, Enum):
    """Canonical payment/checkout event types (spec §4, §6)."""

    ORDER_CREATED = "order.created"
    CHECKOUT_ABANDONED = "checkout.abandoned"
    PAYMENT_INITIATED = "payment.initiated"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_AUTHORIZED = "payment.authorized"
    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_FAILED = "payment.failed"
    ORDER_PAID = "order.paid"
    REFUND_CREATED = "refund.created"


def parse_ts(value: str) -> float:
    """Parse an ISO-8601 timestamp into a POSIX epoch (UTC-aware).

    Returns 0.0 for empty/invalid input so sorting never raises.
    """
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class PaymentEvent:
    """A single payment/checkout event."""

    event_id: str
    event_type: EventType
    order_id: str
    payment_id: Optional[str] = None
    amount: Optional[int] = None  # minor units
    currency: Optional[str] = None  # 3-letter code
    reason: Optional[str] = None
    occurred_at: str = ""  # ISO-8601
    received_at: Optional[str] = None  # ISO-8601 (for out-of-order detection)
    sequence: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.currency, str):
            self.currency = self.currency.upper()

    def occurred_ts(self) -> float:
        return parse_ts(self.occurred_at)


@dataclass
class EventStream:
    """A normalized, ordered, deduplicated stream of events for one order."""

    order_id: str
    events: list[PaymentEvent] = field(default_factory=list)
    out_of_order: bool = False
    dropped: list[str] = field(default_factory=list)

    @classmethod
    def from_raw(
        cls,
        raw_events: list[Union[dict, PaymentEvent]],
        order_id: Optional[str] = None,
    ) -> "EventStream":
        """Build a stream from raw dicts (or PaymentEvents), normalizing as per
        the rules in the module docstring."""
        parsed: list[PaymentEvent] = []
        seen: set[str] = set()
        dropped: list[str] = []

        for raw in raw_events:
            if isinstance(raw, PaymentEvent):
                event = raw
            else:
                if not isinstance(raw, dict):
                    dropped.append("<non-dict>")
                    continue
                etype_raw = raw.get("event_type")
                try:
                    etype = EventType(etype_raw)
                except (ValueError, TypeError):
                    dropped.append(str(raw.get("event_id", "<unknown>")))
                    continue
                eid = raw.get("event_id")
                if not eid:
                    dropped.append("<no-event-id>")
                    continue
                event = PaymentEvent(
                    event_id=eid,
                    event_type=etype,
                    order_id=raw.get("order_id", order_id or ""),
                    payment_id=raw.get("payment_id"),
                    amount=raw.get("amount"),
                    currency=raw.get("currency"),
                    reason=raw.get("reason"),
                    occurred_at=raw.get("occurred_at", ""),
                    received_at=raw.get("received_at"),
                    sequence=int(raw.get("sequence", 0) or 0),
                )

            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            parsed.append(event)

        parsed.sort(key=lambda e: (e.occurred_ts(), e.sequence, e.event_id))

        out_of_order = False
        for e in parsed:
            if e.received_at and parse_ts(e.received_at) < parse_ts(e.occurred_at):
                out_of_order = True
                break

        oid = order_id or (parsed[0].order_id if parsed else "")
        return cls(order_id=oid, events=parsed, out_of_order=out_of_order, dropped=dropped)
