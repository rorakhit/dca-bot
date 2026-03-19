"""
audit.py — Audit log read/write.
"""

import json
from datetime import datetime, timezone

from config import AUDIT_LOG_PATH


def write_audit_entry(event: str, data: dict):
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_audit_log() -> list[dict]:
    """Return parsed audit log entries as JSON, newest first."""
    if not AUDIT_LOG_PATH.exists():
        return []
    entries = []
    for line in AUDIT_LOG_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return list(reversed(entries))
