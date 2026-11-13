
"""Kitsu / Zou 访问层。只通过官方 Gazu 客户端，不改内核。

Zou 1.1.2 实测备注：
- 累计实耗在 task["duration"]（秒），task["time_spent"] 常为 None。
- 评论正文在 comment["text"]。
- set_time_spent 的 duration 单位是秒（与官方开发文档一致，与部分 Gazu docstring「分钟」不一致，以实测为准）。
- 返修状态评论会改 retake_count；若要固定值，在全部评论写完后再 update_task。
- 删除项目前必须先 close_project。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

import gazu
import gazu.cache

from pipesched.config import ProjectTemplate, Settings, load_settings
from pipesched.models import Comment, Task, _date


def connect(settings: Optional[Settings] = None) -> Settings:
    settings = settings or load_settings()
    gazu.cache.disable()
    host = settings.kitsu_host
    if not host.endswith("/api") and not host.rstrip("/").endswith("api"):
        host = host.rstrip("/") + "/api"
    gazu.client.set_host(host)
    gazu.log_in(settings.kitsu_email, settings.kitsu_password)
    return settings


def api_info() -> dict:
    try:
        return gazu.client.get("/") or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _status_flags(status: Optional[dict], status_id: Optional[str], status_index: dict) -> dict:
    st = status if isinstance(status, dict) and status.get("short_name") else None
    if st is None and status_id:
        st = status_index.get(status_id) or {}
    st = st or {}
    short = st.get("short_name") or ""
    return {
        "status_id": st.get("id") or status_id or "",
        "status_name": st.get("name") or "",
        "status_short": short,
        "is_done": bool(st.get("is_done")) or short in {"done", "approved"},
        "is_retake_status": bool(st.get("is_retake")) or short == "retake",
        "is_feedback_request": bool(st.get("is_feedback_request")) or short == "wfa",
        "is_wip": bool(st.get("is_wip")) or short == "wip",
    }


def _task_type_key(name: str, template: Optional[ProjectTemplate]) -> str:
    if not template:
        return ""
    spec = template.task_type_by_name(name)
    return spec.id if spec else ""


def enrich_and_build_tasks(
    raw_tasks: list[dict],
    *,
    project: dict,
    assets: list[dict],
    asset_types: list[dict],
    task_types: list[dict],
    statuses: list[dict],
    template: Optional[ProjectTemplate] = None,
    comments_by_task: Optional[dict[str, list[Comment]]] = None,
    source: str = "kitsu",
) -> list[Task]:
    at_by_id = {a["id"]: a for a in asset_types}
    assets_by_id = {a["id"]: a for a in assets}
    tt_by_id = {t["id"]: t for t in task_types}
    st_by_id = {s["id"]: s for s in statuses}
    comments_by_task = comments_by_task or {}
    out: list[Task] = []
    for raw in raw_tasks:
        entity_id = raw.get("entity_id")
        asset = assets_by_id.get(entity_id) or {}
        # get_task 会带嵌套 entity
        entity = raw.get("entity") if isinstance(raw.get("entity"), dict) else asset
        asset_name = asset.get("name") or entity.get("name") or ""
        asset_type_id = asset.get("entity_type_id") or entity.get("entity_type_id") or ""
        # asset type name
        at = at_by_id.get(asset_type_id) or {}
        asset_type_name = at.get("name") or ""
        if not asset_type_name and isinstance(raw.get("entity_type"), dict):
            asset_type_name = raw["entity_type"].get("name") or ""
        tt = raw.get("task_type") if isinstance(raw.get("task_type"), dict) else tt_by_id.get(raw.get("task_type_id") or "", {})
        tt_name = (tt or {}).get("name") or ""
        flags = _status_flags(raw.get("task_status") if isinstance(raw.get("task_status"), dict) else None, raw.get("task_status_id"), st_by_id)
        duration = raw.get("duration")
        if duration is None:
            duration = 0.0
        estimation = raw.get("estimation") or 0.0
        tid = str(raw["id"])
        out.append(Task(
            id=tid,
            project_id=str(raw.get("project_id") or project.get("id") or ""),
            project_name=project.get("name") or "",
            asset_id=str(entity_id or ""),
            asset_name=asset_name,
            asset_type_id=str(asset_type_id or ""),
            asset_type_name=asset_type_name,
            task_type_id=str(raw.get("task_type_id") or (tt or {}).get("id") or ""),
            task_type_name=tt_name,
            task_type_key=_task_type_key(tt_name, template),
            status_id=flags["status_id"],
            status_name=flags["status_name"],
            status_short=flags["status_short"],
            is_done=flags["is_done"],
            is_retake_status=flags["is_retake_status"],
            is_feedback_request=flags["is_feedback_request"],
            is_wip=flags["is_wip"],
            estimation_seconds=float(estimation or 0),
            duration_seconds=float(duration or 0),
            retake_count=int(raw.get("retake_count") or 0),
            start_date=_date(raw.get("start_date")),
            due_date=_date(raw.get("due_date")),
            real_start_date=_date(raw.get("real_start_date")),
            end_date=_date(raw.get("end_date")),
            done_date=_date(raw.get("done_date")),
            last_comment_date=_date(raw.get("last_comment_date")),
            assignees=list(raw.get("assignees") or []),
            source=source,
            comments=list(comments_by_task.get(tid, [])),
            raw=raw,
        ))
    return out


class KitsuGateway:
    def __init__(self, settings: Optional[Settings] = None, *, connect_now: bool = True):
        self.settings = settings or load_settings()
        if connect_now:
            connect(self.settings)

    def project_by_name(self, name: str) -> Optional[dict]:
        return gazu.project.get_project_by_name(name)

    def all_open_projects(self) -> list[dict]:
        return gazu.project.all_open_projects()

    def all_projects(self) -> list[dict]:
        return gazu.project.all_projects()

    def load_project_tasks(self, project: dict, template: Optional[ProjectTemplate] = None) -> list[Task]:
        raw_tasks = gazu.task.all_tasks_for_project(project)
        assets = gazu.asset.all_assets_for_project(project)
        asset_types = gazu.asset.all_asset_types()
        task_types = gazu.task.all_task_types()
        statuses = gazu.task.all_task_statuses()
        comments_by_task: dict[str, list[Comment]] = {}
        try:
            raw_comments = gazu.task.all_comments_for_project(project)
        except Exception:
            raw_comments = []
            for t in raw_tasks:
                try:
                    raw_comments.extend(gazu.task.all_comments_for_task(t))
                except Exception:
                    pass
        for rc in raw_comments:
            c = Comment.from_kitsu(rc)
            comments_by_task.setdefault(c.task_id, []).append(c)
        return enrich_and_build_tasks(
            raw_tasks,
            project=project,
            assets=assets,
            asset_types=asset_types,
            task_types=task_types,
            statuses=statuses,
            template=template,
            comments_by_task=comments_by_task,
            source="kitsu",
        )

    def get_task_raw(self, task_id: str) -> dict:
        return gazu.task.get_task(task_id)

    def get_comments(self, task_id: str) -> list[Comment]:
        return [Comment.from_kitsu(c, task_id=task_id) for c in gazu.task.all_comments_for_task(task_id)]

    def update_task_dates(self, task_id: str, start_date: str, due_date: str) -> dict:
        """只改 start_date / due_date。调用方必须先通过写回护栏。"""
        task = gazu.task.get_task(task_id)
        task["start_date"] = start_date
        task["due_date"] = due_date
        return gazu.task.update_task(task)

    def snapshot(self, projects: list[dict], templates: dict[str, ProjectTemplate]) -> dict:
        """把当前 Kitsu 状态冻成一份可复现的快照，供离线评测。"""
        info = api_info()
        payload = {"api": info, "projects": []}
        name_to_tmpl = {t.kitsu_project_name: t for t in templates.values()}
        for proj in projects:
            tmpl = name_to_tmpl.get(proj["name"])
            tasks = self.load_project_tasks(proj, tmpl)
            payload["projects"].append({
                "project": {"id": proj["id"], "name": proj["name"]},
                "template_id": tmpl.id if tmpl else None,
                "tasks": [_task_to_snap(t) for t in tasks],
            })
        return payload


def _task_to_snap(t: Task) -> dict:
    d = t.to_public_dict()
    d.update({
        "asset_type_id": t.asset_type_id,
        "task_type_id": t.task_type_id,
        "status_id": t.status_id,
        "is_retake_status": t.is_retake_status,
        "is_feedback_request": t.is_feedback_request,
        "is_wip": t.is_wip,
        "assignees": t.assignees,
        "comments": [c.to_dict() for c in t.comments],
    })
    return d


def tasks_from_snapshot(snapshot: dict, templates: dict[str, ProjectTemplate]) -> list[Task]:
    tmpl_by_id = {t.id: t for t in templates.values()}
    tmpl_by_name = {t.kitsu_project_name: t for t in templates.values()}
    out: list[Task] = []
    for block in snapshot.get("projects") or []:
        proj = block.get("project") or {}
        tmpl = tmpl_by_id.get(block.get("template_id") or "") or tmpl_by_name.get(proj.get("name") or "")
        for d in block.get("tasks") or []:
            comments = [Comment(**{k: v for k, v in c.items() if k in Comment.__dataclass_fields__}) for c in d.get("comments") or []]
            t = Task(
                id=d["id"],
                project_id=d.get("project_id") or proj.get("id") or "",
                project_name=d.get("project_name") or proj.get("name") or "",
                asset_id=d.get("asset_id") or "",
                asset_name=d.get("asset_name") or "",
                asset_type_id=d.get("asset_type_id") or "",
                asset_type_name=d.get("asset_type_name") or "",
                task_type_id=d.get("task_type_id") or "",
                task_type_name=d.get("task_type_name") or "",
                task_type_key=d.get("task_type_key") or _task_type_key(d.get("task_type_name") or "", tmpl),
                status_id=d.get("status_id") or "",
                status_name=d.get("status_name") or "",
                status_short=d.get("status_short") or "",
                is_done=bool(d.get("is_done")),
                is_retake_status=bool(d.get("is_retake_status")),
                is_feedback_request=bool(d.get("is_feedback_request")),
                is_wip=bool(d.get("is_wip")),
                estimation_seconds=float(d.get("estimation_seconds") or 0),
                duration_seconds=float(d.get("duration_seconds") or 0),
                retake_count=int(d.get("retake_count") or 0),
                start_date=_date(d.get("start_date")),
                due_date=_date(d.get("due_date")),
                real_start_date=_date(d.get("real_start_date")),
                end_date=_date(d.get("end_date")),
                done_date=_date(d.get("done_date")),
                last_comment_date=_date(d.get("last_comment_date")),
                assignees=list(d.get("assignees") or []),
                source="snapshot",
                comments=comments,
                raw=d,
            )
            out.append(t)
    return out


def load_snapshot(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
