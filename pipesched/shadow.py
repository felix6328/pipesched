
"""影子模式：扫描出风险卡，只写日志，不写回、不改状态、不发打扰。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from pipesched.models import RiskCard
from pipesched.paths import SHADOW, ensure_data_dirs

TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def write_report(project_name: str, as_of: str, cards: Iterable[RiskCard], extra: dict | None = None) -> Path:
    ensure_data_dirs()
    cards = list(cards)
    day = as_of[:10]
    path = SHADOW / f"{day}_{_slug(project_name)}.json"
    payload = {
        "mode": "shadow",
        "project": project_name,
        "as_of": as_of,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "n_cards": len(cards),
        "cards": [c.to_dict() for c in cards],
        "note": "影子模式：不写回 Kitsu，不改任务状态，不推送打扰。用于事后对照是否真的延期。",
        **(extra or {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _slug(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_|" else "_" for ch in name)
    return keep[:60] or "project"
