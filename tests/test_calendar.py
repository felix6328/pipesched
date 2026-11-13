from datetime import date
from pipesched.calendarutil import add_work_hours, is_workday, next_workday, work_hours_between

WW = (0, 1, 2, 3, 4)

def test_weekend_skipped():
    # 2026-11-13 is Friday. +8h = Friday, +16h = Monday 11-16
    assert date(2026, 11, 13).weekday() == 4
    assert add_work_hours(date(2026, 11, 13), 8, 8, WW, ()) == date(2026, 11, 13)
    assert add_work_hours(date(2026, 11, 13), 16, 8, WW, ()) == date(2026, 11, 16)
    assert add_work_hours(date(2026, 11, 13), 8.1, 8, WW, ()) == date(2026, 11, 16)

def test_next_workday_weekend():
    assert next_workday(date(2026, 11, 14), WW, ()) == date(2026, 11, 16)

def test_hours_between():
    # Fri 13 to Mon 16 inclusive = Fri + Mon = 16h
    assert work_hours_between(date(2026, 11, 13), date(2026, 11, 16), 8, WW, ()) == 16
