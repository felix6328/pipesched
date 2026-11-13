"""工作日历：只在模板规定的工作日上落日期。不做节假日以外的复杂日历（明确非目标）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable


def as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if value is None:
        raise ValueError("date is None")
    s = str(value)[:10]
    return date.fromisoformat(s)


def is_workday(d: date, workweek: Iterable[int] = (0, 1, 2, 3, 4), holidays: Iterable = ()) -> bool:
    holiday_set = {as_date(h) for h in holidays}
    return d.weekday() in set(workweek) and d not in holiday_set


def next_workday(d: date, workweek=(0, 1, 2, 3, 4), holidays=()) -> date:
    cur = d
    for _ in range(366 * 3):
        if is_workday(cur, workweek, holidays):
            return cur
        cur += timedelta(days=1)
    raise RuntimeError("no workday found in 3 years")


def add_work_hours(
    start: date,
    hours: float,
    hours_per_day: float = 8.0,
    workweek=(0, 1, 2, 3, 4),
    holidays=(),
) -> date:
    """从 start 起占用 hours 个工时，返回工作完成的那一个工作日（含当日）。

    hours<=0 时返回 start 本身（若 start 不是工作日则推到下一个）。
    """
    start = next_workday(as_date(start), workweek, holidays)
    if hours <= 0:
        return start
    remaining = float(hours)
    cur = start
    # 完成日是耗尽工时的那天
    while remaining > 0:
        if is_workday(cur, workweek, holidays):
            remaining -= hours_per_day
            if remaining <= 1e-9:
                return cur
        cur += timedelta(days=1)
        if (cur - start).days > 366 * 3:
            raise RuntimeError("add_work_hours overflow")
    return cur


def work_hours_between(
    start: date,
    end: date,
    hours_per_day: float = 8.0,
    workweek=(0, 1, 2, 3, 4),
    holidays=(),
) -> float:
    """闭区间 [start, end] 内的工作小时。"""
    a, b = as_date(start), as_date(end)
    if b < a:
        return 0.0
    hours = 0.0
    cur = a
    while cur <= b:
        if is_workday(cur, workweek, holidays):
            hours += hours_per_day
        cur += timedelta(days=1)
    return hours


def iso_week_key(d: date) -> str:
    y, w, _ = as_date(d).isocalendar()
    return f"{y}-W{w:02d}"
