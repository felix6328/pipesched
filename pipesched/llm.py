"""可插拔 LLM：有 SiliconFlow（或其它 OpenAI 兼容）key 才真正调用；否则 disabled。

禁用时不得报告 LLM 指标。失败时回退到关键词基线，不得把失败当成「不是阻塞」。
密钥不得写入日志、评测结果或异常字符串。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import httpx

from pipesched.comments import grounded, keyword_extract
from pipesched.config import Settings
from pipesched.envload import (
    DEFAULT_MODEL,
    SILICONFLOW_BASE_URL,
    estimate_cost_cny,
    load_siliconflow_api_key,
)
from pipesched.hallucination import hallucination_flags
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
{"is_blocker": true|false, "reason": "短句或null", "citation": "原文片段或空字符串", "comment_id": "传入的 comment_id"}
"""


def _redact(text: str, key: str = "") -> str:
    """Strip secrets from an error or log string. Never leave a Bearer token."""
    if not text:
        return text
    s = str(text)
    if key:
        s = s.replace(key, "[REDACTED]")
    s = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", s, flags=re.I)
    s = re.sub(r"(api[_-]?key|authorization|JWT_SECRET|AES_MASTER_KEY|DATABASE_URL)\s*[:=]\s*\S+",
               r"\1=[REDACTED]", s, flags=re.I)
    return s


