
"""规则风险引擎。不用 LLM。所有数字必须来自 Task 上已由 API 填好的字段。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from pipesched.calendarutil import as_date, is_workday
from pipesched.config import ProjectTemplate
from pipesched.models import RiskCard, Task


RULE_KINDS = ("overdue", "overrun", "high_retake", "dep_blocked", "sla_overdue")


def _as_of(as_of) -> date:
    return as_date(as_of)


def _done(task: Task) -> bool:
    return bool(task.is_done) or task.status_short in {"done", "approved"}


def rule_overdue(task: Task, template: ProjectTemplate, as_of) -> Optional[RiskCard]:
    if _done(task) or not task.due_date:
        return None
    grace = int(template.risk.overdue_grace_days)
    deadline = task.due_date + timedelta(days=grace)
    if _as_of(as_of) <= deadline:
        return None
    days_late = (_as_of(as_of) - task.due_date).days
    return RiskCard(
        task_id=task.id,
        project_id=task.project_id,
        asset_name=task.asset_name,
        task_type_name=task.task_type_name,
        kind="overdue",
        severity="high",
        title=f"{task.asset_name} / {task.task_type_name} 已逾期 {days_late} 天",
        evidence={
            "due_date": task.due_date.isoformat(),
            "as_of": _as_of(as_of).isoformat(),
            "grace_days": grace,
            "status_short": task.status_short,
            "days_late": days_late,
            "is_done": task.is_done,
        },
        evidence_fields=["due_date", "status_short", "is_done"],
        layer="rule",
        as_of=_as_of(as_of).isoformat(),
    )


def rule_overrun(task: Task, template: ProjectTemplate, as_of) -> Optional[RiskCard]:
    if _done(task):
        return None
    if not task.estimation_seconds or task.estimation_seconds <= 0:
        return None
    ratio = (task.duration_seconds or 0) / task.estimation_seconds
    if ratio < template.risk.overrun_ratio:
        return None
    return RiskCard(
        task_id=task.id,
        project_id=task.project_id,
        asset_name=task.asset_name,
        task_type_name=task.task_type_name,
        kind="overrun",
        severity="high" if ratio >= template.risk.overrun_ratio * 1.25 else "medium",
        title=f"{task.asset_name} / {task.task_type_name} 实耗已达估时的 {ratio:.2f} 倍",
        evidence={
            "duration_seconds": task.duration_seconds,
            "estimation_seconds": task.estimation_seconds,
            "ratio": round(ratio, 4),
            "threshold": template.risk.overrun_ratio,
            "duration_days": task.duration_days(template.hours_per_day),
            "estimation_days": task.estimation_days(template.hours_per_day),
            "field_note": "实耗取 Zou 的 duration（秒），不是 task.time_spent",
        },
        evidence_fields=["duration_seconds", "estimation_seconds"],
        layer="rule",
        as_of=_as_of(as_of).isoformat(),
    )


def rule_high_retake(task: Task, template: ProjectTemplate, as_of) -> Optional[RiskCard]:
    if _done(task):
        return None
    if (task.retake_count or 0) < template.risk.retake_threshold:
        return None
    return RiskCard(
        task_id=task.id,
        project_id=task.project_id,
        asset_name=task.asset_name,
        task_type_name=task.task_type_name,
        kind="high_retake",
        severity="high" if task.retake_count >= template.risk.retake_threshold + 1 else "medium",
        title=f"{task.asset_name} / {task.task_type_name} 返修 {task.retake_count} 次，达到阈值 {template.risk.retake_threshold}",
        evidence={
            "retake_count": task.retake_count,
            "threshold": template.risk.retake_threshold,
            "status_short": task.status_short,
        },
        evidence_fields=["retake_count"],
        layer="rule",
        as_of=_as_of(as_of).isoformat(),
    )


def predecessors_of(task: Task, all_tasks: Iterable[Task], template: ProjectTemplate) -> list[Task]:
    spec = template.task_type_by_name(task.task_type_name) or (
        template.task_type(task.task_type_key) if task.task_type_key else None
    )
    if not spec or not spec.depends_on:
        return []
    pred_keys = set(spec.depends_on)
    pred_names = set()
    for k in pred_keys:
        try:
            pred_names.add(template.task_type(k).name)
        except KeyError:
            pred_names.add(k)
    out = []
    for t in all_tasks:
        if t.asset_id != task.asset_id:
            continue
        if t.id == task.id:
            continue
        if (t.task_type_key in pred_keys) or (t.task_type_name in pred_names) or (t.task_type_name in pred_keys):
            out.append(t)
    return out


def rule_dep_blocked(task: Task, all_tasks: Iterable[Task], template: ProjectTemplate, as_of) -> Optional[RiskCard]:
    if _done(task):
        return None
    # 前序未完成，且本任务开始日已到（或没有开始日但已在进行）
    asof = _as_of(as_of)
    started = False
    if task.start_date and task.start_date <= asof:
        started = True
    if task.is_wip or task.status_short in {"wip", "retake", "wfa"}:
        started = True
    if not started:
        return None
    preds = predecessors_of(task, all_tasks, template)
    unfinished = [p for p in preds if not _done(p)]
    if not unfinished:
        return None
    ev = [
        {
            "predecessor_id": p.id,
            "predecessor_asset": p.asset_name,
            "predecessor_type": p.task_type_name,
            "status_short": p.status_short,
            "is_done": p.is_done,
            "due_date": p.due_date.isoformat() if p.due_date else None,
        }
        for p in unfinished
    ]
    names = ", ".join(f"{p.task_type_name}({p.status_short})" for p in unfinished)
    return RiskCard(
        task_id=task.id,
        project_id=task.project_id,
        asset_name=task.asset_name,
        task_type_name=task.task_type_name,
        kind="dep_blocked",
        severity="high",
        title=f"{task.asset_name} / {task.task_type_name} 前序未完成：{names}",
        evidence={
            "start_date": task.start_date.isoformat() if task.start_date else None,
            "as_of": asof.isoformat(),
            "unfinished_predecessors": ev,
            "dependency_source": "template DAG + 同资产实际任务状态（不是模型推断）",
        },
        evidence_fields=["start_date", "unfinished_predecessors"],
        layer="rule",
        as_of=asof.isoformat(),
    )


def rule_sla(task: Task, template: ProjectTemplate, as_of) -> Optional[RiskCard]:
    if _done(task):
        return None
    if not task.is_feedback_request and task.status_short != "wfa":
        return None
    asof = _as_of(as_of)
    anchor = task.last_comment_date or task.real_start_date or task.start_date
    if not anchor:
        return None
    waited = (asof - anchor).days
    if waited < template.risk.review_sla_days:
        return None
    return RiskCard(
        task_id=task.id,
        project_id=task.project_id,
        asset_name=task.asset_name,
        task_type_name=task.task_type_name,
        kind="sla_overdue",
        severity="medium",
        title=f"{task.asset_name} / {task.task_type_name} 提交评审已 {waited} 天，超过 SLA {template.risk.review_sla_days} 天",
        evidence={
            "status_short": task.status_short,
            "is_feedback_request": task.is_feedback_request,
            "last_comment_date": task.last_comment_date.isoformat() if task.last_comment_date else None,
            "waited_days": waited,
            "sla_days": template.risk.review_sla_days,
        },
        evidence_fields=["status_short", "last_comment_date"],
        layer="rule",
        as_of=asof.isoformat(),
    )


def scan_task(
    task: Task,
    all_tasks: Iterable[Task],
    template: ProjectTemplate,
    as_of,
) -> list[RiskCard]:
    cards = []
    for fn in (
        lambda t: rule_overdue(t, template, as_of),
        lambda t: rule_overrun(t, template, as_of),
        lambda t: rule_high_retake(t, template, as_of),
        lambda t: rule_dep_blocked(t, all_tasks, template, as_of),
        lambda t: rule_sla(t, template, as_of),
    ):
        card = fn(task)
        if card:
            cards.append(card)
    return cards


def scan_project(
    tasks: Iterable[Task],
    template: ProjectTemplate,
    as_of,
) -> list[RiskCard]:
    tasks = list(tasks)
    cards: list[RiskCard] = []
    for t in tasks:
        cards.extend(scan_task(t, tasks, template, as_of))
    order = {k: i for i, k in enumerate(RULE_KINDS)}
    sev = {"high": 0, "medium": 1, "low": 2}
    cards.sort(key=lambda c: (sev.get(c.severity, 9), order.get(c.kind, 9), c.asset_name, c.task_type_name))
    return cards
