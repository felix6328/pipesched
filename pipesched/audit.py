
"""写回审计日志。每次尝试（含被拒绝的）都落盘。"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from pipesched.paths import AUDIT, ensure_data_dirs

TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def audit_path() -> Path:
    ensure_data_dirs()
    return AUDIT / "writeback.jsonl"


def log(event: str, payload: dict, *, ok: bool, reason: str = "") -> dict:
    rec = {
        "ts": _now(),
        "event": event,
        "ok": ok,
        "reason": reason,
        **payload,
    }
    path = audit_path()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_all(limit: int = 200) -> list[dict]:
    path = audit_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]
