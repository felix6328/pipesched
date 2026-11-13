
"""按模板 DAG + 同类估时，在工作日历和周产能约束下给出建议档期。不做多人多技能 APS。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional
from uuid import uuid4

from pipesched.calendarutil import add_work_hours, as_date, iso_week_key, is_workday, next_workday
from pipesched.config import ProjectTemplate
from pipesched.estimate import estimate_task
from pipesched.models import EstimateResult, ScheduleProposal, Task
from pipesched.risk import predecessors_of


def _week_hours_ok(week_load: dict[str, float], day: date, add_hours: float, cap: float) -> bool:
    if cap <= 0:
        return True
    return week_load[iso_week_key(day)] + add_hours <= cap + 1e-6


def _place(
    start_min: date,
    hours: float,
    template: ProjectTemplate,
    week_load: dict[str, float],
) -> tuple[date, date]:
    workweek = template.workweek
    holidays = template.holidays
    hpd = template.hours_per_day
    cap = template.team_weekly_hours
    start = next_workday(start_min, workweek, holidays)
    # 若本周已满，推到下一工作日直到某周能放下（粗粒度：按任务整体放入起始周，超了就顺延）
    # 简单策略：从 start 起找一个工作日，使从该日起占用 hours 所跨的每一周都不超额。
    for _ in range(400):
        due = add_work_hours(start, hours, hpd, workweek, holidays)
        # 估算每周占用：均匀按工作日
        ok = True
        # walk days
        from datetime import timedelta
        cur = start
        remaining = hours
        trial = defaultdict(float)
        while remaining > 1e-9:
            if is_workday(cur, workweek, holidays):
                take = min(hpd, remaining)
                trial[iso_week_key(cur)] += take
                remaining -= take
            cur += timedelta(days=1)
            if (cur - start).days > 800:
                break
        for wk, h in trial.items():
            if week_load[wk] + h > cap + 1e-6:
                ok = False
                break
        if ok:
            for wk, h in trial.items():
                week_load[wk] += h
            return start, due
        start = next_workday(start + timedelta(days=1), workweek, holidays)
    due = add_work_hours(start, hours, hpd, workweek, holidays)
    return start, due


def propose_for_asset(
    asset_id: str,
    asset_name: str,
    project_id: str,
    tasks_of_asset: list[Task],
    all_pool: Iterable[Task],
    template: ProjectTemplate,
    as_of,
    *,
    week_load: Optional[dict[str, float]] = None,
    exclude_ids: Optional[set[str]] = None,
    schedule_stat: Optional[str] = None,
) -> list[ScheduleProposal]:
    """对一个资产按模板拓扑顺序提议 start/due。已完成任务不改，只把它的结束日当约束。"""
    asof = as_date(as_of)
    stat = schedule_stat or template.schedule_stat
    by_key: dict[str, Task] = {}
    for t in tasks_of_asset:
        key = t.task_type_key or (template.task_type_by_name(t.task_type_name).id if template.task_type_by_name(t.task_type_name) else None)
        if key:
            t.task_type_key = key
            by_key[key] = t

    # 已占用产能：已排或进行中任务按已有日期计入（粗）
    if week_load is None:
        week_load = defaultdict(float)

    proposed_dates: dict[str, tuple[date, date]] = {}
    proposals: list[ScheduleProposal] = []

    for key in template.topo_order():
        task = by_key.get(key)
        if task is None:
            continue
        if task.is_done:
            end = task.done_date or task.end_date or task.due_date
            start = task.real_start_date or task.start_date or end
            if start and end:
                proposed_dates[key] = (start, end)
            continue

        est = estimate_task(task, all_pool, template, exclude_ids=exclude_ids)
        days = est.picked_days(stat)
        if days is None:
            days = est.template_default_days or 1.0
        hours = days * template.hours_per_day
        source = est.source if not est.low_confidence else "template_default"
        if source == "similar":
            source = f"similar_{stat}"

        # 约束：不得早于所有前序的建议/实际结束日的次一工作日，也不得早于 as_of
        start_min = asof
        pred_ends = []
        for pred_key in template.predecessors(key):
            if pred_key in proposed_dates:
                pred_ends.append((pred_key, proposed_dates[pred_key][1]))
        if pred_ends:
            latest = max(d for _, d in pred_ends)
            from pipesched.calendarutil import next_workday
            start_min = max(start_min, next_workday(latest + timedelta(days=1), template.workweek, template.holidays))

        start, due = _place(start_min, hours, template, week_load)
        proposed_dates[key] = (start, due)

        conflicts = []
        for pred_key, pred_end in pred_ends:
            if start <= pred_end:
                conflicts.append({
                    "type": "dependency",
                    "predecessor": pred_key,
                    "predecessor_end": pred_end.isoformat(),
                    "proposed_start": start.isoformat(),
                    "message": f"建议开始日 {start.isoformat()} 不晚于前序 {pred_key} 结束日 {pred_end.isoformat()}（应标红，不得静默覆盖）",
                })

        # 若现有日期本身已经和依赖冲突，也标出来
        if task.start_date and pred_ends:
            for pred_key, pred_end in pred_ends:
                if task.start_date <= pred_end:
                    conflicts.append({
                        "type": "existing_dependency",
                        "predecessor": pred_key,
                        "predecessor_end": pred_end.isoformat(),
                        "current_start": task.start_date.isoformat(),
                        "message": f"现有开始日 {task.start_date.isoformat()} 已与前序 {pred_key} 冲突",
                    })

        basis = (
            f"估时 {days:.2f} 人天，来源 {source}"
            + (f"，参照 {len(est.similar_task_ids)} 条同类任务" if est.similar_task_ids else "，模板默认（低置信）")
            + f"；依赖约束与每周产能 {template.team_weekly_hours} 人时。"
        )
        proposals.append(ScheduleProposal(
            proposal_id=str(uuid4()),
            project_id=project_id,
            asset_id=asset_id,
            asset_name=asset_name,
            task_id=task.id,
            task_type_name=task.task_type_name,
            task_type_key=key,
            current_start=task.start_date.isoformat() if task.start_date else None,
            current_due=task.due_date.isoformat() if task.due_date else None,
            proposed_start=start.isoformat(),
            proposed_due=due.isoformat(),
            estimate_days=round(days, 3),
            estimate_source=source,
            similar_task_ids=list(est.similar_task_ids),
            conflicts=conflicts,
            basis=basis,
        ))
    return proposals


def propose_project(
    tasks: list[Task],
    template: ProjectTemplate,
    as_of,
    *,
    only_open: bool = True,
    schedule_stat: Optional[str] = None,
) -> list[ScheduleProposal]:
    """对项目内每个尚未完成全部工序的资产出一套建议。同一 week_load 跨资产共享产能。"""
    week_load: dict[str, float] = defaultdict(float)
    # 先把已完成 / 已排且正在进行的工时计入产能（按现有 start-due）
    for t in tasks:
        if t.start_date and t.due_date and not t.is_done:
            hours = t.estimation_days(template.hours_per_day) or t.duration_days(template.hours_per_day)
            # 粗计入起始周
            week_load[iso_week_key(t.start_date)] += (hours or 0) * template.hours_per_day

    by_asset: dict[str, list[Task]] = defaultdict(list)
    names: dict[str, str] = {}
    pids: dict[str, str] = {}
    for t in tasks:
        by_asset[t.asset_id].append(t)
        names[t.asset_id] = t.asset_name
        pids[t.asset_id] = t.project_id

    out: list[ScheduleProposal] = []
    for asset_id, group in sorted(by_asset.items(), key=lambda kv: names.get(kv[0], "")):
        if only_open and all(t.is_done for t in group):
            continue
        out.extend(propose_for_asset(
            asset_id,
            names[asset_id],
            pids[asset_id],
            group,
            tasks,
            template,
            as_of,
            week_load=week_load,
            schedule_stat=schedule_stat,
        ))
    return out
