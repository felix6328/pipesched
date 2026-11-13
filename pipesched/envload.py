"""Load ONLY SILICONFLOW_API_KEY from a local env file.

``.env.local`` may also contain unrelated secrets (DATABASE_URL, JWT_SECRET,
AES_MASTER_KEY, DEEPSEEK_* pointing at a third-party relay, etc.). This module
parses a single named line and never loads, prints, logs, or returns those
other values. Callers must not log the returned key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Official OpenAI-compatible endpoint. Do NOT substitute DEEPSEEK_BASE_URL
# (that variable in the author's file points at a third-party relay).
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

# Listed by GET /v1/models on this account; strong Chinese instruct, V3-class.
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2"

# Published list prices, retrieved 2026-11-14 (Asia/Shanghai) from
# https://www.siliconflow.cn/pricing — CNY per 1,000,000 tokens.
# The model catalog page has shown ¥2 / ¥3 for the same id; we follow the
# official pricing page and record the source with every cost figure.
SILICONFLOW_PRICES = {
    "deepseek-ai/DeepSeek-V3.2": {
        "input_cny_per_m": 4.00,
        "output_cny_per_m": 6.00,
        "cache_hit_cny_per_m": 0.40,
        "source": "https://www.siliconflow.cn/pricing",
        "retrieved": "2026-11-14",
        "note": (
            "官网价格页标价：输入 ¥4/M、输出 ¥6/M、缓存命中 ¥0.40/M。"
            "模型详情页曾出现 ¥2 / ¥3，本仓库以价格页为准。"
        ),
    },
}


def candidate_env_paths(root: Optional[Path] = None) -> list[Path]:
    if root is None:
        from pipesched.paths import ROOT
        root = ROOT
    return [root / ".env.local", Path.cwd() / ".env.local"]


def load_siliconflow_api_key(path: Optional[Path] = None) -> str:
    """Return the SiliconFlow API key, or '' if not found.

    Only the line whose name is SILICONFLOW_API_KEY is read. Values of any
    other variable in the same file are discarded and never returned.
    """
    paths = [Path(path)] if path is not None else candidate_env_paths()
    seen: set[Path] = set()
    for p in paths:
        try:
            p = p.resolve()
        except OSError:
            continue
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            if k != "SILICONFLOW_API_KEY":
                continue
            return v.strip().strip("'").strip('"')
    return ""


def estimate_cost_cny(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
) -> tuple[Optional[float], str]:
    """Estimate CNY from token counts and published list prices.

    This is not a billing-statement figure. Returns (None, reason) when the
    model has no recorded published price — caller must then report tokens only.
    Cache-hit tokens are billed at the cache-hit rate and subtracted from input.
    """
    price = SILICONFLOW_PRICES.get(model)
    if not price:
        return None, (
            f"未收录 {model} 的官网标价，只报告 token 用量，不估算人民币。"
        )
    cache_hit_tokens = max(0, int(cache_hit_tokens or 0))
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    billed_input = max(0, prompt_tokens - cache_hit_tokens)
    cny = (
        billed_input / 1_000_000 * price["input_cny_per_m"]
        + cache_hit_tokens / 1_000_000 * price["cache_hit_cny_per_m"]
        + completion_tokens / 1_000_000 * price["output_cny_per_m"]
    )
    note = (
        f"{model} 标价 输入¥{price['input_cny_per_m']}/M、"
        f"输出¥{price['output_cny_per_m']}/M、"
        f"缓存命中¥{price['cache_hit_cny_per_m']}/M；"
        f"来源 {price['source']}，查阅日期 {price['retrieved']}（Asia/Shanghai）。"
        "此为按返回 token 与标价估算，不是账单原件。"
    )
    return cny, note
