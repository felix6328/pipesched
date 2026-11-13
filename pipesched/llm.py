
"""可插拔 LLM：有 API key 才真正调用；否则 disabled。禁用时不得报告 LLM 指标。"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import httpx

from pipesched.comments import grounded
from pipesched.config import Settings
from pipesched.models import BlockerExtraction, Comment
from pipesched.paths import COST, ensure_data_dirs

SYSTEM = """你是游戏美术制作跟踪里的备注解读助手。你只做一件事：判断一条评审备注**当前**是否在说「任务被阻塞或必须返修」，并给出阻塞原因。
硬性规则：
1. 日期、工时、返修次数、人员姓名不得编造，本任务也不需要你填写这些字段。
2. 每条结论必须引用备注原文中的一个连续片段（citation），且该片段必须逐字出现在原文里。无法引用则输出 is_blocker=false，reason=null。
3. 反话、玩笑（例如「开玩笑的，这次过了」「并没有要重做」）不是阻塞。
4. 在说「上次已经重做过、这次没问题」的，不是当前阻塞。
5. 只讨论制作反馈，不要总结人事或敏感信息。
6. 只输出 JSON，不要 markdown。格式：
{"is_blocker": true|false, "reason": "短句或null", "citation": "原文片段或空字符串"}
"""


@dataclass
class LLMUsage:
    backend: str
    n_calls: int = 0
    n_ok: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_ms: float = 0.0
    estimated_cny: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "n_calls": self.n_calls,
            "n_ok": self.n_ok,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_ms": self.total_ms,
            "estimated_cny": self.estimated_cny,
            "note": self.note,
        }


class LLMBackend:
    name = "base"

    def extract_one(self, comment: Comment) -> BlockerExtraction:
        raise NotImplementedError

    def extract(self, comments: Iterable[Comment]) -> list[BlockerExtraction]:
        return [self.extract_one(c) for c in comments if (c.text or "").strip()]

    def usage(self) -> LLMUsage:
        return LLMUsage(backend=self.name)


class DisabledLLM(LLMBackend):
    name = "disabled"

    def extract_one(self, comment: Comment) -> BlockerExtraction:
        return BlockerExtraction(
            comment_id=comment.id,
            task_id=comment.task_id,
            is_blocker=False,
            reason=None,
            citation_text="",
            citation_ok=True,
            backend="disabled",
        )

    def extract(self, comments: Iterable[Comment]) -> list[BlockerExtraction]:
        # 禁用时不产出任何阻塞结论，让规则层和关键词基线独立工作。
        return []

    def usage(self) -> LLMUsage:
        return LLMUsage(backend="disabled", note="未调用真实模型，不得填写 LLM 评测数字。")


class OpenAICompatLLM(LLMBackend):
    name = "openai_compat"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._usage = LLMUsage(backend=self.name, note="真实模型调用。费用若无账单，仅按返回的 token 计数，单价不臆造，estimated_cny=0。")

    def extract_one(self, comment: Comment) -> BlockerExtraction:
        text = (comment.text or "")[: self.settings.max_comment_chars]
        user = f"comment_id={comment.id}\n原文：{text}"
        url = (self.settings.llm_base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.settings.llm_model or "gpt-4o-mini",
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        t0 = time.perf_counter()
        self._usage.n_calls += 1
        try:
            with httpx.Client(timeout=self.settings.llm_timeout) as client:
                r = client.post(url, headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            self._usage.total_ms += (time.perf_counter() - t0) * 1000
            return BlockerExtraction(
                comment_id=comment.id,
                task_id=comment.task_id,
                is_blocker=False,
                reason=None,
                citation_text="",
                citation_ok=True,
                backend="openai_compat",
                raw=f"ERROR: {type(e).__name__}: {e}",
            )
        ms = (time.perf_counter() - t0) * 1000
        self._usage.total_ms += ms
        usage = data.get("usage") or {}
        self._usage.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self._usage.completion_tokens += int(usage.get("completion_tokens") or 0)
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            content = json.dumps(data, ensure_ascii=False)[:500]
        parsed = _parse_json(content)
        citation = (parsed.get("citation") or "") if parsed else ""
        is_blocker = bool(parsed and parsed.get("is_blocker"))
        cite_ok = grounded(citation, comment.text or "") if citation else (not is_blocker)
        if is_blocker and not cite_ok:
            # 无法对齐原文的句子不得输出为阻塞
            is_blocker = False
            reason = None
            citation = ""
        else:
            reason = (parsed.get("reason") if parsed else None) if is_blocker else None
        if is_blocker and cite_ok:
            self._usage.n_ok += 1
        elif not is_blocker:
            self._usage.n_ok += 1
        self._log(comment.id, ms, usage, content)
        return BlockerExtraction(
            comment_id=comment.id,
            task_id=comment.task_id,
            is_blocker=is_blocker,
            reason=reason,
            citation_text=citation,
            citation_ok=cite_ok,
            backend="openai_compat",
            raw=content[:1000],
        )

    def usage(self) -> LLMUsage:
        return self._usage

    def _log(self, comment_id, ms, usage, content) -> None:
        ensure_data_dirs()
        path = COST / "llm.jsonl"
        rec = {
            "comment_id": comment_id,
            "ms": ms,
            "usage": usage,
            "model": self.settings.llm_model,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _parse_json(content: str) -> Optional[dict]:
    if not content:
        return None
    content = content.strip()
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def resolve_backend(settings: Settings) -> LLMBackend:
    backend = (settings.llm_backend or "auto").lower()
    key = settings.llm_api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("PIPESCHED_API_KEY") or ""
    if backend in {"disabled", "off", "none", "rule"}:
        return DisabledLLM()
    if backend == "auto":
        if key:
            return OpenAICompatLLM(settings)
        return DisabledLLM()
    if backend in {"openai", "openai_compat", "llm"}:
        if not key:
            return DisabledLLM()
        return OpenAICompatLLM(settings)
    return DisabledLLM()
