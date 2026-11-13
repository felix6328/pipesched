
"""评审备注解读：关键词确定性基线 + 可选 LLM。每条结论必须带回评论 ID 与原文片段。"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from pipesched.models import BlockerExtraction, Comment, RiskCard

# 确定性关键词。会误伤反话和「上次重做已经修完」——这是基线的已知代价，评测里会写出来。
BLOCKER_PATTERNS = [
    (r"要重做", "需要重做"),
    (r"得重做", "需要重做"),
    (r"重做", "出现「重做」表述"),
    (r"得重展", "UV 需要重展"),
    (r"重展", "需要重展"),
    (r"绑不了", "拓扑/布线导致无法绑定"),
    (r"布线要重", "布线需要重做"),
    (r"等主美", "等待主美决策"),
    (r"先搁着", "任务被搁置"),
    (r"源文件(丢了|没有|找不到)", "源文件缺失"),
    (r"等待.{0,10}方案", "等待方案"),
    (r"TA.{0,8}没空", "等待 TA 档期"),
    (r"UV\s*重叠", "UV 重叠"),
    (r"渲染有花", "渲染问题需返修"),
]

# 用于「看起来像 blocker 但可能是反话 / 历史」的提示，关键词基线本身不去掉，
# 让评测暴露 FP。LLM 路径应能区分。
SARCASM_HINTS = [
    r"开玩笑",
    r"并没有",
    r"不是反话",
    r"真的，不是",
    r"如果这是三个月前",
]
HISTORICAL_HINTS = [
    r"上次重做",
    r"已经修完",
    r"v\d+ 已通过",
    r"这次已经",
    r"不要重做",
    r"保持现状",
    r"这次没问题",
    r"这次拓扑没问题",
    r"重做记录见",
]


def _cite(text: str, span: tuple[int, int], window: int = 18) -> str:
    a = max(0, span[0] - window)
    b = min(len(text), span[1] + window)
    return text[a:b]


def keyword_extract(comments: Iterable[Comment]) -> list[BlockerExtraction]:
    """确定性关键词基线。明确标注 backend=keyword，不得冒充 LLM 指标。"""
    out: list[BlockerExtraction] = []
    for c in comments:
        text = c.text or ""
        if not text.strip():
            continue
        hit = None
        reason = None
        span = None
        for pat, why in BLOCKER_PATTERNS:
            m = re.search(pat, text)
            if m:
                hit = m
                reason = why
                span = m.span()
                break
        if not hit:
            continue
        # 基线仍然报告为 blocker，即使有反话提示——这是故意保留的 FP 来源
        citation = _cite(text, span)
        out.append(BlockerExtraction(
            comment_id=c.id,
            task_id=c.task_id,
            is_blocker=True,
            reason=reason,
            citation_text=citation,
            citation_ok=citation and citation in text,
            backend="keyword",
        ))
    return out


def extractions_to_cards(
    extractions: list[BlockerExtraction],
    comments: Iterable[Comment],
    tasks_by_id: dict,
    as_of: str,
) -> list[RiskCard]:
    cmap = {c.id: c for c in comments}
    cards = []
    for ex in extractions:
        if not ex.is_blocker:
            continue
        c = cmap.get(ex.comment_id)
        t = tasks_by_id.get(ex.task_id)
        if t is None:
            continue
        cards.append(RiskCard(
            task_id=ex.task_id,
            project_id=t.project_id,
            asset_name=t.asset_name,
            task_type_name=t.task_type_name,
            kind="comment_blocker",
            severity="medium",
            title=f"{t.asset_name} / {t.task_type_name} 备注提示阻塞：{ex.reason or '见原文'}",
            evidence={
                "comment_id": ex.comment_id,
                "reason": ex.reason,
                "backend": ex.backend,
                "citation_ok": ex.citation_ok,
            },
            evidence_fields=["comment_id", "citation_text"],
            comment_citations=[{
                "comment_id": ex.comment_id,
                "text": c.text if c else "",
                "citation": ex.citation_text,
                "created_at": c.created_at if c else None,
            }],
            layer=ex.backend,
            as_of=as_of,
        ))
    return cards


def grounded(citation: str, full_text: str) -> bool:
    if not citation or not full_text:
        return False
    if citation in full_text:
        return True
    # 允许去掉空白后比对
    def norm(s):
        return re.sub(r"\s+", "", s)
    return len(citation) >= 4 and norm(citation) in norm(full_text)
