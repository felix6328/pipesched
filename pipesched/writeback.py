
"""写回护栏：没有 confirmed_by 和有效确认令牌，代码层直接拒绝，不调用 Kitsu 写接口。"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from uuid import uuid4

from pipesched import audit
from pipesched.models import ScheduleProposal

TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class WritebackRefused(PermissionError):
    """无确认或令牌无效。"""


@dataclass
class StoredProposal:
    proposal: ScheduleProposal
    token_hash: str
    created_at: str
    consumed: bool = False


class ProposalStore:
    def __init__(self):
        self._items: dict[str, StoredProposal] = {}

    def issue(self, proposal: ScheduleProposal) -> str:
        token = secrets.token_urlsafe(24)
        proposal.confirm_token_fingerprint = _fp(token)
        self._items[proposal.proposal_id] = StoredProposal(
            proposal=proposal,
            token_hash=_hash(token),
            created_at=datetime.now(TZ).isoformat(timespec="seconds"),
        )
        return token

    def get(self, proposal_id: str) -> Optional[StoredProposal]:
        return self._items.get(proposal_id)

    def all_public(self) -> list[dict]:
        return [s.proposal.to_dict() for s in self._items.values()]


# 进程内默认仓库。演示服务用这个；测试可以 new 一个。
DEFAULT_STORE = ProposalStore()


def _hash(token: str) -> str:
    return hashlib.sha256(f"pipesched-confirm:{token}".encode()).hexdigest()


def _fp(token: str) -> str:
    return _hash(token)[:12]


def confirm_and_write(
    proposal_id: str,
    confirm_token: Optional[str],
    confirmed_by: Optional[str],
    writer: Callable[[str, str, str], dict],
    *,
    store: Optional[ProposalStore] = None,
    allow_dates: bool = True,
) -> dict:
    """writer(task_id, start_date, due_date) 只在校验通过后被调用。

    writer 应由调用方包一层 Gazu：只改 start_date / due_date。
    """
    store = store or DEFAULT_STORE
    payload_base = {
        "proposal_id": proposal_id,
        "confirmed_by": confirmed_by or "",
        "token_present": bool(confirm_token),
    }
    if not (confirmed_by and str(confirmed_by).strip()):
        audit.log("writeback_refused", payload_base, ok=False, reason="missing_confirmed_by")
        raise WritebackRefused("写回被拒绝：缺少 confirmed_by（制片确认人）。")
    if not confirm_token:
        audit.log("writeback_refused", payload_base, ok=False, reason="missing_confirm_token")
        raise WritebackRefused("写回被拒绝：缺少确认令牌。")
    stored = store.get(proposal_id)
    if stored is None:
        audit.log("writeback_refused", payload_base, ok=False, reason="unknown_proposal")
        raise WritebackRefused("写回被拒绝：找不到对应的排期建议。")
    if stored.consumed:
        audit.log("writeback_refused", {**payload_base, "task_id": stored.proposal.task_id}, ok=False, reason="already_consumed")
        raise WritebackRefused("写回被拒绝：该建议已被确认过。")
    if not hmac.compare_digest(stored.token_hash, _hash(confirm_token)):
        audit.log("writeback_refused", {**payload_base, "task_id": stored.proposal.task_id}, ok=False, reason="bad_token")
        raise WritebackRefused("写回被拒绝：确认令牌不匹配。")
    if not allow_dates:
        audit.log("writeback_refused", {**payload_base, "task_id": stored.proposal.task_id}, ok=False, reason="dates_not_allowed_by_template")
        raise WritebackRefused("写回被拒绝：当前模板不允许改日期。")

    p = stored.proposal
    if p.has_conflict():
        # 允许在人明确确认的情况下写回，但必须在日志里记下冲突，界面也应标红。
        pass

    result = writer(p.task_id, p.proposed_start, p.proposed_due)
    stored.consumed = True
    rec = audit.log(
        "writeback_ok",
        {
            **payload_base,
            "task_id": p.task_id,
            "asset_name": p.asset_name,
            "task_type_name": p.task_type_name,
            "current_start": p.current_start,
            "current_due": p.current_due,
            "new_start": p.proposed_start,
            "new_due": p.proposed_due,
            "estimate_source": p.estimate_source,
            "conflicts": p.conflicts,
            "kitsu_result_ids": {
                "start_date": (result or {}).get("start_date"),
                "due_date": (result or {}).get("due_date"),
            },
        },
        ok=True,
        reason="confirmed",
    )
    return rec
