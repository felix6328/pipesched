
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Optional


# Kitsu / Zou 把累计实耗放在 duration 字段，单位秒。task["time_spent"] 在 1.1.2 上常为 None。
SECONDS_PER_HOUR = 3600.0


def _date(value) -> Optional[date]:
    if not value:
        return None
    if hasattr(value, "date") and callable(value.date) and not isinstance(value, date):
        return value.date()
    s = str(value)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def seconds_to_days(seconds: Optional[float], hours_per_day: float = 8.0) -> Optional[float]:
    if seconds is None:
        return None
    if hours_per_day <= 0:
        hours_per_day = 8.0
    return float(seconds) / (hours_per_day * SECONDS_PER_HOUR)


def days_to_seconds(days: float, hours_per_day: float = 8.0) -> float:
    return float(days) * hours_per_day * SECONDS_PER_HOUR


@dataclass
class Comment:
    id: str
    task_id: str
    text: str
    created_at: Optional[str] = None
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    task_status_id: Optional[str] = None
    task_status_short: Optional[str] = None
    # 种子标注（仅合成数据有）。评测评论解读时用，扫描产品路径不依赖。
    gold_label: Optional[str] = None  # blocker / sarcasm / misleading / normal / none
    gold_blocker_reason: Optional[str] = None

    @classmethod
    def from_kitsu(cls, raw: dict, task_id: Optional[str] = None) -> "Comment":
        status = raw.get("task_status") or {}
        person = raw.get("person") or {}
        name = person.get("full_name") or " ".join(
            filter(None, [person.get("first_name"), person.get("last_name")])
        ) or None
        return cls(
            id=str(raw.get("id")),
            task_id=str(raw.get("object_id") or raw.get("task_id") or task_id or ""),
            text=raw.get("text") or raw.get("comment") or "",
            created_at=raw.get("created_at"),
            person_id=raw.get("person_id"),
            person_name=name,
            task_status_id=raw.get("task_status_id"),
            task_status_short=status.get("short_name") if isinstance(status, dict) else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    id: str
    project_id: str
    project_name: str
    asset_id: str
    asset_name: str
    asset_type_id: str
    asset_type_name: str
    task_type_id: str
    task_type_name: str
    task_type_key: str  # 模板里的 id，如 modeling；未知则为空
    status_id: str
    status_name: str
    status_short: str
    is_done: bool
    is_retake_status: bool
    is_feedback_request: bool
    is_wip: bool
    estimation_seconds: float
    duration_seconds: float  # 累计实耗，来自 Zou 的 duration
    retake_count: int
    start_date: Optional[date]
    due_date: Optional[date]
    real_start_date: Optional[date]
    end_date: Optional[date]
    done_date: Optional[date]
    last_comment_date: Optional[date]
    assignees: list[str] = field(default_factory=list)
    source: str = "kitsu"  # kitsu | snapshot | synthetic
    comments: list[Comment] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    def estimation_days(self, hours_per_day: float = 8.0) -> Optional[float]:
        if not self.estimation_seconds:
            return None
        return seconds_to_days(self.estimation_seconds, hours_per_day)

    def duration_days(self, hours_per_day: float = 8.0) -> float:
        return seconds_to_days(self.duration_seconds or 0.0, hours_per_day) or 0.0

    def to_public_dict(self) -> dict:
        """给工具 / 界面用的原始字段视图。日期与工时只从这里来，不经模型。"""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "asset_type_name": self.asset_type_name,
            "task_type_name": self.task_type_name,
            "task_type_key": self.task_type_key,
            "status_short": self.status_short,
            "status_name": self.status_name,
            "is_done": self.is_done,
            "estimation_seconds": self.estimation_seconds,
            "duration_seconds": self.duration_seconds,
            "retake_count": self.retake_count,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "real_start_date": self.real_start_date.isoformat() if self.real_start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "done_date": self.done_date.isoformat() if self.done_date else None,
            "last_comment_date": self.last_comment_date.isoformat() if self.last_comment_date else None,
            "source": self.source,
            "field_sources": {
                "estimation_seconds": "kitsu.task.estimation (seconds)",
                "duration_seconds": "kitsu.task.duration (accumulated time spent, seconds; task.time_spent is typically null on Zou 1.1.2)",
                "retake_count": "kitsu.task.retake_count",
                "start_date": "kitsu.task.start_date",
                "due_date": "kitsu.task.due_date",
                "done_date": "kitsu.task.done_date",
                "end_date": "kitsu.task.end_date",
                "last_comment_date": "kitsu.task.last_comment_date",
                "status": "kitsu.task_status.short_name",
            },
        }


@dataclass
class RiskCard:
    task_id: str
    project_id: str
    asset_name: str
    task_type_name: str
    kind: str  # overdue / overrun / high_retake / dep_blocked / sla_overdue
    severity: str  # high / medium / low
    title: str
    evidence: dict
    evidence_fields: list[str]
    comment_citations: list[dict] = field(default_factory=list)
    layer: str = "rule"  # rule | keyword | llm
    as_of: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EstimateResult:
    task_id: str
    task_type_key: str
    asset_type_name: str
    median_days: Optional[float]
    p75_days: Optional[float]
    n_similar: int
    similar_task_ids: list[str]
    source: str  # similar_median / similar_p75 / template_default
    template_default_days: Optional[float]
    low_confidence: bool
    note: str = ""

    def picked_days(self, stat: str = "p75") -> Optional[float]:
        if self.source.startswith("template") or self.n_similar <= 0:
            return self.template_default_days
        if stat == "median":
            return self.median_days
        return self.p75_days

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScheduleProposal:
    proposal_id: str
    project_id: str
    asset_id: str
    asset_name: str
    task_id: str
    task_type_name: str
    task_type_key: str
    current_start: Optional[str]
    current_due: Optional[str]
    proposed_start: str
    proposed_due: str
    estimate_days: Optional[float]
    estimate_source: str
    similar_task_ids: list[str]
    conflicts: list[dict]
    basis: str
    confirm_token_fingerprint: str = ""

    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BlockerExtraction:
    comment_id: str
    task_id: str
    is_blocker: bool
    reason: Optional[str]
    citation_text: str
    citation_ok: bool
    backend: str  # keyword / llm / siliconflow / keyword_fallback / disabled
    raw: Optional[str] = None
    fallback: bool = False
    failure: Optional[str] = None  # exception type or short code, never a secret
    raw_is_blocker: Optional[bool] = None
    raw_citation: str = ""
    raw_reason: Optional[str] = None
    raw_parsed_ok: bool = False
    latency_ms: float = 0.0
    hallucination_flags: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
