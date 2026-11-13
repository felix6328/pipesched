
"""同类已完成任务检索估时。数字只来自历史 duration，模型不得口算。"""

from __future__ import annotations

from statistics import median
from typing import Iterable, Optional, Sequence

from pipesched.config import ProjectTemplate
from pipesched.models import EstimateResult, Task


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        raise ValueError("empty")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def similar_pool(
    target: Task,
    pool: Iterable[Task],
    *,
    hours_per_day: float = 8.0,
    match_asset_type: bool = True,
    exclude_ids: Optional[set[str]] = None,
) -> list[Task]:
    """同类 = 同任务类型 +（默认）同资产类型。必须排除自身，防止泄漏。"""
    exclude = set(exclude_ids or ())
    exclude.add(target.id)
    out = []
    for t in pool:
        if t.id in exclude:
            continue
        if not t.is_done:
            continue
        if (t.duration_seconds or 0) <= 0:
            continue
        same_type = (
            (target.task_type_key and t.task_type_key == target.task_type_key)
            or (t.task_type_name == target.task_type_name)
        )
        if not same_type:
            continue
        if match_asset_type and t.asset_type_name != target.asset_type_name:
            continue
        out.append(t)
    return out


def estimate_task(
    target: Task,
    pool: Iterable[Task],
    template: ProjectTemplate,
    *,
    min_similar: Optional[int] = None,
    exclude_ids: Optional[set[str]] = None,
    match_asset_type: bool = True,
) -> EstimateResult:
    min_n = min_similar if min_similar is not None else template.min_similar
    hours = template.hours_per_day
    key = target.task_type_key or target.task_type_name
    try:
        default = template.default_days(target.task_type_key) if target.task_type_key else None
    except KeyError:
        default = None
    if default is None:
        spec = template.task_type_by_name(target.task_type_name)
        default = spec.default_days if spec else None

    sims = similar_pool(
        target, pool, hours_per_day=hours, match_asset_type=match_asset_type, exclude_ids=exclude_ids
    )
    days = [t.duration_days(hours) for t in sims]
    ids = [t.id for t in sims]
    if len(days) >= min_n:
        med = float(median(days))
        p75 = float(percentile(days, 0.75))
        return EstimateResult(
            task_id=target.id,
            task_type_key=key,
            asset_type_name=target.asset_type_name,
            median_days=med,
            p75_days=p75,
            n_similar=len(days),
            similar_task_ids=ids,
            source="similar",
            template_default_days=default,
            low_confidence=False,
            note=f"同类已完成 {len(days)} 条（{target.task_type_name} × {target.asset_type_name}），已排除自身。",
        )
    return EstimateResult(
        task_id=target.id,
        task_type_key=key,
        asset_type_name=target.asset_type_name,
        median_days=float(median(days)) if days else None,
        p75_days=float(percentile(days, 0.75)) if days else None,
        n_similar=len(days),
        similar_task_ids=ids,
        source="template_default",
        template_default_days=default,
        low_confidence=True,
        note=(
            f"同类已完成仅 {len(days)} 条，不足 {min_n}，回退模板默认 "
            f"{default} 人天。低置信。"
            if default is not None
            else f"同类已完成仅 {len(days)} 条，且模板无默认估时。"
        ),
    )
