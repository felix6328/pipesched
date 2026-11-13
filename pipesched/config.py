
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from pipesched.paths import ROOT, SETTINGS, TEMPLATES


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m):
            return os.environ.get(m.group(1), "")
        return re.sub(r"\$\{([A-Z0-9_]+)\}", repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class TaskTypeSpec:
    id: str
    name: str
    color: str
    default_days: float
    depends_on: list[str]


@dataclass
class AssetTypeSpec:
    id: str
    kitsu_name: str
    label: str


@dataclass
class RiskSpec:
    overdue_grace_days: int = 0
    overrun_ratio: float = 1.2
    retake_threshold: int = 2
    review_sla_days: int = 1
    early_warning_days: int = 7


@dataclass
class ProjectTemplate:
    id: str
    name: str
    kitsu_project_name: str
    description: str
    pipeline: str
    production_style: str
    hours_per_day: float
    team_weekly_hours: float
    workweek: list[int]
    holidays: list[str]
    synthetic: bool
    asset_types: list[AssetTypeSpec]
    task_types: list[TaskTypeSpec]
    risk: RiskSpec
    allow_date_writeback: bool = True
    min_similar: int = 3
    schedule_stat: str = "p75"
    raw: dict = field(default_factory=dict, repr=False)

    def task_type(self, key: str) -> TaskTypeSpec:
        for t in self.task_types:
            if t.id == key or t.name == key:
                return t
        raise KeyError(key)

    def task_type_by_name(self, name: str) -> Optional[TaskTypeSpec]:
        for t in self.task_types:
            if t.name == name or t.id == name:
                return t
        return None

    def predecessors(self, key: str) -> list[str]:
        return list(self.task_type(key).depends_on)

    def topo_order(self) -> list[str]:
        remaining = {t.id: set(t.depends_on) for t in self.task_types}
        out: list[str] = []
        while remaining:
            ready = [k for k, deps in remaining.items() if not deps]
            if not ready:
                raise ValueError(f"cycle in DAG: {remaining}")
            ready.sort()
            k = ready[0]
            out.append(k)
            del remaining[k]
            for deps in remaining.values():
                deps.discard(k)
        return out

    def default_days(self, key: str) -> float:
        return float(self.task_type(key).default_days)


def load_template(path: Path | str) -> ProjectTemplate:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    risk = raw.get("risk") or {}
    writeback = raw.get("writeback") or {}
    estimate = raw.get("estimate") or {}
    return ProjectTemplate(
        id=raw["id"],
        name=raw["name"],
        kitsu_project_name=raw["kitsu_project_name"],
        description=(raw.get("description") or "").strip(),
        pipeline=raw.get("pipeline", ""),
        production_style=raw.get("production_style", "video-game"),
        hours_per_day=float(raw.get("hours_per_day", 8)),
        team_weekly_hours=float(raw.get("team_weekly_hours", 40)),
        workweek=list(raw.get("workweek") or [0, 1, 2, 3, 4]),
        holidays=list(raw.get("holidays") or []),
        synthetic=bool(raw.get("synthetic", True)),
        asset_types=[AssetTypeSpec(**a) for a in raw["asset_types"]],
        task_types=[TaskTypeSpec(**t) for t in raw["task_types"]],
        risk=RiskSpec(
            overdue_grace_days=int(risk.get("overdue_grace_days", 0)),
            overrun_ratio=float(risk.get("overrun_ratio", 1.2)),
            retake_threshold=int(risk.get("retake_threshold", 2)),
            review_sla_days=int(risk.get("review_sla_days", 1)),
            early_warning_days=int(risk.get("early_warning_days", 7)),
        ),
        allow_date_writeback=bool(writeback.get("allow_dates", True)),
        min_similar=int(estimate.get("min_similar", 3)),
        schedule_stat=str(estimate.get("schedule_stat", "p75")),
        raw=raw,
    )


def load_all_templates(settings: Optional["Settings"] = None) -> dict[str, ProjectTemplate]:
    settings = settings or load_settings()
    out = {}
    for key, rel in settings.template_paths.items():
        path = Path(rel)
        if not path.is_absolute():
            path = ROOT / path
        out[key] = load_template(path)
    return out


@dataclass
class Settings:
    kitsu_host: str
    kitsu_email: str
    kitsu_password: str
    as_of: str
    min_similar: int
    schedule_stat: str
    llm_backend: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout: int
    max_comment_chars: int
    app_host: str
    app_port: int
    template_paths: dict
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def llm_enabled_by_config(self) -> bool:
        return self.llm_backend in {"auto", "openai", "openai_compat"}


def load_settings(path: Path | str = SETTINGS) -> Settings:
    raw = _expand_env(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    kitsu = raw.get("kitsu") or {}
    estimate = raw.get("estimate") or {}
    llm = raw.get("llm") or {}
    app = raw.get("app") or {}
    # env overrides
    host = os.environ.get("PIPESCHED_KITSU_HOST") or kitsu.get("host")
    email = os.environ.get("PIPESCHED_KITSU_EMAIL") or kitsu.get("email")
    password = os.environ.get("PIPESCHED_KITSU_PASSWORD") or kitsu.get("password")
    api_key = os.environ.get("PIPESCHED_API_KEY") or os.environ.get("OPENAI_API_KEY") or llm.get("api_key") or ""
    backend = os.environ.get("PIPESCHED_LLM_BACKEND") or llm.get("backend") or "auto"
    return Settings(
        kitsu_host=host,
        kitsu_email=email,
        kitsu_password=password,
        as_of=os.environ.get("PIPESCHED_AS_OF") or raw.get("as_of") or "2026-11-13",
        min_similar=int(estimate.get("min_similar", 3)),
        schedule_stat=str(estimate.get("schedule_stat", "p75")),
        llm_backend=backend,
        llm_base_url=os.environ.get("OPENAI_BASE_URL") or llm.get("base_url") or "",
        llm_model=os.environ.get("OPENAI_MODEL") or llm.get("model") or "gpt-4o-mini",
        llm_api_key=api_key,
        llm_timeout=int(llm.get("timeout_seconds", 30)),
        max_comment_chars=int(llm.get("max_comment_chars", 400)),
        app_host=app.get("host", "127.0.0.1"),
        app_port=int(os.environ.get("PIPESCHED_PORT") or app.get("port") or 8879),
        template_paths=raw.get("templates") or {
            "p_mobile": "configs/templates/p_mobile.yaml",
            "p_character": "configs/templates/p_character.yaml",
        },
        raw=raw,
    )
