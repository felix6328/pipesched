import pytest
from pipesched.models import ScheduleProposal
from pipesched.writeback import ProposalStore, WritebackRefused, confirm_and_write

def _prop():
    return ScheduleProposal(
        proposal_id="p1",
        project_id="proj",
        asset_id="a",
        asset_name="sword",
        task_id="task-1",
        task_type_name="制作",
        task_type_key="production",
        current_start=None,
        current_due=None,
        proposed_start="2026-11-16",
        proposed_due="2026-11-18",
        estimate_days=2.0,
        estimate_source="similar_p75",
        similar_task_ids=["x"],
        conflicts=[],
        basis="test",
    )

def test_refuse_without_confirmed_by():
    store = ProposalStore()
    p = _prop()
    token = store.issue(p)
    called = []
    def writer(*a):
        called.append(a)
        return {}
    with pytest.raises(WritebackRefused):
        confirm_and_write(p.proposal_id, token, "", writer, store=store)
    assert called == []

def test_refuse_without_token():
    store = ProposalStore()
    p = _prop()
    store.issue(p)
    called = []
    def writer(*a):
        called.append(a)
        return {}
    with pytest.raises(WritebackRefused):
        confirm_and_write(p.proposal_id, None, "制片张三", writer, store=store)
    with pytest.raises(WritebackRefused):
        confirm_and_write(p.proposal_id, "", "制片张三", writer, store=store)
    assert called == []

def test_refuse_bad_token():
    store = ProposalStore()
    p = _prop()
    store.issue(p)
    called = []
    def writer(*a):
        called.append(a)
        return {}
    with pytest.raises(WritebackRefused):
        confirm_and_write(p.proposal_id, "not-the-token", "制片张三", writer, store=store)
    assert called == []

def test_confirm_calls_writer_once():
    store = ProposalStore()
    p = _prop()
    token = store.issue(p)
    called = []
    def writer(tid, start, due):
        called.append((tid, start, due))
        return {"start_date": start + "T00:00:00", "due_date": due + "T00:00:00"}
    rec = confirm_and_write(p.proposal_id, token, "制片张三", writer, store=store)
    assert rec["ok"] is True
    assert called == [("task-1", "2026-11-16", "2026-11-18")]
    # second time refused, no second write
    with pytest.raises(WritebackRefused):
        confirm_and_write(p.proposal_id, token, "制片张三", writer, store=store)
    assert len(called) == 1

def test_unknown_proposal():
    store = ProposalStore()
    called = []
    with pytest.raises(WritebackRefused):
        confirm_and_write("nope", "tok", "人", lambda *a: called.append(a) or {}, store=store)
    assert called == []
