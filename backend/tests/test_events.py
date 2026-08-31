"""Focused tests for backend/events.py (event model + normalization, P1)."""

from backend.events import EventType, EventStream, PaymentEvent, parse_ts


def _raw(event_id, event_type, occurred_at=None, received_at=None, **kw):
    base = {"event_id": event_id, "event_type": event_type, "order_id": "ORD-1"}
    if occurred_at is not None:
        base["occurred_at"] = occurred_at
    if received_at is not None:
        base["received_at"] = received_at
    base.update(kw)
    return base


def test_event_type_values_match_spec():
    assert EventType.PAYMENT_CAPTURED.value == "payment.captured"
    assert EventType.ORDER_PAID.value == "order.paid"
    assert {e.value for e in EventType} == {
        "order.created",
        "checkout.abandoned",
        "payment.initiated",
        "payment.pending",
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "order.paid",
        "refund.created",
    }


def test_parse_ts_handles_edge_cases():
    assert parse_ts("") == 0.0
    assert parse_ts("not-a-date") == 0.0
    ts = parse_ts("2026-08-26T10:00:00+00:00")
    assert ts > 0
    assert parse_ts("2026-08-26T10:00:00Z") == ts  # Zulu suffix supported


def test_dedupe_by_event_id_keeps_first():
    raw = [
        _raw("a", "payment.captured", occurred_at="2026-08-26T11:00:00+00:00"),
        _raw("a", "payment.captured", occurred_at="2026-08-26T12:00:00+00:00"),
        _raw("b", "payment.captured", occurred_at="2026-08-26T10:00:00+00:00"),
    ]
    stream = EventStream.from_raw(raw, order_id="ORD-1")
    ids = [e.event_id for e in stream.events]
    # Dedup keeps the first occurrence of "a" (11:00), and the stream is then
    # stable-sorted by occurred_at (b=10:00 before a=11:00).
    assert set(ids) == {"a", "b"}
    a_ev = next(e for e in stream.events if e.event_id == "a")
    assert a_ev.occurred_at == "2026-08-26T11:00:00+00:00"
    assert ids == ["b", "a"]


def test_stable_sort_by_occurred_at():
    raw = [
        _raw("late", "payment.captured", occurred_at="2026-08-26T12:00:00+00:00"),
        _raw("early", "payment.captured", occurred_at="2026-08-26T09:00:00+00:00"),
        _raw("mid", "payment.captured", occurred_at="2026-08-26T10:00:00+00:00"),
    ]
    stream = EventStream.from_raw(raw, order_id="ORD-1")
    assert [e.event_id for e in stream.events] == ["early", "mid", "late"]


def test_out_of_order_flag():
    ooo = EventStream.from_raw(
        [_raw("x", "payment.captured", occurred_at="2026-08-26T10:00:00+00:00",
              received_at="2026-08-26T09:00:00+00:00")],
        order_id="ORD-1",
    )
    assert ooo.out_of_order is True

    ok = EventStream.from_raw(
        [_raw("y", "payment.captured", occurred_at="2026-08-26T10:00:00+00:00",
              received_at="2026-08-26T10:05:00+00:00")],
        order_id="ORD-1",
    )
    assert ok.out_of_order is False


def test_invalid_events_dropped_and_recorded():
    raw = [
        {"event_id": "ok", "event_type": "payment.captured"},
        {"event_id": "bad", "event_type": "payment.teleported"},  # unknown type
        {"event_type": "payment.captured"},  # missing event_id
        "not-a-dict",
    ]
    stream = EventStream.from_raw(raw, order_id="ORD-1")
    assert [e.event_id for e in stream.events] == ["ok"]
    assert "bad" in stream.dropped
    assert "<no-event-id>" in stream.dropped
    assert "<non-dict>" in stream.dropped


def test_accepts_payment_event_objects():
    ev = PaymentEvent(
        event_id="p1",
        event_type=EventType.PAYMENT_CAPTURED,
        order_id="ORD-1",
        amount=100,
        currency="INR",
    )
    stream = EventStream.from_raw([ev], order_id="ORD-1")
    assert len(stream.events) == 1
    assert stream.events[0].amount == 100
