from pipesched.comments import keyword_extract
from pipesched.models import Comment

def test_keyword_hits_blocker_and_sarcasm():
    comments = [
        Comment(id="1", task_id="t", text="角色布线要重做，边流太乱，绑不了。", gold_label="blocker"),
        Comment(id="2", task_id="t", text="又双叒重做？开玩笑的，这次一次过。", gold_label="sarcasm"),
        Comment(id="3", task_id="t", text="预览过了，细节很好。", gold_label="normal"),
        Comment(id="4", task_id="t", text="这个重做是上次的，这次拓扑没问题。", gold_label="misleading"),
    ]
    ex = {e.comment_id: e for e in keyword_extract(comments)}
    assert "1" in ex and ex["1"].is_blocker and ex["1"].citation_ok
    assert "2" in ex  # expected FP
    assert "3" not in ex
    assert "4" in ex  # expected FP
    assert all(e.backend == "keyword" for e in ex.values())
    assert "要重做" in ex["1"].citation_text or "重做" in ex["1"].citation_text
