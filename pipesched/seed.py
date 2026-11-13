"""合成种子数据生成器。

所有项目、资产、任务、工时、评论都是脚本用固定随机种子（默认 42）生成的
**模拟数据**，明确标注 synthetic。不是真实制片或腾讯项目组的数据。

生成逻辑（写在这里是为了面试时可被追问、可被复现）：

1. 两个 Kitsu 项目对应两份 YAML 模板，任务类型与依赖只来自模板。
2. 已完成任务的「真实人天」服从对数正态，均值按 (资产类型, 任务类型) 查表，
   有意让角色建模偏长、道具建模偏短，使「只用任务类型、不分资产类型」的估时会串。
3. 任务写进 Kitsu 的 estimation 用模板默认人天（模拟制片按模板拍的估时），
   实耗 duration 用生成的真实人天。MAPE 比较的就是「同类历史中位数」对真实实耗，
   对照基线是模板默认。
4. 约 28% 已完成任务被设为延期（done_date > due_date），其中一部分在到期日前
   就有超耗、返修或阻塞评论，一部分看起来正常却延期（用于回顾式预警的 FN）。
5. 进行中任务按「意图」注入：逾期、超估、高返修、依赖阻塞、超 SLA、
   仅评论阻塞、反话、误导性「上次重做」、宽限期内、临界未超阈值。
   意图写在 manifest 里，不是事后用规则打的标签。
6. 评论全部为中文，含阻塞、反话、误导和正常评审。
7. 本环境 Zou 对新建人员做邮箱投递性检查，外网 DNS 被劫持后无法新建用户，
   因此任务都指派给官方镜像默认管理员。这是环境限制，不是产品设计。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import gazu
import gazu.cache

from pipesched.calendarutil import add_work_hours, as_date, is_workday, next_workday, work_hours_between
from pipesched.config import ProjectTemplate, Settings, load_all_templates, load_settings
from pipesched.kitsu_client import KitsuGateway, connect
from pipesched.models import days_to_seconds
from pipesched.paths import GOLD, ensure_data_dirs

SEED_DEFAULT = 42
AS_OF_DEFAULT = date(2026, 11, 13)

# ---------- 真实人天均值（与模板默认有意偏离）----------
TRUE_MEAN = {
    "p_mobile": {
        ("prop", "concept"): 0.8,
        ("prop", "production"): 1.6,
        ("prop", "ingest_qa"): 0.4,
        ("ui", "concept"): 0.6,
        ("ui", "production"): 1.0,
        ("ui", "ingest_qa"): 0.3,
        ("character", "concept"): 1.5,
        ("character", "production"): 2.8,
        ("character", "ingest_qa"): 0.6,
    },
    "p_character": {
        ("character", "concept"): 3.2,
        ("character", "modeling"): 5.5,
        ("character", "surfacing"): 4.0,
        ("character", "rigging"): 4.8,
        ("character", "animation"): 7.0,
        ("character", "vfx"): 2.5,
        ("prop", "concept"): 1.5,
        ("prop", "modeling"): 1.8,
        ("prop", "surfacing"): 1.6,
        ("prop", "rigging"): 1.2,
        ("prop", "animation"): 2.5,
        ("prop", "vfx"): 1.5,
    },
}

BLOCKERS = [
    ("角色布线要重做，边流太乱，绑不了。", "布线需要重做才能绑定"),
    ("等主美定发型，这个先搁着。", "等待主美定发型"),
    ("外包回来说源文件丢了，要重新要。", "源文件缺失"),
    ("等待物理骨骼方案，TA 这周没空。", "等待 TA 与骨骼方案"),
    ("贴图 UV 重叠，渲染有花，得重展。", "UV 重叠需重展"),
    ("等主美定最终字号，这个先搁着。", "等待主美决策"),
    ("这一版拓扑没法绑，要重做边流。", "拓扑无法绑定"),
]
SARCASM = [
    "又双叒重做？开玩笑的，这次一次过。",
    "完美，完全不用返修（真的，不是反话）。",
    "建议重做——如果这是三个月前的版本。现在过了。",
    "又一次完美通过（并没有）重做吧重做吧，开玩笑的，这次过了。",
]
MISLEADING = [
    "备注：上次重做是因为布线，这次已经修完了。",
    "重做记录见 v3 评论，v4 已通过。",
    "不要重做，保持现状。",
    "这个重做是上次的，这次拓扑没问题。",
]
NORMAL = [
    "预览过了，细节很好。",
    "按反馈改了高光，请再看一眼。",
    "进度正常，明天交。",
    "主美说风格方向对了，继续。",
    "已按走查意见收了边。",
    "提交评审，请看 v2 预览。",
    "按清单改了命名，准备入库。",
    "外包已交，等待评审。",
]

MOBILE_NAMES = {
    "prop": [
        "sword", "shield_round", "shield_rect", "spear", "axe", "bow", "arrow",
        "potion_red", "potion_blue", "coin", "chest", "key", "lantern", "barrel",
        "crate", "gem", "scroll", "hammer", "dagger", "helmet",
    ],
    "ui": [
        "btn_play", "btn_close", "btn_settings", "icon_shop", "icon_mail",
        "icon_gold", "banner_event", "panel_inventory", "slot_item", "avatar_frame",
        "hp_bar", "joystick", "icon_quest", "badge_vip", "cursor",
    ],
    "character": [
        "mascot_panda", "mascot_fox", "npc_merchant", "npc_guard", "hero_rook",
        "hero_sage", "pet_cat", "pet_bird", "crowd_a", "crowd_b", "boss_slime",
        "child_npc",
    ],
}
CHAR_NAMES = {
    "character": [
        "hero_ling", "hero_zhao", "hero_min", "npc_innkeeper", "npc_blacksmith",
        "npc_diviner", "boss_tiger", "boss_crane", "villager_a", "villager_b",
        "guard_captain", "child_lin", "elder_zhou", "scout_fei", "mage_yun",
        "monk_shan", "dancer_qiu", "hunter_wei", "prince_an", "rebel_kou",
        "sailor_he", "scholar_song",
    ],
    "prop": [
        "spear_royal", "lantern_palace", "sword_heirloom", "shield_garrison",
        "tea_set", "umbrella_oil", "flute", "seal_jade", "carriage", "banner_sect",
        "altar", "gong",
    ],
}


@dataclass
class TimeLog:
    date: str
    seconds: float


@dataclass
class PlannedComment:
    created_at: str
    text: str
    status: str  # todo/wip/retake/wfa/done
    gold_label: str  # blocker/sarcasm/misleading/normal/status
    gold_blocker_reason: Optional[str] = None


@dataclass
class TaskPlan:
    project_id_key: str
    asset_name: str
    asset_type: str  # template asset type id
    task_type_key: str
    status: str
    start: str
    due: str
    estimation_days: float
    actual_days: float
    retake_count: int
    time_logs: list
    comments: list
    done_date: Optional[str] = None
    end_date: Optional[str] = None
    was_late: bool = False
    # 生成时写入的意图，不是事后用规则打的
    intended_open_risks: list = field(default_factory=list)
    scenario: str = "historical_completed"
    note: str = ""
    kitsu_task_id: Optional[str] = None
    kitsu_asset_id: Optional[str] = None


def _lognormal(rng: random.Random, mean: float, sigma: float) -> float:
    """使 E[X]≈mean 的对数正态抽样，并截到合理区间。"""
    mean = max(0.15, mean)
    mu = math.log(mean) - 0.5 * sigma * sigma
    x = rng.lognormvariate(mu, sigma)
    return max(0.15, min(x, mean * 3.5))


def _iso(d: date) -> str:
    return d.isoformat()


def _dt(d: date, hour: int = 10, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute, 0).isoformat()


def split_time_logs(rng: random.Random, start: date, days: float, hours_per_day: float, workweek, holidays) -> list[TimeLog]:
    remaining = days * hours_per_day * 3600.0
    logs = []
    cur = next_workday(start, workweek, holidays)
    guard = 0
    while remaining > 1 and guard < 400:
        guard += 1
        if is_workday(cur, workweek, holidays):
            # 有时半天，有时整天
            frac = 0.5 if rng.random() < 0.25 else 1.0
            take = min(remaining, hours_per_day * frac * 3600.0)
            if take >= 600:
                logs.append(TimeLog(date=_iso(cur), seconds=round(take, 1)))
                remaining -= take
        cur += timedelta(days=1)
    if remaining > 600:
        logs.append(TimeLog(date=_iso(cur), seconds=round(remaining, 1)))
    return logs


def _pick_due(start: date, estimate_days: float, actual_days: float, late: bool, tmpl: ProjectTemplate, rng: random.Random) -> date:
    """late=True 时让 due 落在实际完成之前。"""
    workweek, holidays, hpd = tmpl.workweek, tmpl.holidays, tmpl.hours_per_day
    actual_end = add_work_hours(start, actual_days * hpd, hpd, workweek, holidays)
    if late:
        # 截止日期早于实际完成 1–4 个工作日
        pull = rng.randint(1, 4)
        due = add_work_hours(start, max(0.5, actual_days - pull) * hpd, hpd, workweek, holidays)
        if due >= actual_end:
            due = actual_end - timedelta(days=1)
            while due >= actual_end or not is_workday(due, workweek, holidays):
                due -= timedelta(days=1)
        return due
    # 准时：due 在实际完成当天或之后 0–2 个工作日
    buffer = rng.choice([0, 0, 1, 2])
    if buffer == 0:
        return actual_end
    return add_work_hours(actual_end + timedelta(days=1), buffer * hpd, hpd, workweek, holidays)


def _historical_task(
    rng: random.Random,
    tmpl: ProjectTemplate,
    asset_name: str,
    asset_type: str,
    task_key: str,
    done_center: date,
) -> TaskPlan:
    mean = TRUE_MEAN[tmpl.id][(asset_type, task_key)]
    retakes = 0
    r = rng.random()
    # 建模 / 绑定 更容易返修
    bump = 0.12 if task_key in {"modeling", "rigging", "production"} else 0.06
    if r < bump:
        retakes = 1
    elif r < bump + 0.04:
        retakes = 2
    elif r < bump + 0.055:
        retakes = 3
    actual = _lognormal(rng, mean * (1 + 0.22 * retakes), 0.28)
    default = tmpl.default_days(task_key)
    late = rng.random() < 0.28
    # 完成日落在 2026-07-20 到 2026-10-31
    jitter = rng.randint(-50, 40)
    done = done_center + timedelta(days=jitter)
    # 推到工作日
    done = next_workday(done, tmpl.workweek, tmpl.holidays)
    if done >= AS_OF_DEFAULT - timedelta(days=10):
        done = next_workday(date(2026, 10, 20) - timedelta(days=rng.randint(0, 20)), tmpl.workweek, tmpl.holidays)
    start = done
    # 往前推实际工时
    # 反推 start：从 done 往回走 actual 人天
    hours_needed = actual * tmpl.hours_per_day
    cur = done
    acc = 0.0
    while acc < hours_needed:
        if is_workday(cur, tmpl.workweek, tmpl.holidays):
            acc += tmpl.hours_per_day
        cur -= timedelta(days=1)
        if (done - cur).days > 400:
            break
    start = next_workday(cur + timedelta(days=1), tmpl.workweek, tmpl.holidays)
    due = _pick_due(start, default, actual, late, tmpl, rng)
    was_late = done > due
    logs = split_time_logs(rng, start, actual, tmpl.hours_per_day, tmpl.workweek, tmpl.holidays)

    comments: list[PlannedComment] = []
    comments.append(PlannedComment(_dt(start, 9, 30), "开始制作。", "wip", "status"))
    # 返修评论
    span = max(0, (done - start).days)
    mid = start + timedelta(days=max(0, min(span - 1, span // 2)))
    mid = next_workday(mid, tmpl.workweek, tmpl.holidays)
    if mid >= done:
        mid = start
    for i in range(retakes):
        rday = mid + timedelta(days=i)
        if rday >= done:
            rday = done - timedelta(days=1)
        if rday < start:
            rday = start
        comments.append(PlannedComment(_dt(rday, 15, 10), f"评审不通过，第 {i+1} 次返修。", "retake", "status"))
    # 额外自然语言
    roll = rng.random()
    if was_late and roll < 0.55:
        text, reason = rng.choice(BLOCKERS)
        # 放在 due 之前，回顾式评测能看到
        cday = due - timedelta(days=rng.randint(1, 3)) if due > start else start
        cday = max(start, min(cday, done - timedelta(days=1)))
        comments.append(PlannedComment(_dt(cday, 16, 0), text, "wip", "blocker", reason))
    elif was_late and roll < 0.7:
        comments.append(PlannedComment(_dt(mid, 11, 0), rng.choice(NORMAL), "wip", "normal"))
    elif (not was_late) and roll < 0.12:
        comments.append(PlannedComment(_dt(mid, 14, 0), rng.choice(SARCASM), "wip", "sarcasm"))
    elif (not was_late) and roll < 0.2:
        comments.append(PlannedComment(_dt(mid, 14, 20), rng.choice(MISLEADING), "wip", "misleading"))
    elif roll < 0.55:
        comments.append(PlannedComment(_dt(mid, 11, 20), rng.choice(NORMAL), "wip", "normal"))
    # 所有非「完成」评论必须早于 done，否则 Zou 按 created_at 排最终状态会把任务留在 wip/retake。
    comments.append(PlannedComment(_dt(done, 18, 0), "完成，预览已过。", "done", "status"))
    done_ts = _dt(done, 18, 0)
    clamped = []
    for c in comments:
        if c.status != "done" and c.created_at >= done_ts:
            # 推到完成前一个工作时段
            c = PlannedComment(_dt(done, 16, 0), c.text, c.status, c.gold_label, c.gold_blocker_reason)
        clamped.append(c)
    comments = clamped
    comments.sort(key=lambda c: (c.created_at, 1 if c.status == "done" else 0))

    # 回顾式「到期前 2 天看起来危险」的意图，只作记录
    intended = []
    if was_late:
        intended.append("actually_late")
    return TaskPlan(
        project_id_key=tmpl.id,
        asset_name=asset_name,
        asset_type=asset_type,
        task_type_key=task_key,
        status="done",
        start=_iso(start),
        due=_iso(due),
        estimation_days=default,
        actual_days=round(actual, 3),
        retake_count=retakes,
        time_logs=logs,
        comments=comments,
        done_date=_iso(done),
        end_date=_iso(done),
        was_late=was_late,
        intended_open_risks=intended,
        scenario="historical_completed",
        note="已完成历史任务，用于估时 MAPE 与回顾式风险评测。",
    )


def _names(pool: list[str], prefix: str, n: int) -> list[str]:
    out = []
    i = 0
    while len(out) < n:
        if i < len(pool):
            out.append(f"{prefix}_{pool[i]}")
        else:
            out.append(f"{prefix}_{pool[i % len(pool)]}_{i}")
        i += 1
    return out


def plan_historical_mobile(rng: random.Random, tmpl: ProjectTemplate) -> list[TaskPlan]:
    plans = []
    # 14 prop + 12 ui + 12 character 全部做完三条工序
    groups = [("prop", 14), ("ui", 12), ("character", 12)]
    names_used = {}
    for at, n in groups:
        names = _names(MOBILE_NAMES[at], f"m_{at}", n)
        names_used[at] = names
        for name in names:
            for key in tmpl.topo_order():
                plans.append(_historical_task(rng, tmpl, name, at, key, date(2026, 9, 15)))
    return plans


def plan_historical_character(rng: random.Random, tmpl: ProjectTemplate) -> list[TaskPlan]:
    plans = []
    groups = [("character", 16), ("prop", 8)]
    for at, n in groups:
        names = _names(CHAR_NAMES[at], f"c_{at}", n)
        for name in names:
            for key in tmpl.topo_order():
                plans.append(_historical_task(rng, tmpl, name, at, key, date(2026, 9, 10)))
    return plans


def _open_base(tmpl: ProjectTemplate, asset_name, asset_type, key, status, start, due, actual_days, retakes, comments, intended, scenario, note, extra_logs=None) -> TaskPlan:
    default = tmpl.default_days(key)
    logs = extra_logs or split_time_logs(random.Random(0), start, actual_days, tmpl.hours_per_day, tmpl.workweek, tmpl.holidays)
    return TaskPlan(
        project_id_key=tmpl.id,
        asset_name=asset_name,
        asset_type=asset_type,
        task_type_key=key,
        status=status,
        start=_iso(start),
        due=_iso(due),
        estimation_days=default,
        actual_days=round(actual_days, 3),
        retake_count=retakes,
        time_logs=logs,
        comments=comments,
        intended_open_risks=intended,
        scenario=scenario,
        note=note,
    )


def plan_open_mobile(rng: random.Random, tmpl: ProjectTemplate) -> list[TaskPlan]:
    """进行中样本，每条都有写明的注入意图。"""
    as_of = AS_OF_DEFAULT
    ww, hol, hpd = tmpl.workweek, tmpl.holidays, tmpl.hours_per_day
    out: list[TaskPlan] = []

    def C(day, h, m, text, st, lab, reason=None):
        return PlannedComment(_dt(day, h, m), text, st, lab, reason)

    # 1. 逾期：due 11-08，wip
    start = date(2026, 11, 3)
    due = date(2026, 11, 8)
    actual = 1.5
    logs = split_time_logs(rng, start, actual, hpd, ww, hol)
    out.append(_open_base(
        tmpl, "m_ui_open_shop", "ui", "production", "wip", start, due, actual, 0,
        [C(start, 9, 0, "开始制作商店图标。", "wip", "status"),
         C(date(2026, 11, 7), 16, 0, "还差切图规范，进度比想的慢。", "wip", "normal")],
        ["overdue"], "open_overdue",
        "注入逾期：due=2026-11-08，as_of=2026-11-13，模板宽限 0 天，未完成。",
        extra_logs=logs,
    ))
    # 前置原画完成
    out.append(_historical_like_done(tmpl, "m_ui_open_shop", "ui", "concept", date(2026, 10, 28), date(2026, 11, 1), 0.6, rng))

    # 2. 超估：due 在未来，实耗 / 估时 ≥ 1.2
    # 制作默认 2 人天，实耗 2.8
    start = date(2026, 11, 4)
    due = date(2026, 11, 20)
    actual = 2.8
    logs = split_time_logs(rng, start, actual, hpd, ww, hol)
    out.append(_open_base(
        tmpl, "m_prop_open_chest", "prop", "production", "wip", start, due, actual, 0,
        [C(start, 9, 30, "开始做箱子。", "wip", "status"),
         C(date(2026, 11, 11), 15, 0, "细节比估计多，还在抠锁扣。", "wip", "normal")],
        ["overrun"], "open_overrun",
        "注入超估：实耗 2.8 人天 / 模板估时 2.0 = 1.4 ≥ 1.2，due 在未来故不应报逾期。",
        extra_logs=logs,
    ))
    out.append(_historical_like_done(tmpl, "m_prop_open_chest", "prop", "concept", date(2026, 10, 27), date(2026, 10, 31), 0.8, rng))

    # 3. 高返修：retake=3 ≥ 2
    start = date(2026, 11, 2)
    due = date(2026, 11, 21)
    actual = 1.8
    logs = split_time_logs(rng, start, actual, hpd, ww, hol)
    out.append(_open_base(
        tmpl, "m_character_open_panda", "character", "production", "retake", start, due, actual, 3,
        [C(start, 10, 0, "开始做熊猫吉祥物。", "wip", "status"),
         C(date(2026, 11, 5), 15, 0, "评审不通过，第 1 次返修。", "retake", "status"),
         C(date(2026, 11, 7), 15, 0, "评审不通过，第 2 次返修。", "retake", "status"),
         C(date(2026, 11, 10), 15, 0, "评审不通过，第 3 次返修。", "retake", "status")],
        ["high_retake"], "open_high_retake",
        "注入高返修：retake_count=3，阈值 2。日期未逾期。",
        extra_logs=logs,
    ))
    out.append(_historical_like_done(tmpl, "m_character_open_panda", "character", "concept", date(2026, 10, 20), date(2026, 10, 26), 1.4, rng))

    # 4. 依赖阻塞：制作已开始，原画未完成
    start_c = date(2026, 11, 6)
    due_c = date(2026, 11, 18)
    start_p = date(2026, 11, 10)
    due_p = date(2026, 11, 22)
    out.append(_open_base(
        tmpl, "m_prop_open_lantern", "prop", "concept", "wip", start_c, due_c, 0.5, 0,
        [C(start_c, 10, 0, "原画还在改轮廓。", "wip", "status")],
        [], "open_dep_pred",
        "依赖阻塞场景的前序：原画未完成。本身日期未逾期。",
        extra_logs=split_time_logs(rng, start_c, 0.5, hpd, ww, hol),
    ))
    out.append(_open_base(
        tmpl, "m_prop_open_lantern", "prop", "production", "wip", start_p, due_p, 0.4, 0,
        [C(start_p, 11, 0, "按旧稿先做了模型，原画其实还没定。", "wip", "normal")],
        ["dep_blocked"], "open_dep_blocked",
        "注入依赖阻塞：制作 start=2026-11-10 已到，同资产原画仍为 wip。",
        extra_logs=split_time_logs(rng, start_p, 0.4, hpd, ww, hol),
    ))

    # 5. 超 SLA：wfa，最后评论 11-10，SLA=1
    start = date(2026, 11, 5)
    due = date(2026, 11, 21)
    out.append(_open_base(
        tmpl, "m_ui_open_ok", "ui", "ingest_qa", "wfa", start, due, 0.3, 0,
        [C(start, 10, 0, "开始入库检查。", "wip", "status"),
         C(date(2026, 11, 10), 17, 0, "提交评审，请看 v2 预览。", "wfa", "normal")],
        ["sla_overdue"], "open_sla",
        "注入超 SLA：11-10 进入 wfa，as_of=11-13，SLA=1 天。",
        extra_logs=split_time_logs(rng, start, 0.3, hpd, ww, hol),
    ))
    out.append(_historical_like_done(tmpl, "m_ui_open_ok", "ui", "concept", date(2026, 10, 25), date(2026, 10, 28), 0.5, rng))
    out.append(_historical_like_done(tmpl, "m_ui_open_ok", "ui", "production", date(2026, 10, 29), date(2026, 11, 4), 1.0, rng))

    # 6. 临界未超阈值：实耗/估时=1.15 < 1.2，due 是今天，不应报逾期或超估
    start = date(2026, 11, 9)
    due = date(2026, 11, 13)
    actual = 2.0 * 1.15  # 2.3，对 production 默认 2.0
    # wait, this is a prop? use production of a prop default 2.0, 2.3/2.0=1.15
    out.append(_open_base(
        tmpl, "m_prop_open_coin", "prop", "production", "wip", start, due, actual, 0,
        [C(start, 9, 0, "进度正常，明天交。", "wip", "normal")],
        [], "open_near_miss",
        "临界负例：due=as_of 当天（宽限 0，规则应不报逾期）；实耗/估时=1.15 < 1.2，不报超估。",
        extra_logs=split_time_logs(rng, start, actual, hpd, ww, hol),
    ))
    out.append(_historical_like_done(tmpl, "m_prop_open_coin", "prop", "concept", date(2026, 11, 2), date(2026, 11, 6), 0.7, rng))

    # 7. 仅评论阻塞
    start = date(2026, 11, 6)
    due = date(2026, 11, 25)
    out.append(_open_base(
        tmpl, "m_ui_open_banner", "ui", "production", "wip", start, due, 0.8, 0,
        [C(start, 10, 0, "开始做活动横幅。", "wip", "status"),
         C(date(2026, 11, 11), 16, 30, "等主美定最终字号，这个先搁着。", "wip", "blocker", "等待主美决策")],
        ["comment_blocker"], "open_comment_blocker",
        "仅评论阻塞：日期与工时看起来正常，备注写「等主美…先搁着」。规则层不应报，关键词/LLM 应报。",
        extra_logs=split_time_logs(rng, start, 0.8, hpd, ww, hol),
    ))
    out.append(_historical_like_done(tmpl, "m_ui_open_banner", "ui", "concept", date(2026, 10, 30), date(2026, 11, 4), 0.6, rng))

    # 8. 反话
    start = date(2026, 11, 7)
    due = date(2026, 11, 24)
    out.append(_open_base(
        tmpl, "m_prop_open_sword", "prop", "production", "wip", start, due, 0.9, 0,
        [C(start, 10, 0, "开始做剑。", "wip", "status"),
         C(date(2026, 11, 12), 14, 0, "又双叒重做？开玩笑的，这次一次过。", "wip", "sarcasm")],
        [], "open_sarcasm",
        "反话评论：含「重做」但「开玩笑的，这次一次过」。关键词基线预期误报，真值不是阻塞。",
        extra_logs=split_time_logs(rng, start, 0.9, hpd, ww, hol),
    ))
    out.append(_historical_like_done(tmpl, "m_prop_open_sword", "prop", "concept", date(2026, 11, 1), date(2026, 11, 5), 0.8, rng))

    # 9. 误导：上次重做
    start = date(2026, 11, 5)
    due = date(2026, 11, 26)
    out.append(_open_base(
        tmpl, "m_ui_open_close", "ui", "production", "wip", start, due, 0.7, 0,
        [C(start, 10, 0, "开始做关闭按钮。", "wip", "status"),
         C(date(2026, 11, 11), 11, 0, "这个重做是上次的，这次拓扑没问题。", "wip", "misleading")],
        [], "open_misleading",
        "误导评论：在说上次的重做，这次没问题。关键词基线预期误报。",
        extra_logs=split_time_logs(rng, start, 0.7, hpd, ww, hol),
    ))
    out.append(_historical_like_done(tmpl, "m_ui_open_close", "ui", "concept", date(2026, 10, 27), date(2026, 10, 30), 0.5, rng))

    # 10. 待排期：全新资产，todo 无日期，给排期建议演示
    for key in tmpl.topo_order():
        out.append(TaskPlan(
            project_id_key=tmpl.id,
            asset_name="m_prop_newpack",
            asset_type="prop",
            task_type_key=key,
            status="todo",
            start="",
            due="",
            estimation_days=tmpl.default_days(key),
            actual_days=0,
            retake_count=0,
            time_logs=[],
            comments=[],
            intended_open_risks=[],
            scenario="unscheduled",
            note="无日期的待排任务，用于演示 DAG 排期建议。写回需制片确认。",
        ))
    return out


def _historical_like_done(tmpl, name, at, key, start, done, actual, rng) -> TaskPlan:
    default = tmpl.default_days(key)
    due = add_work_hours(start, default * tmpl.hours_per_day, tmpl.hours_per_day, tmpl.workweek, tmpl.holidays)
    if due < done:
        # 让这个前序不算「要测的延期样本」也可以，但尽量准时
        due = done
    logs = split_time_logs(rng, start, actual, tmpl.hours_per_day, tmpl.workweek, tmpl.holidays)
    comments = [
        PlannedComment(_dt(start, 9, 30), "开始。", "wip", "status"),
        PlannedComment(_dt(done, 18, 0), "完成。", "done", "status"),
    ]
    return TaskPlan(
        project_id_key=tmpl.id,
        asset_name=name,
        asset_type=at,
        task_type_key=key,
        status="done",
        start=_iso(start),
        due=_iso(due),
        estimation_days=default,
        actual_days=actual,
        retake_count=0,
        time_logs=logs,
        comments=comments,
        done_date=_iso(done),
        end_date=_iso(done),
        was_late=done > due,
        intended_open_risks=[],
        scenario="support_completed",
        note="为进行中资产补齐已完成的前序工序。",
    )


def plan_open_character(rng: random.Random, tmpl: ProjectTemplate) -> list[TaskPlan]:
    as_of = AS_OF_DEFAULT
    ww, hol, hpd = tmpl.workweek, tmpl.holidays, tmpl.hours_per_day
    out: list[TaskPlan] = []

    def C(day, h, m, text, st, lab, reason=None):
        return PlannedComment(_dt(day, h, m), text, st, lab, reason)

    def done_prefix(name, at, last_done_key, start0):
        """把 last_done_key 及之前的工序标为完成。"""
        acc = start0
        for key in tmpl.topo_order():
            d = tmpl.default_days(key) * 0.9
            st = acc
            en = add_work_hours(st, d * hpd, hpd, ww, hol)
            out.append(_historical_like_done(tmpl, name, at, key, st, en, d, rng))
            acc = en + timedelta(days=1)
            if key == last_done_key:
                break
        return acc

    # 1. 逾期超出宽限：due 11-10，grace=1，deadline 11-11，as_of 11-13
    name = "c_character_hero_demo"
    next_start = done_prefix(name, "character", "concept", date(2026, 9, 1))
    start = date(2026, 10, 27)
    due = date(2026, 11, 10)
    actual = 4.0
    out.append(_open_base(
        tmpl, name, "character", "modeling", "wip", start, due, actual, 0,
        [C(start, 10, 0, "角色建模进行中。", "wip", "status")],
        ["overdue"], "open_overdue",
        "注入逾期：due=2026-11-10，宽限 1 天，截止 11-11，as_of=11-13。",
        extra_logs=split_time_logs(rng, start, actual, hpd, ww, hol),
    ))

    # 2. 宽限期内：due=11-12，grace=1，deadline=11-13 == as_of，不应报逾期
    name = "c_character_grace_window"
    done_prefix(name, "character", "concept", date(2026, 9, 5))
    start = date(2026, 11, 2)
    due = date(2026, 11, 12)
    out.append(_open_base(
        tmpl, name, "character", "modeling", "wip", start, due, 2.0, 0,
        [C(start, 10, 0, "还在宽限期内，今天截止宽限。", "wip", "normal")],
        [], "open_in_grace",
        "宽限期内负例：due=11-12，宽限 1 天，deadline=11-13，as_of<=deadline，规则按设计不报逾期。这是产品取舍，不是漏报。",
        extra_logs=split_time_logs(rng, start, 2.0, hpd, ww, hol),
    ))

    # 3. 高返修：阈值 1，retake=1 应报
    name = "c_character_retake_one"
    done_prefix(name, "character", "modeling", date(2026, 8, 20))
    start = date(2026, 11, 1)
    due = date(2026, 11, 25)
    out.append(_open_base(
        tmpl, name, "character", "surfacing", "retake", start, due, 2.0, 1,
        [C(start, 10, 0, "开始贴图。", "wip", "status"),
         C(date(2026, 11, 8), 16, 0, "评审不通过，第 1 次返修。", "retake", "status")],
        ["high_retake"], "open_high_retake",
        "注入高返修：retake_count=1，本模板阈值 1（比手游更严）。",
        extra_logs=split_time_logs(rng, start, 2.0, hpd, ww, hol),
    ))

    # 4. 超估：贴图默认 3.5，实耗 3.5*1.2=4.2，阈值 1.15
    name = "c_character_overrun"
    done_prefix(name, "character", "modeling", date(2026, 8, 10))
    start = date(2026, 10, 28)
    due = date(2026, 11, 28)
    actual = 4.3
    out.append(_open_base(
        tmpl, name, "character", "surfacing", "wip", start, due, actual, 0,
        [C(start, 10, 0, "贴图工作量比估计大。", "wip", "normal")],
        ["overrun"], "open_overrun",
        "注入超估：实耗 4.3 / 估时 3.5 ≈ 1.23 ≥ 1.15，due 在未来。",
        extra_logs=split_time_logs(rng, start, actual, hpd, ww, hol),
    ))

    # 5. 依赖阻塞：绑定已开始，贴图未完成
    name = "c_character_dep_block"
    done_prefix(name, "character", "modeling", date(2026, 8, 5))
    start_s = date(2026, 11, 3)
    due_s = date(2026, 11, 24)
    start_r = date(2026, 11, 9)
    due_r = date(2026, 11, 28)
    out.append(_open_base(
        tmpl, name, "character", "surfacing", "wip", start_s, due_s, 1.5, 0,
        [C(start_s, 10, 0, "贴图还没过。", "wip", "status")],
        [], "open_dep_pred",
        "依赖阻塞的前序贴图，仍为 wip。",
        extra_logs=split_time_logs(rng, start_s, 1.5, hpd, ww, hol),
    ))
    out.append(_open_base(
        tmpl, name, "character", "rigging", "wip", start_r, due_r, 0.8, 0,
        [C(start_r, 11, 0, "先按临时模型绑了，贴图还没过。", "wip", "normal")],
        ["dep_blocked"], "open_dep_blocked",
        "注入依赖阻塞：绑定 start 已到，同资产贴图未完成（模板规定 绑定 依赖 贴图）。",
        extra_logs=split_time_logs(rng, start_r, 0.8, hpd, ww, hol),
    ))

    # 6. SLA：wfa 4 天，SLA=3
    name = "c_character_sla"
    done_prefix(name, "character", "rigging", date(2026, 7, 15))
    start = date(2026, 11, 2)
    due = date(2026, 11, 27)
    out.append(_open_base(
        tmpl, name, "character", "animation", "wfa", start, due, 2.0, 0,
        [C(start, 10, 0, "开始动画。", "wip", "status"),
         C(date(2026, 11, 8), 17, 30, "提交评审，请看 v2 预览。", "wfa", "normal")],
        ["sla_overdue"], "open_sla",
        "注入超 SLA：11-08 进入 wfa，as_of=11-13，已等 5 天，SLA=3。",
        extra_logs=split_time_logs(rng, start, 2.0, hpd, ww, hol),
    ))

    # 7. 仅评论阻塞
    name = "c_character_wiring"
    done_prefix(name, "character", "surfacing", date(2026, 7, 20))
    start = date(2026, 11, 4)
    due = date(2026, 11, 28)
    out.append(_open_base(
        tmpl, name, "character", "rigging", "wip", start, due, 1.5, 0,
        [C(start, 10, 0, "开始绑定。", "wip", "status"),
         C(date(2026, 11, 11), 15, 40, "角色布线要重做，边流太乱，绑不了。", "wip", "blocker", "布线需要重做才能绑定")],
        ["comment_blocker"], "open_comment_blocker",
        "仅评论阻塞：规则层日期/工时正常；备注明确「布线要重做」。",
        extra_logs=split_time_logs(rng, start, 1.5, hpd, ww, hol),
    ))

    # 8. 反话
    name = "c_prop_sarcasm"
    done_prefix(name, "prop", "concept", date(2026, 10, 1))
    start = date(2026, 11, 6)
    due = date(2026, 11, 27)
    out.append(_open_base(
        tmpl, name, "prop", "modeling", "wip", start, due, 0.8, 0,
        [C(start, 10, 0, "开始做道具。", "wip", "status"),
         C(date(2026, 11, 12), 13, 0, "建议重做——如果这是三个月前的版本。现在过了。", "wip", "sarcasm")],
        [], "open_sarcasm",
        "反话评论，关键词可能误报「重做」。",
        extra_logs=split_time_logs(rng, start, 0.8, hpd, ww, hol),
    ))

    # 9. 误导
    name = "c_prop_misleading"
    done_prefix(name, "prop", "concept", date(2026, 10, 4))
    start = date(2026, 11, 5)
    due = date(2026, 11, 26)
    out.append(_open_base(
        tmpl, name, "prop", "modeling", "wip", start, due, 0.7, 0,
        [C(start, 10, 0, "开始建模。", "wip", "status"),
         C(date(2026, 11, 10), 12, 0, "备注：上次重做是因为布线，这次已经修完了。", "wip", "misleading")],
        [], "open_misleading",
        "误导评论，关键词可能把「重做」当当前阻塞。",
        extra_logs=split_time_logs(rng, start, 0.7, hpd, ww, hol),
    ))

    # 10. 待排期新角色
    for key in tmpl.topo_order():
        out.append(TaskPlan(
            project_id_key=tmpl.id,
            asset_name="c_character_new_npc",
            asset_type="character",
            task_type_key=key,
            status="todo",
            start="",
            due="",
            estimation_days=tmpl.default_days(key),
            actual_days=0,
            retake_count=0,
            time_logs=[],
            comments=[],
            intended_open_risks=[],
            scenario="unscheduled",
            note="无日期待排任务，用于角色全流程 DAG 排期演示。",
        ))
    return out


def build_all_plans(seed: int = SEED_DEFAULT) -> dict[str, list[TaskPlan]]:
    rng = random.Random(seed)
    tmpls = load_all_templates()
    return {
        "p_mobile": plan_historical_mobile(rng, tmpls["p_mobile"]) + plan_open_mobile(rng, tmpls["p_mobile"]),
        "p_character": plan_historical_character(rng, tmpls["p_character"]) + plan_open_character(rng, tmpls["p_character"]),
    }


# ---------------- Kitsu I/O ----------------

def _remove_project_if_exists(name: str) -> None:
    p = gazu.project.get_project_by_name(name)
    if not p:
        return
    try:
        gazu.project.close_project(p)
    except Exception:
        pass
    try:
        gazu.project.remove_project(p, force=True)
    except Exception as e:
        print(f"  警告：删除项目 {name} 失败：{e}")


def _ensure_asset_type(name: str) -> dict:
    t = gazu.asset.get_asset_type_by_name(name)
    if t:
        return t
    return gazu.asset.new_asset_type(name)


def _ensure_task_type(name: str, color: str) -> dict:
    t = gazu.task.get_task_type_by_name(name, "Asset")
    if t:
        return t
    return gazu.task.new_task_type(name, color=color, for_entity="Asset")


def _status(short: str) -> dict:
    st = gazu.task.get_task_status_by_short_name(short)
    if not st:
        raise RuntimeError(f"missing task status {short}")
    return st


def create_project(tmpl: ProjectTemplate, admin) -> dict:
    _remove_project_if_exists(tmpl.kitsu_project_name)
    statuses = gazu.task.all_task_statuses()
    asset_types = [_ensure_asset_type(a.kitsu_name) for a in tmpl.asset_types]
    task_types = [_ensure_task_type(t.name, t.color) for t in tmpl.task_types]
    proj = gazu.project.new_project(
        tmpl.kitsu_project_name,
        production_type="short",
        production_style=tmpl.production_style,
        asset_types=asset_types,
        task_statuses=statuses,
        task_types=task_types,
    )
    for i, tt in enumerate(task_types):
        try:
            gazu.project.add_task_type(proj, tt, priority=i)
        except Exception:
            pass
    for at in asset_types:
        try:
            gazu.project.add_asset_type(proj, at)
        except Exception:
            pass
    try:
        gazu.project.add_person_to_team(proj, admin)
    except Exception as e:
        print("  加入团队失败：", e)
    # 描述写成合成数据声明
    try:
        proj = gazu.project.get_project(proj["id"])
        proj["description"] = "[SYNTHETIC] " + tmpl.description
        gazu.project.update_project(proj)
    except Exception:
        pass
    return gazu.project.get_project(proj["id"])


def push_task(plan: TaskPlan, asset, task_type, admin, tmpl: ProjectTemplate) -> str:
    statuses = {s: _status(s) for s in ("todo", "wip", "retake", "wfa", "done", "ready")}
    initial = statuses.get(plan.status if plan.status in statuses else "todo", statuses["todo"])
    # 先以 todo 建，再用评论推状态（这样 created_at 能控制最终状态）
    task = gazu.task.new_task(asset, task_type, task_status=statuses["todo"], assignees=[admin])
    tid = task["id"]
    t = gazu.task.get_task(tid)
    if plan.start:
        t["start_date"] = plan.start
    if plan.due:
        t["due_date"] = plan.due
    t["estimation"] = days_to_seconds(plan.estimation_days, tmpl.hours_per_day)
    t = gazu.task.update_task(t)
    for log in plan.time_logs:
        if log.seconds and log.date:
            try:
                gazu.task.set_time_spent(t, admin, log.date, int(round(log.seconds)))
            except Exception as e:
                print(f"  time_spent fail {plan.asset_name}/{plan.task_type_key} {log.date}: {e}")
    # 评论按时间
    for c in sorted(plan.comments, key=lambda x: x.created_at):
        st = statuses.get(c.status, statuses["wip"])
        try:
            gazu.task.add_comment(t, st, comment=c.text, person=admin, created_at=c.created_at)
        except Exception as e:
            print(f"  comment fail {plan.asset_name}: {e}")
    # 若没有任何评论但需要处于某状态（纯 todo 无日期）
    if not plan.comments and plan.status and plan.status != "todo":
        gazu.task.add_comment(t, statuses.get(plan.status, statuses["todo"]), comment="")
    t = gazu.task.get_task(tid)
    t["retake_count"] = plan.retake_count
    if plan.end_date:
        t["end_date"] = plan.end_date
    if plan.start:
        t["start_date"] = plan.start
    if plan.due:
        t["due_date"] = plan.due
    gazu.task.update_task(t)
    return tid


def _plan_to_dict(p: TaskPlan) -> dict:
    d = asdict(p)
    # TimeLog / PlannedComment already asdict via dataclass
    return d


def run_seed(seed: int = SEED_DEFAULT, settings: Optional[Settings] = None) -> dict:
    settings = settings or load_settings()
    connect(settings)
    gazu.cache.disable()
    admin = gazu.person.get_person_by_email(settings.kitsu_email)
    if not admin:
        raise RuntimeError("cannot load kitsu admin person")
    templates = load_all_templates(settings)
    plans = build_all_plans(seed)
    ensure_data_dirs()

    # 清掉沙箱
    _remove_project_if_exists("PipeSched-Sandbox")

    manifest = {
        "synthetic": True,
        "seed": seed,
        "as_of": AS_OF_DEFAULT.isoformat(),
        "generated_by": "pipesched.seed",
        "note": "全部为合成数据，不是真实项目组或腾讯内部数据。",
        "person_limitation": "Zou 邮箱投递性检查在本环境因 DNS 劫持无法新建人员，任务均指派给镜像默认 admin@example.com。",
        "true_mean_days": {k: {f"{a}|{t}": v for (a, t), v in tbl.items()} for k, tbl in TRUE_MEAN.items()},
        "projects": {},
    }

    for key, tmpl in templates.items():
        print(f"== 创建项目 {tmpl.kitsu_project_name} ==")
        proj = create_project(tmpl, admin)
        asset_types = {a.kitsu_name: _ensure_asset_type(a.kitsu_name) for a in tmpl.asset_types}
        at_by_id = {a.id: asset_types[a.kitsu_name] for a in tmpl.asset_types}
        task_types = {t.id: _ensure_task_type(t.name, t.color) for t in tmpl.task_types}
        assets: dict[str, dict] = {}
        project_plans = plans[key]
        # 按资产建
        n = 0
        for plan in project_plans:
            if plan.asset_name not in assets:
                at = at_by_id[plan.asset_type]
                desc = f"[SYNTHETIC] seed={seed} template={tmpl.id} asset_type={plan.asset_type}"
                assets[plan.asset_name] = gazu.asset.new_asset(proj, at, plan.asset_name, description=desc)
            asset = assets[plan.asset_name]
            plan.kitsu_asset_id = asset["id"]
            tt = task_types[plan.task_type_key]
            tid = push_task(plan, asset, tt, admin, tmpl)
            plan.kitsu_task_id = tid
            n += 1
            if n % 25 == 0:
                print(f"  {n} tasks pushed...")
        print(f"  完成 {n} 条任务，{len(assets)} 个资产。")
        manifest["projects"][key] = {
            "kitsu_id": proj["id"],
            "kitsu_name": tmpl.kitsu_project_name,
            "template_id": tmpl.id,
            "n_assets": len(assets),
            "n_tasks": n,
            "tasks": [_plan_to_dict(p) for p in project_plans],
        }

    # 快照
    gw = KitsuGateway(settings, connect_now=False)
    snap_projects = []
    for key, tmpl in templates.items():
        proj = gazu.project.get_project_by_name(tmpl.kitsu_project_name)
        snap_projects.append(proj)
    snapshot = gw.snapshot(snap_projects, templates)
    # 把金标贴到 snapshot 的评论上
    text_label = {}
    for key, block in manifest["projects"].items():
        for tp in block["tasks"]:
            for c in tp["comments"]:
                text_label[(tp["kitsu_task_id"], c["text"])] = c
    for block in snapshot["projects"]:
        for t in block["tasks"]:
            for c in t.get("comments") or []:
                meta = text_label.get((t["id"], c.get("text") or ""))
                if meta:
                    c["gold_label"] = meta.get("gold_label")
                    c["gold_blocker_reason"] = meta.get("gold_blocker_reason")

    (GOLD / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    (GOLD / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print("已写入 data/gold/manifest.json 与 snapshot.json")
    return manifest


def _json_default(o):
    if hasattr(o, "__dict__") and not isinstance(o, dict):
        try:
            return asdict(o)
        except Exception:
            return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


if __name__ == "__main__":
    run_seed()
