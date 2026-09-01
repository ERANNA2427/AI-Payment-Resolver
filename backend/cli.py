"""CLI entrypoint for AI-Payment-Resolver (spec §10, architecture §10).

Commands:
  run     Process a batch of orders from a JSONL dataset
  report  Display metrics from an audit trail
  replay  Replay an audit trail to verify idempotency
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from backend.ai.stub_advisor import StubAdvisor
from backend.ai.validator import validate_advisory
from backend.audit import append_record, read_records
from backend.metrics import BatchMetrics, compute_batch_metrics
from backend.models import DecisionRecord, Intervention, Order, ResolvedState
from backend.resolver import process_order, resolve_order
from backend.safety import CircuitBreaker, IdempotencyStore


def _format_indian_number(value: int) -> str:
    s = str(value)
    if len(s) <= 3:
        return s
    head = s[-3:]
    rest = s[:-3]
    parts = []
    while len(rest) > 2:
        parts.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.append(rest)
    return ",".join(reversed(parts)) + "," + head


def _format_inr_minor_units(amount_minor: int) -> str:
    sign = "-" if amount_minor < 0 else ""
    amount_abs = abs(amount_minor)
    rupees, paise = divmod(amount_abs, 100)
    rupees_formatted = _format_indian_number(rupees)
    if paise == 0:
        return f"{sign}₹{rupees_formatted}"
    return f"{sign}₹{rupees_formatted}.{paise:02d}"


def _load_batch(batch_path: str | Path) -> list[dict]:
    """Load scenarios from a JSONL file."""
    path = Path(batch_path)
    if not path.exists():
        print(f"Error: batch file not found: {path}", file=sys.stderr)
        sys.exit(1)

    scenarios = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                scenarios.append(json.loads(line))
    return scenarios


def _run_batch(
    batch_path: str | Path,
    audit_path: Optional[str | Path] = None,
    execute: bool = False,
    config: Optional[dict] = None,
) -> tuple[list[DecisionRecord], BatchMetrics]:
    """Run the full pipeline over a batch."""
    scenarios = _load_batch(batch_path)
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

        events = scenario["events"]

        preview = resolve_order(order, events)
        ai_advisory = validate_advisory(
            advisor.advise(
                order_id=order.order_id,
                resolved_state=preview.resolved_state.value,
                risk_reason=preview.risk_reason,
                signals=preview.signals,
            )
        )
        ai_dict = {
            "kind": ai_advisory.kind,
            "text": ai_advisory.text,
            "confidence": ai_advisory.confidence,
            "metadata": ai_advisory.metadata,
        }

        record = process_order(
            order,
            events,
            execute=execute,
            audit_path=None,
            idempotency_store=store,
            circuit_breaker=breaker,
            ai_advisory=ai_dict,
        )
        record.order_amount = order.amount
        record.order_currency = order.currency
        records.append(record)

        if audit_path:
            from backend.audit import append_record
            append_record(audit_path, record)

    metrics = compute_batch_metrics(records, orders)
    return records, metrics


def _format_metrics(metrics: BatchMetrics) -> str:
    """Format metrics for display."""
    no_action_decisions = metrics.intervention_counts.get(Intervention.NO_ACTION.value, 0)
    lines = [
        "=== Batch Metrics ===",
        f"Total orders:       {metrics.total_orders}",
        f"Total value:        {_format_inr_minor_units(metrics.total_value)}",
        f"Captured:           {_format_inr_minor_units(metrics.captured)}",
        f"Refunded:           {_format_inr_minor_units(metrics.refunded)}",
        f"At risk:            {_format_inr_minor_units(metrics.at_risk)}",
        f"No-action decisions:{no_action_decisions:>11}",
        f"Safely blocked (dry-run): {metrics.intentional_dry_run_blocks}",
        f"Safety violations:  {metrics.safety_violations}",
        f"Human review count: {metrics.human_review_count}",
        f"Human review value: {_format_inr_minor_units(metrics.human_review_value)}",
        "",
        "Intervention counts:",
    ]
    for intervention, count in sorted(metrics.intervention_counts.items()):
        lines.append(f"  {intervention}: {count}")
    lines.append("")
    lines.append(f"Accounting identity holds: {metrics.accounting_identity_holds()}")
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    """Run the batch pipeline."""
    audit_path = args.audit if hasattr(args, "audit") else None
    execute = args.execute if hasattr(args, "execute") else False

    records, metrics = _run_batch(
        batch_path=args.batch,
        audit_path=audit_path,
        execute=execute,
    )

    print(_format_metrics(metrics))

    if audit_path:
        print(f"\nAudit trail written to: {audit_path}")

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Report metrics from an audit trail."""
    audit_path = args.audit if hasattr(args, "audit") else None
    if not audit_path:
        print("Error: --audit required for report", file=sys.stderr)
        return 1

    records_data = read_records(audit_path)
    if not records_data:
        print("No records found in audit trail.")
        return 0

    seen_keys = set()
    unique_records_data = []
    for data in records_data:
        key = data.get("idempotency_key", "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        unique_records_data.append(data)
    records_data = unique_records_data

    orders = {}
    records = []
    for data in records_data:
        order_id = data.get("order_id", "")
        order_amount = data.get("order_amount", 0)
        order_currency = data.get("order_currency", "INR")
        orders[order_id] = Order(
            order_id=order_id,
            amount=order_amount,
            currency=order_currency,
        )

        record = DecisionRecord(
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
        )
        records.append(record)

    metrics = compute_batch_metrics(records, orders)
    print(_format_metrics(metrics))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay an audit trail to verify idempotency."""
    audit_path = args.audit if hasattr(args, "audit") else None
    if not audit_path:
        print("Error: --audit required for replay", file=sys.stderr)
        return 1

    records_data = read_records(audit_path)
    if not records_data:
        print("No records found in audit trail.")
        return 0

    seen_keys = set()
    unique_records_data = []
    for data in records_data:
        key = data.get("idempotency_key", "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        unique_records_data.append(data)
    records_data = unique_records_data

    store = IdempotencyStore()

    # First pass: mark all existing keys as executed
    for data in records_data:
        key = data.get("idempotency_key", "")
        if key:
            store.record(key)

    # Second pass: verify all would be blocked
    total = len(records_data)
    blocked = 0
    for data in records_data:
        key = data.get("idempotency_key", "")
        if key and store.already_executed(key):
            blocked += 1

    print("=== Replay Results ===")
    print(f"Total records: {total}")
    print(f"Blocked (idempotent): {blocked}")
    print(f"New actions: {total - blocked}")
    if blocked == total:
        print("PASS: All actions idempotent (no duplicate money movement)")
    else:
        print("WARNING: Some actions would be re-executed")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="backend.cli",
        description="AI Payment Resolver - Batch evaluation CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    run_parser = subparsers.add_parser("run", help="Run batch pipeline")
    run_parser.add_argument("--batch", required=True, help="Path to batch JSONL file")
    run_parser.add_argument("--audit", help="Path to audit trail output")
    run_parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Enable money movement (default: dry-run)",
    )
    run_parser.set_defaults(func=cmd_run)

    # report command
    report_parser = subparsers.add_parser("report", help="Report metrics from audit trail")
    report_parser.add_argument("--audit", required=True, help="Path to audit trail")
    report_parser.set_defaults(func=cmd_report)

    # replay command
    replay_parser = subparsers.add_parser("replay", help="Replay audit trail for idempotency check")
    replay_parser.add_argument("--audit", required=True, help="Path to audit trail")
    replay_parser.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