def _cache_hit_tokens(usage: dict) -> int:
    if not usage:
        return 0
    details = usage.get("prompt_tokens_details") or {}
    for k in ("prompt_cache_hit_tokens", "cache_hit_tokens"):
        if usage.get(k) is not None:
            try:
                return int(usage[k] or 0)
            except (TypeError, ValueError):
                pass
    try:
        return int(details.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _parse_json(content: str) -> Optional[dict]:
    if not content:
        return None
    content = content.strip()
    # strip markdown fences if a model ignores the JSON-only instruction
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else None
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None


@dataclass
class LLMUsage:
    backend: str
    model: str = ""
    n_calls: int = 0
    n_ok: int = 0
    n_parse_fail: int = 0
    n_api_fail: int = 0
    n_fallback: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    total_ms: float = 0.0
    estimated_cny: Optional[float] = None
    price_note: str = ""
    note: str = ""
    latencies_ms: list = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return self._to_dict()

    def _to_dict(self) -> dict:
        def pct(p):
            xs = [x for x in self.latencies_ms if x is not None]
            if not xs:
                return None
            ys = sorted(xs)
            if len(ys) == 1:
                return round(ys[0], 2)
            k = (len(ys) - 1) * p / 100.0
            f = int(k)
            c = min(f + 1, len(ys) - 1)
            return round(ys[f] + (ys[c] - ys[f]) * (k - f), 2)

        cny, note = estimate_cost_cny(
            self.model, self.prompt_tokens, self.completion_tokens, self.cache_hit_tokens,
        )
        return {
            "backend": self.backend,
            "model": self.model,
            "n_calls": self.n_calls,
            "n_ok": self.n_ok,
            "n_parse_fail": self.n_parse_fail,
            "n_api_fail": self.n_api_fail,
            "n_fallback": self.n_fallback,
            "fallback_rate": (self.n_fallback / self.n_calls) if self.n_calls else None,
            "failure_rate": ((self.n_api_fail + self.n_parse_fail) / self.n_calls) if self.n_calls else None,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "total_ms": round(self.total_ms, 2),
            "latency_p50_ms": pct(50),
            "latency_p95_ms": pct(95),
            "estimated_cny": None if cny is None else round(cny, 6),
            "price_note": note,
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
        self.model = settings.llm_model or DEFAULT_MODEL
        self.base_url = (settings.llm_base_url or SILICONFLOW_BASE_URL).rstrip("/")
        if "siliconflow" in (self.base_url or "").lower():
            self.name = "siliconflow"
        self._key = settings.llm_api_key or ""
        self._usage = LLMUsage(
            backend=self.name,
            model=self.model,
            note="真实模型调用。费用按 API 返回的 token 与官网标价估算，不是账单原件。",
        )
        # text -> extraction, so retrospective can reuse gold-set calls
        self._cache_by_text: dict[str, BlockerExtraction] = {}
        self._usage_lock = asyncio.Lock()

    def usage(self) -> LLMUsage:
        return self._usage

    def extract_one(self, comment: Comment) -> BlockerExtraction:
        return asyncio.run(self._extract_async([comment]))[0]

    def extract(self, comments: Iterable[Comment]) -> list[BlockerExtraction]:
        items = [c for c in comments if (c.text or "").strip()]
        if not items:
            return []
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running and running.is_running():
            # Nested loop (unlikely in our scripts): sequential fallback.
            out = []
            for c in items:
                out.append(asyncio.run(self._extract_async([c]))[0] if False else self._extract_one_sync(c))
            return out
        return asyncio.run(self._extract_async(items))

    def _extract_one_sync(self, comment: Comment) -> BlockerExtraction:
        # Used only if already inside an event loop; still goes through HTTP via httpx sync.
        return asyncio.run(self._extract_async([comment]))[0]

    async def _extract_async(self, comments: list[Comment]) -> list[BlockerExtraction]:
        timeout = httpx.Timeout(
            connect=15.0,
            read=float(self.settings.llm_timeout or 45),
            write=15.0,
            pool=15.0,
        )
        limits = httpx.Limits(
            max_connections=max(4, self.settings.llm_concurrency + 2),
            max_keepalive_connections=max(2, self.settings.llm_concurrency),
        )
        sem = asyncio.Semaphore(max(1, int(self.settings.llm_concurrency or 6)))
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = self.base_url + "/chat/completions"

        # Dedupe by exact text first so concurrent workers don't race the
        # in-memory cache and double-bill identical synthetic comments.
        # (v2 评测跑了 44 次调用、只有 29 条不重复原文，就是修这之前的并发重复。)
        first_of_text: dict[str, Comment] = {}
        for c in comments:
            key = (c.text or "")[: self.settings.max_comment_chars]
            if key not in first_of_text:
                first_of_text[key] = c
        uniques = list(first_of_text.values())

        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            async def one(c: Comment) -> BlockerExtraction:
                async with sem:
                    return await self._call_one(client, url, headers, c)

            await asyncio.gather(*[one(c) for c in uniques])
            # Map every comment through the now-warm text cache (0 extra tokens).
            return [await self._call_one(client, url, headers, c) for c in comments]

    async def _call_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict,
        comment: Comment,
    ) -> BlockerExtraction:
        text = (comment.text or "")[: self.settings.max_comment_chars]
        cached = self._cache_by_text.get(text)
        if cached is not None:
            # Reuse model judgment, but retarget ids. Do not double-count tokens.
            clone = BlockerExtraction(
                comment_id=comment.id,
                task_id=comment.task_id,
                is_blocker=cached.is_blocker,
                reason=cached.reason,
                citation_text=cached.citation_text,
                citation_ok=cached.citation_ok,
                backend=cached.backend,
                raw=cached.raw,
                fallback=cached.fallback,
                failure=cached.failure,
                raw_is_blocker=cached.raw_is_blocker,
                raw_citation=cached.raw_citation,
                raw_reason=cached.raw_reason,
                raw_parsed_ok=cached.raw_parsed_ok,
                latency_ms=0.0,
                hallucination_flags=list(cached.hallucination_flags),
                prompt_tokens=0,
                completion_tokens=0,
                cache_hit_tokens=0,
            )
            return clone

        user = f"comment_id={comment.id}\n原文：{text}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": int(self.settings.llm_max_tokens or 256),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        t0 = time.perf_counter()
        async with self._usage_lock:
            self._usage.n_calls += 1
        data = None
        last_fail = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                r = await client.post(url, headers=headers, json=payload)
                if r.status_code in {429, 503, 502, 504} and attempt < max_attempts - 1:
                    last_fail = f"HTTP{r.status_code}"
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except httpx.TimeoutException:
                last_fail = "Timeout"
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
            except Exception as e:
                last_fail = _redact(f"{type(e).__name__}", self._key)
                # HTTPStatusError after raise_for_status
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                    last_fail = f"HTTP{e.response.status_code}"
                if attempt < max_attempts - 1 and last_fail.startswith("HTTP"):
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
                break

        ms = (time.perf_counter() - t0) * 1000
        async with self._usage_lock:
            self._usage.total_ms += ms
            self._usage.latencies_ms.append(ms)

        if data is None:
            async with self._usage_lock:
                self._usage.n_api_fail += 1
                self._usage.n_fallback += 1
            ext = self._keyword_fallback(comment, last_fail or "api_fail", ms)
            self._log(comment.id, ms, {}, fallback=True, failure=ext.failure)
            self._cache_by_text[text] = ext
            return ext

        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cht = _cache_hit_tokens(usage)
        async with self._usage_lock:
            self._usage.prompt_tokens += pt
            self._usage.completion_tokens += ct
            self._usage.cache_hit_tokens += cht

        content = ""
        try:
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning_content"):
                # thinking model leaked reasoning; try to parse a JSON tail
                content = msg.get("reasoning_content") or ""
        except Exception:
            content = ""

        parsed = _parse_json(content)
        if parsed is None:
            async with self._usage_lock:
                self._usage.n_parse_fail += 1
                self._usage.n_fallback += 1
            ext = self._keyword_fallback(comment, "parse_fail", ms)
            ext.raw = (content or "")[:1000]
            ext.prompt_tokens = pt
            ext.completion_tokens = ct
            ext.cache_hit_tokens = cht
            self._log(comment.id, ms, usage, fallback=True, failure="parse_fail")
            self._cache_by_text[text] = ext
            return ext

        raw_citation = parsed.get("citation") or ""
        if not isinstance(raw_citation, str):
            raw_citation = str(raw_citation) if raw_citation is not None else ""
        raw_reason = parsed.get("reason")
        if raw_reason is not None and not isinstance(raw_reason, str):
            raw_reason = str(raw_reason)
        raw_is_blocker = bool(parsed.get("is_blocker"))
        cite_ok = grounded(raw_citation, comment.text or "") if raw_citation else False
        flags = hallucination_flags(parsed, comment.text or "")

        # Product guardrail: ungrounded blocker is suppressed (not shown as a blocker).
        is_blocker = raw_is_blocker and cite_ok
        citation = raw_citation if cite_ok else ""
        reason = (raw_reason if is_blocker else None)
        async with self._usage_lock:
            self._usage.n_ok += 1
        ext = BlockerExtraction(
            comment_id=comment.id,
            task_id=comment.task_id,
            is_blocker=is_blocker,
            reason=reason,
            citation_text=citation,
            citation_ok=cite_ok if raw_citation else (not raw_is_blocker),
            backend=self.name,
            raw=(content or "")[:1000],
            fallback=False,
            failure=None,
            raw_is_blocker=raw_is_blocker,
            raw_citation=raw_citation,
            raw_reason=raw_reason if isinstance(raw_reason, str) or raw_reason is None else str(raw_reason),
            raw_parsed_ok=True,
            latency_ms=ms,
            hallucination_flags=flags,
            prompt_tokens=pt,
            completion_tokens=ct,
            cache_hit_tokens=cht,
        )
        self._log(comment.id, ms, usage, fallback=False, failure=None)
        self._cache_by_text[text] = ext
        return ext

    def _keyword_fallback(self, comment: Comment, failure: str, ms: float = 0.0) -> BlockerExtraction:
        hits = keyword_extract([comment])
        if hits:
            e = hits[0]
            return BlockerExtraction(
                comment_id=comment.id,
                task_id=comment.task_id,
                is_blocker=True,
                reason=e.reason,
                citation_text=e.citation_text,
                citation_ok=e.citation_ok,
                backend="keyword_fallback",
                raw=None,
                fallback=True,
                failure=failure,
                raw_is_blocker=None,
                raw_citation="",
                raw_reason=None,
                raw_parsed_ok=False,
                latency_ms=ms,
                hallucination_flags=[],
            )
        return BlockerExtraction(
            comment_id=comment.id,
            task_id=comment.task_id,
            is_blocker=False,
            reason=None,
            citation_text="",
            citation_ok=True,
            backend="keyword_fallback",
            raw=None,
            fallback=True,
            failure=failure,
            raw_is_blocker=None,
            raw_citation="",
            raw_reason=None,
            raw_parsed_ok=False,
            latency_ms=ms,
            hallucination_flags=[],
        )

    def _log(self, comment_id, ms, usage, fallback=False, failure=None) -> None:
        ensure_data_dirs()
        path = COST / "llm.jsonl"
        rec = {
            "comment_id": comment_id,
            "ms": ms,
            "usage": {
                k: usage[k]
                for k in usage
                if k in {
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens",
                    "cache_hit_tokens",
                    "prompt_tokens_details",
                    "completion_tokens_details",
                }
            } if isinstance(usage, dict) else {},
            "model": self.model,
            "fallback": fallback,
            "failure": failure,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def resolve_backend(settings: Settings) -> LLMBackend:
    backend = (settings.llm_backend or "auto").lower()
    if backend in {"disabled", "off", "none", "rule", "keyword"}:
        return DisabledLLM()

    # Prefer SiliconFlow. Never read or send DEEPSEEK_* (third-party relay).
    sf_key = load_siliconflow_api_key()
    if sf_key:
        settings.llm_api_key = sf_key
        settings.llm_base_url = SILICONFLOW_BASE_URL
        if (
            not settings.llm_model
            or settings.llm_model.startswith("${")
            or "aiproxy" in (settings.llm_base_url or "")
        ):
            settings.llm_model = DEFAULT_MODEL
        if not settings.llm_model:
            settings.llm_model = DEFAULT_MODEL
        return OpenAICompatLLM(settings)

    key = settings.llm_api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("PIPESCHED_API_KEY") or ""
    if backend == "auto":
        if key:
            return OpenAICompatLLM(settings)
        return DisabledLLM()
    if backend in {"openai", "openai_compat", "llm", "siliconflow"}:
        if not key:
            return DisabledLLM()
        return OpenAICompatLLM(settings)
    return DisabledLLM()
