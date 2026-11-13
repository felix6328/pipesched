
"""扫描、排期、确认写回的编排。给工作台和评测共用。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from pipesched.comments import extractions_to_cards, keyword_extract
from pipesched.config import ProjectTemplate, Settings, load_all_templates, load_settings
from pipesched.kitsu_client import KitsuGateway, tasks_from_snapshot
from pipesched.llm import DisabledLLM, LLMBackend, resolve_backend
from pipesched.models import RiskCard, ScheduleProposal, Task
from pipesched.risk import scan_project
from pipesched.schedule import propose_project
from pipesched.shadow import write_report
from pipesched.writeback import (
    DEFAULT_STORE,
    ProposalStore,
    WritebackRefused,
    confirm_and_write,
)


@dataclass
class ScanResult:
    project_name: str
    template_id: str
    as_of: str
    n_tasks: int
    rule_cards: list[RiskCard]
    keyword_cards: list[RiskCard]
    llm_cards: list[RiskCard]
    llm_backend: str
    llm_ran: bool
    latency_ms: dict
    warnings: list[str] = field(default_factory=list)

    def all_cards(self) -> list[RiskCard]:
        return list(self.rule_cards) + list(self.keyword_cards) + list(self.llm_cards)


def scan_tasks(
    tasks: list[Task],
    template: ProjectTemplate,
    as_of: str,
    *,
    llm: Optional[LLMBackend] = None,
    project_name: str = "",
    shadow: bool = True,
) -> ScanResult:
    warnings = []
    if not tasks:
        warnings.append("扫描结果为空：没有读到任务。不会输出「一切正常」，请检查种子数据或 Kitsu 连接。")

    t0 = time.perf_counter()
    rule_cards = scan_project(tasks, template, as_of)
    t_rule = (time.perf_counter() - t0) * 1000

    # 产品扫描只解读尚未完成任务的备注；已完成历史里的反话不应每天再报警。
    # 评测评论基线仍用 manifest 全量金标，不走这里。
    open_tasks = [t for t in tasks if not t.is_done]
    comments = [c for t in open_tasks for c in t.comments]
    t1 = time.perf_counter()
    kw = keyword_extract(comments)
    by_id = {t.id: t for t in tasks}
    keyword_cards = extractions_to_cards(kw, comments, by_id, as_of)
    t_kw = (time.perf_counter() - t1) * 1000

    llm = llm or DisabledLLM()
    t2 = time.perf_counter()
    llm_ex = llm.extract(comments) if llm.name != "disabled" else []
    llm_cards = extractions_to_cards(llm_ex, comments, by_id, as_of) if llm_ex else []
    t_llm = (time.perf_counter() - t2) * 1000
    llm_ran = llm.name not in {"disabled", "keyword"} and getattr(llm, "usage", lambda: None)() and llm.usage().n_calls > 0

    # 若 LLM 没跑，界面只展示规则 + 关键词，并提示智能解读暂停
    if not llm_ran:
        warnings.append("智能解读暂停（未配置或未调用真实 LLM），规则扫描正常。关键词基线仅作对照，不是模型结果。")

    result = ScanResult(
        project_name=project_name or template.name,
        template_id=template.id,
        as_of=as_of,
        n_tasks=len(tasks),
        rule_cards=rule_cards,
        keyword_cards=keyword_cards,
        llm_cards=llm_cards,
        llm_backend=llm.name,
        llm_ran=bool(llm_ran),
        latency_ms={
            "rule": round(t_rule, 2),
            "keyword": round(t_kw, 2),
            "llm": round(t_llm, 2),
            "total": round(t_rule + t_kw + t_llm, 2),
        },
        warnings=warnings,
    )
    if shadow:
        write_report(
            result.project_name,
            as_of,
            result.all_cards(),
            extra={"latency_ms": result.latency_ms, "llm_backend": llm.name, "n_tasks": len(tasks), "warnings": warnings},
        )
    return result


def make_proposals(
    tasks: list[Task],
    template: ProjectTemplate,
    as_of: str,
    *,
    store: Optional[ProposalStore] = None,
) -> list[tuple[ScheduleProposal, str]]:
    """返回 (proposal, raw_token)。token 只交给确认接口，不写进前端公开列表的可复制字段之外的审计。"""
    store = store or DEFAULT_STORE
    proposals = propose_project(tasks, template, as_of)
    issued = []
    for p in proposals:
        token = store.issue(p)
        issued.append((p, token))
    return issued


def confirm_dates(
    proposal_id: str,
    token: str,
    confirmed_by: str,
    gateway: KitsuGateway,
    *,
    store: Optional[ProposalStore] = None,
    allow_dates: bool = True,
) -> dict:
    def writer(task_id, start, due):
        return gateway.update_task_dates(task_id, start, due)

    return confirm_and_write(
        proposal_id,
        token,
        confirmed_by,
        writer,
        store=store or DEFAULT_STORE,
        allow_dates=allow_dates,
    )
