from datetime import date
from pipesched.config import load_all_templates
from pipesched.models import Task, days_to_seconds
from pipesched.risk import scan_task, rule_overdue

AS_OF = date(2026, 11, 13)

def _task(**kw):
    base = dict(
        id="t1", project_id="p", project_name="P-Mobile｜风格化手游（模拟）",
        asset_id="a", asset_name="sword", asset_type_id="x", asset_type_name="Prop",
        task_type_id="y", task_type_name="制作", task_type_key="production",
        status_id="s", status_name="WIP", status_short="wip",
        is_done=False, is_retake_status=False, is_feedback_request=False, is_wip=True,
        estimation_seconds=days_to_seconds(2), duration_seconds=0, retake_count=0,
        start_date=date(2026, 11, 3), due_date=date(2026, 11, 20),
        real_start_date=None, end_date=None, done_date=None, last_comment_date=None,
    )
    base.update(kw)
    return Task(**base)

def test_overdue_and_grace():
    tmpl = load_all_templates()["p_mobile"]
    t = _task(due_date=date(2026, 11, 8))
    cards = scan_task(t, [t], tmpl, AS_OF)
    assert any(c.kind == "overdue" for c in cards)
    # due today, not overdue
    t2 = _task(due_date=date(2026, 11, 13))
    assert rule_overdue(t2, tmpl, AS_OF) is None
    # character grace=1, due 11-12 not overdue on 11-13
    ct = load_all_templates()["p_character"]
    t3 = _task(due_date=date(2026, 11, 12), task_type_name="建模", task_type_key="modeling")
    assert rule_overdue(t3, ct, AS_OF) is None
    t4 = _task(due_date=date(2026, 11, 10), task_type_name="建模", task_type_key="modeling")
    assert rule_overdue(t4, ct, AS_OF) is not None

def test_overrun_threshold():
    tmpl = load_all_templates()["p_mobile"]
    # 2 days estimate, 2.3 duration = 1.15 < 1.2
    t = _task(duration_seconds=days_to_seconds(2.3), estimation_seconds=days_to_seconds(2))
    assert not any(c.kind == "overrun" for c in scan_task(t, [t], tmpl, AS_OF))
    t2 = _task(duration_seconds=days_to_seconds(2.5), estimation_seconds=days_to_seconds(2))
    assert any(c.kind == "overrun" for c in scan_task(t2, [t2], tmpl, AS_OF))

def test_done_tasks_not_flagged():
    tmpl = load_all_templates()["p_mobile"]
    t = _task(is_done=True, status_short="done", due_date=date(2026, 11, 1), retake_count=5,
              duration_seconds=days_to_seconds(10), estimation_seconds=days_to_seconds(1))
    assert scan_task(t, [t], tmpl, AS_OF) == []

def test_dep_blocked():
    tmpl = load_all_templates()["p_mobile"]
    pred = _task(id="pred", task_type_name="原画", task_type_key="concept", is_done=False, status_short="wip",
                 start_date=date(2026, 11, 1), due_date=date(2026, 11, 20))
    cur = _task(id="cur", task_type_name="制作", task_type_key="production",
                start_date=date(2026, 11, 10), due_date=date(2026, 11, 25), asset_id="a")
    pred.asset_id = "a"
    cards = scan_task(cur, [pred, cur], tmpl, AS_OF)
    assert any(c.kind == "dep_blocked" for c in cards)
    pred.is_done = True
    pred.status_short = "done"
    cards2 = scan_task(cur, [pred, cur], tmpl, AS_OF)
    assert not any(c.kind == "dep_blocked" for c in cards2)
