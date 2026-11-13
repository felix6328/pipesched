from pipesched.llm import OpenAICompatLLM, _redact, _parse_json
from pipesched.models import Comment


def _dummy_settings():
    from pipesched.config import Settings
    return Settings(
        kitsu_host="http://127.0.0.1:8878/api",
        kitsu_email="a",
        kitsu_password="b",
        as_of="2026-11-13",
        min_similar=3,
        schedule_stat="p75",
        llm_backend="siliconflow",
        llm_base_url="https://api.siliconflow.cn/v1",
        llm_model="deepseek-ai/DeepSeek-V3.2",
        llm_api_key="dummy-not-a-real-key",
        llm_timeout=45,
        max_comment_chars=400,
        app_host="127.0.0.1",
        app_port=8879,
        template_paths={},
    )


def test_keyword_fallback_on_blocker_and_normal():
    llm = OpenAICompatLLM(_dummy_settings())
    blocker = Comment(id="1", task_id="t", text="角色布线要重做，边流太乱，绑不了。")
    normal = Comment(id="2", task_id="t", text="预览过了，细节很好。")
    e1 = llm._keyword_fallback(blocker, "Timeout", ms=12.0)
    e2 = llm._keyword_fallback(normal, "HTTP429", ms=9.0)
    assert e1.fallback and e1.backend == "keyword_fallback" and e1.is_blocker
    assert e1.failure == "Timeout"
    assert e1.citation_ok and e1.citation_text
    assert e2.fallback and not e2.is_blocker
    assert e2.failure == "HTTP429"


def test_redact_strips_bearer_and_key():
    key = "sk-secret-abc-123456789"
    s = _redact(f"Authorization: Bearer {key} boom {key}", key)
    assert key not in s
    assert "REDACTED" in s
    s2 = _redact("Bearer sk-other-token leftover", "")
    assert "sk-other-token" not in s2
    assert "Bearer [REDACTED]" in s2


def test_parse_json_fence_and_plain():
    assert _parse_json('{"is_blocker": true, "citation": "a"}')["is_blocker"] is True
    assert _parse_json('```json\\n{"is_blocker": false, "citation": ""}\\n```')["is_blocker"] is False
    assert _parse_json("not json") is None
