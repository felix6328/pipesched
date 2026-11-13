from datetime import date
from pipesched.config import load_all_templates
from pipesched.models import Task, days_to_seconds
from pipesched.schedule import propose_for_asset

def test_dag_order_and_conflict_flag():
    tmpl = load_all_templates()["p_mobile"]
    def t(id, key, name, done=False, start=None, due=None):
        return Task(
            id=id, project_id="p", project_name=tmpl.kitsu_project_name,
            asset_id="asset", asset_name="m_prop_newpack", asset_type_id="x", asset_type_name="Prop",
            task_type_id="y", task_type_name=name, task_type_key=key,
            status_id="s", status_name="Todo", status_short="todo" if not done else "done",
            is_done=done, is_retake_status=False, is_feedback_request=False, is_wip=False,
            estimation_seconds=days_to_seconds(tmpl.default_days(key)), duration_seconds=0,
            retake_count=0, start_date=start, due_date=due,
            real_start_date=None, end_date=due if done else None,
            done_date=due if done else None, last_comment_date=None,
        )
    # no history → template default, low confidence
    tasks = [
        t("1", "concept", "原画"),
        t("2", "production", "制作"),
        t("3", "ingest_qa", "入库检查"),
    ]
    props = propose_for_asset("asset", "m_prop_newpack", "p", tasks, tasks, tmpl, date(2026, 11, 13))
    assert [p.task_type_key for p in props] == ["concept", "production", "ingest_qa"]
    # production starts after concept ends
    c, p, q = props
    assert p.proposed_start > c.proposed_due
    assert q.proposed_start > p.proposed_due
    assert c.estimate_source == "template_default"
