from pipesched.hallucination import hallucination_flags


SRC = "角色布线要重做，边流太乱，绑不了。"


def test_grounded_paraphrase_not_flagged():
    parsed = {
        "is_blocker": True,
        "reason": "技术实现存在明确障碍",
        "citation": "角色布线要重做",
    }
    assert hallucination_flags(parsed, SRC) == []


def test_ungrounded_citation_and_blocker_claim():
    parsed = {
        "is_blocker": True,
        "reason": "x",
        "citation": "完全编造的一段引用内容",
    }
    flags = hallucination_flags(parsed, SRC)
    assert "citation_ungrounded" in flags
    assert "blocker_without_grounded_citation" in flags


def test_blocker_without_citation():
    parsed = {"is_blocker": True, "reason": "要返修", "citation": ""}
    flags = hallucination_flags(parsed, SRC)
    assert "blocker_without_grounded_citation" in flags
    assert "citation_ungrounded" not in flags


def test_invented_number():
    parsed = {
        "is_blocker": True,
        "reason": "还要再等 15 天",
        "citation": "等主美回来",
    }
    src = "等主美回来再改。"
    flags = hallucination_flags(parsed, src)
    assert "reason_invented_number" in flags
    assert "citation_ungrounded" not in flags


def test_invented_quote():
    parsed = {
        "is_blocker": True,
        "reason": "备注写了「必须明天交角色最终绑定」",
        "citation": "先搁着",
    }
    src = "等主美，先搁着。"
    flags = hallucination_flags(parsed, src)
    assert "reason_invented_quote" in flags


def test_none_parse():
    assert hallucination_flags(None, SRC) == []
    assert hallucination_flags({}, SRC) == []
