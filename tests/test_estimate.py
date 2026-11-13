from datetime import date
from pipesched.config import load_all_templates
from pipesched.estimate import estimate_task
from pipesched.models import Task, days_to_seconds

def _t(id, at, key, days, done=True, name=None):
    tmpl = load_all_templates()["p_character"]
    tn = tmpl.task_type(key).name
    return Task(
        id=id, project_id="p", project_name=tmpl.kitsu_project_name,
        asset_id=id+"a", asset_name=name or id, asset_type_id="x", asset_type_name=at,
        task_type_id="y", task_type_name=tn, task_type_key=key,
        status_id="s", status_name="Done" if done else "WIP", status_short="done" if done else "wip",
        is_done=done, is_retake_status=False, is_feedback_request=False, is_wip=not done,
        estimation_seconds=days_to_seconds(4), duration_seconds=days_to_seconds(days),
        retake_count=0, start_date=date(2026, 8, 1), due_date=date(2026, 8, 10),
        real_start_date=None, end_date=date(2026, 8, 10) if done else None,
        done_date=date(2026, 8, 10) if done else None, last_comment_date=None,
    )

def test_excludes_self_and_uses_same_type():
    tmpl = load_all_templates()["p_character"]
    pool = [
        _t("c1", "Character", "modeling", 5.0),
        _t("c2", "Character", "modeling", 6.0),
        _t("c3", "Character", "modeling", 5.5),
        _t("p1", "Prop", "modeling", 1.5),
        _t("p2", "Prop", "modeling", 2.0),
        _t("p3", "Prop", "modeling", 1.8),
    ]
    target = _t("c_new", "Character", "modeling", 9.0, done=True)
    est = estimate_task(target, pool + [target], tmpl)
    assert est.n_similar == 3
    assert "c_new" not in est.similar_task_ids
    assert abs(est.median_days - 5.5) < 0.01
    assert not est.low_confidence
    # prop should not use character times
    pt = _t("p_new", "Prop", "modeling", 9.0)
    estp = estimate_task(pt, pool + [pt], tmpl)
    assert estp.n_similar == 3
    assert estp.median_days < 3

def test_fallback_template():
    tmpl = load_all_templates()["p_character"]
    pool = [_t("c1", "Character", "modeling", 5.0)]
    target = _t("c_new", "Character", "modeling", 1.0, done=False)
    est = estimate_task(target, pool, tmpl, min_similar=3)
    assert est.source == "template_default"
    assert est.low_confidence
    assert est.template_default_days == 4.0
