"""PipeSched 工作台：风险卡 + 排期建议确认写回。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipesched import audit
from pipesched.config import ProjectTemplate, load_all_templates, load_settings
from pipesched.kitsu_client import KitsuGateway, api_info, tasks_from_snapshot, load_snapshot
from pipesched.llm import resolve_backend
from pipesched.paths import GOLD, ROOT as PROJ_ROOT
from pipesched.service import ScanResult, confirm_dates, make_proposals, scan_tasks
from pipesched.writeback import DEFAULT_STORE, WritebackRefused

settings = load_settings()
templates = load_all_templates(settings)
llm = resolve_backend(settings)
# proposal_id -> token（只存在服务端会话，前端确认时带回）
TOKEN_MEM: dict[str, str] = {}
CACHE: dict = {"tasks": {}, "gw": None, "connected": False, "error": None, "api": None}

app = FastAPI(title="PipeSched", version="1.0.0")
STATIC = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _try_connect() -> Optional[KitsuGateway]:
    if CACHE.get("gw") and CACHE.get("connected"):
        return CACHE["gw"]
    try:
        gw = KitsuGateway(settings, connect_now=True)
        CACHE["gw"] = gw
        CACHE["connected"] = True
        CACHE["error"] = None
        CACHE["api"] = api_info()
        return gw
    except Exception as e:
        CACHE["connected"] = False
        CACHE["error"] = f"{type(e).__name__}: {e}"
        CACHE["api"] = None
        return None


def _tasks_for(tmpl: ProjectTemplate, prefer_live: bool = True):
    gw = _try_connect() if prefer_live else None
    if gw:
        proj = gw.project_by_name(tmpl.kitsu_project_name)
        if proj:
            tasks = gw.load_project_tasks(proj, tmpl)
            CACHE["tasks"][tmpl.id] = tasks
            return tasks, "kitsu"
    # fallback snapshot
    snap_path = GOLD / "snapshot.json"
    if snap_path.exists():
        tasks = [t for t in tasks_from_snapshot(load_snapshot(snap_path), templates) if t.project_name == tmpl.kitsu_project_name]
        return tasks, "snapshot"
    return [], "empty"


@app.get("/")
def index():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/api/health")
def health():
    _try_connect()
    return {
        "kitsu_connected": CACHE["connected"],
        "kitsu_error": CACHE["error"],
        "kitsu_host": settings.kitsu_host,
        "api": CACHE["api"],
        "llm_backend": llm.name,
        "llm_ran_capable": llm.name not in {"disabled"},
        "as_of": settings.as_of,
        "banner": (
            "智能解读暂停（未配置真实 LLM），规则扫描正常。"
            if llm.name == "disabled"
            else f"LLM 后端：{llm.name}"
        ),
        "synthetic_notice": "两个项目都是合成模拟数据，不是真实项目组或腾讯项目。",
    }


@app.get("/api/projects")
def list_projects():
    out = []
    for tmpl in templates.values():
        out.append({
            "id": tmpl.id,
            "name": tmpl.name,
            "kitsu_project_name": tmpl.kitsu_project_name,
            "description": tmpl.description,
            "pipeline": tmpl.pipeline,
            "task_types": [{"id": t.id, "name": t.name, "depends_on": t.depends_on, "default_days": t.default_days} for t in tmpl.task_types],
            "risk": {
                "overdue_grace_days": tmpl.risk.overdue_grace_days,
                "overrun_ratio": tmpl.risk.overrun_ratio,
                "retake_threshold": tmpl.risk.retake_threshold,
                "review_sla_days": tmpl.risk.review_sla_days,
            },
            "team_weekly_hours": tmpl.team_weekly_hours,
        })
    return {"projects": out}


@app.get("/api/scan/{template_id}")
def scan(template_id: str, shadow: bool = True):
    tmpl = templates.get(template_id)
    if not tmpl:
        raise HTTPException(404, "unknown template")
    tasks, source = _tasks_for(tmpl)
    if not tasks:
        return JSONResponse({
            "ok": False,
            "warning": "扫描结果为空：没有读到任务。不会输出「一切正常」。请先运行 python scripts/seed.py。",
            "source": source,
            "rule_cards": [],
            "keyword_cards": [],
            "llm_cards": [],
        })
    result = scan_tasks(tasks, tmpl, settings.as_of, llm=llm, project_name=tmpl.kitsu_project_name, shadow=shadow)
    return {
        "ok": True,
        "source": source,
        "project": tmpl.kitsu_project_name,
        "template_id": tmpl.id,
        "as_of": result.as_of,
        "n_tasks": result.n_tasks,
        "n_open": sum(1 for t in tasks if not t.is_done),
        "n_done": sum(1 for t in tasks if t.is_done),
        "rule_cards": [c.to_dict() for c in result.rule_cards],
        "keyword_cards": [c.to_dict() for c in result.keyword_cards],
        "llm_cards": [c.to_dict() for c in result.llm_cards],
        "llm_backend": result.llm_backend,
        "llm_ran": result.llm_ran,
        "latency_ms": result.latency_ms,
        "warnings": result.warnings,
        "tasks": [t.to_public_dict() for t in tasks],
    }


@app.get("/api/schedule/{template_id}")
def schedule(template_id: str):
    tmpl = templates.get(template_id)
    if not tmpl:
        raise HTTPException(404, "unknown template")
    tasks, source = _tasks_for(tmpl)
    issued = make_proposals(tasks, tmpl, settings.as_of, store=DEFAULT_STORE)
    items = []
    for p, token in issued:
        TOKEN_MEM[p.proposal_id] = token
        d = p.to_dict()
        # 把 token 交给这个会话的前端，用于点确认。不是公开 API 密钥。
        d["confirm_token"] = token
        items.append(d)
    return {"ok": True, "source": source, "as_of": settings.as_of, "proposals": items, "n": len(items)}


class ConfirmBody(BaseModel):
    proposal_id: str
    confirm_token: str = ""
    confirmed_by: str = ""


@app.post("/api/confirm")
def confirm(body: ConfirmBody):
    tmpl_allow = True
    gw = _try_connect()
    if not gw:
        raise HTTPException(503, f"Kitsu 未连接，无法写回。{CACHE.get('error')}")
    try:
        rec = confirm_dates(
            body.proposal_id,
            body.confirm_token,
            body.confirmed_by,
            gw,
            store=DEFAULT_STORE,
            allow_dates=tmpl_allow,
        )
        return {"ok": True, "audit": rec}
    except WritebackRefused as e:
        return JSONResponse({"ok": False, "refused": True, "reason": str(e)}, status_code=403)
    except Exception as e:
        raise HTTPException(500, f"写回失败：{type(e).__name__}: {e}")


@app.post("/api/confirm-denied-demo")
def confirm_denied_demo(body: ConfirmBody):
    """演示：故意不带令牌，应被拒绝。"""
    gw = _try_connect()
    if not gw:
        raise HTTPException(503, f"Kitsu 未连接。{CACHE.get('error')}")
    try:
        confirm_dates(body.proposal_id, "", body.confirmed_by or "someone", gw, store=DEFAULT_STORE)
        return {"ok": True, "unexpected": "should have been refused"}
    except WritebackRefused as e:
        return {"ok": False, "refused": True, "reason": str(e)}


@app.get("/api/audit")
def get_audit():
    return {"entries": audit.read_all(200)}


@app.get("/api/task/{task_id}")
def get_task(task_id: str):
    gw = _try_connect()
    if not gw:
        # search snapshot
        for tmpl in templates.values():
            tasks, _ = _tasks_for(tmpl, prefer_live=False)
            for t in tasks:
                if t.id == task_id:
                    return {"task": t.to_public_dict(), "comments": [c.to_dict() for c in t.comments], "source": "snapshot"}
        raise HTTPException(503, "Kitsu 未连接且快照中无此任务")
    raw = gw.get_task_raw(task_id)
    comments = [c.to_dict() for c in gw.get_comments(task_id)]
    return {"task": raw, "comments": comments, "source": "kitsu"}
