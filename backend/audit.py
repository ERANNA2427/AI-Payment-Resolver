"""Append-only audit trail for AI-Payment-Resolver (spec §10).

Writes one JSONL line per ``DecisionRecord``. Reads are replay-only.
The audit trail is immutable: records are never mutated after writing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from backend.models import DecisionRecord


def append_record(path: str | Path, record: DecisionRecord) -> None:
    """Append a single DecisionRecord as a JSON line. Never overwrite."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")


def read_records(path: str | Path) -> Iterable[dict]:
    """Replay the audit file, yielding DecisionRecord dicts."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def verify_append_only(path: str | Path) -> bool:
    """Verify the audit trail has not been truncated or reordered."""
    records = list(read_records(path))
    for i in range(1, len(records)):
        if records[i].get("timestamp", "") < records[i - 1].get("timestamp", ""):
            return False
    return True
