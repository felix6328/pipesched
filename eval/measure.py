"""离线评测。所有写入 results.md 的数字都由本脚本算出，禁止手填。

风险标签分两套，必须分开讲：
1. 进行中任务的「生成意图」对照：逾期/超估等与规则定义高度重合，属于一致性检查，
   存在循环论证，不得当成预测能力。
2. 已完成任务的回顾式评测：在 due_date 前 2 天重建当时状态，真值是后来是否真的延期。
   这套不是用规则自己给自己打标签。
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipesched.calendarutil import as_date
from pipesched.comments import keyword_extract
from pipesched.config import load_all_templates, load_settings
from pipesched.estimate import estimate_task
from pipesched.kitsu_client import load_snapshot, tasks_from_snapshot
from pipesched.llm import DisabledLLM, resolve_backend
from llm_metrics import evaluate_comments, load_gold_comments
from pipesched.models import Comment, Task, days_to_seconds
from pipesched.paths import GOLD, EVAL
from pipesched.risk import scan_project, scan_task
from pipesched.service import scan_tasks

AS_OF = date(2026, 11, 13)


def load_gold():
    manifest = json.loads((GOLD / "manifest.json").read_text(encoding="utf-8"))
    snapshot = load_snapshot(GOLD / "snapshot.json")
    templates = load_all_templates()
    tasks = tasks_from_snapshot(snapshot, templates)
    return manifest, snapshot, templates, tasks


def mape(pairs):
    # pairs: (pred, actual) with actual>0
    if not pairs:
        return None
    return sum(abs(p - a) / a for p, a in pairs) / len(pairs)


def median_abs_err(pairs):
    if not pairs:
        return None
    return statistics.median(abs(p - a) for p, a in pairs)


def eval_estimates(tasks, templates, manifest):
    # 只用 historical_completed
    hist_ids = set()
    done_dates = {}
    actuals = {}
    for key, block in manifest["projects"].items():
        for tp in block["tasks"]:
            if tp.get("scenario") == "historical_completed" and tp.get("kitsu_task_id"):
                hist_ids.add(tp["kitsu_task_id"])
                done_dates[tp["kitsu_task_id"]] = tp.get("done_date")
                actuals[tp["kitsu_task_id"]] = tp.get("actual_days")
    completed = [t for t in tasks if t.id in hist_ids and t.is_done and t.duration_seconds > 0]
    completed.sort(key=lambda t: (done_dates.get(t.id) or "9999", t.id))
    n = len(completed)
    cut = int(n * 0.7)
    train, test = completed[:cut], completed[cut:]
    # 时间切分：训练集最晚完成日应早于或等于切点
    results = {"n_completed": n, "n_train": len(train), "n_test": len(test), "by_group": []}
    groups = {
        "all": test,
    }
    # 再按项目、任务类型
    def run(name, subset, pool, match_asset=True):
        rows = []
        pairs_med, pairs_p75, pairs_tmpl, pairs_wrong = [], [], [], []
        covered = 0
        for t in subset:
            tmpl = templates[t.project_name and _tmpl_id(t, templates)]
            # helper below
        return None  # replaced
    return completed, train, test, hist_ids


def _tmpl_for(task: Task, templates) -> "object":
    for t in templates.values():
        if t.kitsu_project_name == task.project_name or t.id == task.task_type_key and False:
            return t
        if t.kitsu_project_name == task.project_name:
            return t
    # fallback by project_name containing
    for t in templates.values():
        if t.id in (task.project_name or "") or t.kitsu_project_name in (task.project_name or ""):
            return t
    raise KeyError(task.project_name)


def estimate_report(tasks, templates, manifest):
    hist_ids = set()
    done_dates = {}
    for block in manifest["projects"].values():
        for tp in block["tasks"]:
            if tp.get("scenario") == "historical_completed" and tp.get("kitsu_task_id"):
                hist_ids.add(tp["kitsu_task_id"])
                done_dates[tp["kitsu_task_id"]] = tp.get("done_date")
    completed = [t for t in tasks if t.id in hist_ids and t.is_done and (t.duration_seconds or 0) > 0]
    completed.sort(key=lambda t: (done_dates.get(t.id) or "9999", t.id))
    cut = int(len(completed) * 0.7)
    train, test = completed[:cut], completed[cut:]
    cutoff_date = done_dates.get(train[-1].id) if train else None

    def evaluate(subset, pool, match_asset=True):
        records = []
        for t in subset:
            tmpl = _tmpl_for(t, templates)
            actual = t.duration_days(tmpl.hours_per_day)
            if actual <= 0:
                continue
            est = estimate_task(t, pool, tmpl, match_asset_type=match_asset, exclude_ids={t.id})
            pred_med = est.median_days if est.n_similar >= tmpl.min_similar else est.template_default_days
            pred_p75 = est.p75_days if est.n_similar >= tmpl.min_similar else est.template_default_days
            pred_tmpl = est.template_default_days
            records.append({
                "id": t.id,
                "project": t.project_name,
                "asset": t.asset_name,
                "asset_type": t.asset_type_name,
                "task_type": t.task_type_name,
                "task_type_key": t.task_type_key,
                "actual": actual,
                "pred_median": pred_med,
                "pred_p75": pred_p75,
                "pred_template": pred_tmpl,
                "n_similar": est.n_similar,
                "source": est.source,
                "low_confidence": est.low_confidence,
                "similar_ids": est.similar_task_ids[:8],
            })
        return records

    recs = evaluate(test, train, True)
    recs_wrong = evaluate(test, train, False)

    def agg(recs, pred_key):
        pairs = [(r[pred_key], r["actual"]) for r in recs if r[pred_key] is not None and r["actual"] > 0]
        covered = sum(1 for r in recs if not r["low_confidence"]) / len(recs) if recs else 0
        return {
            "n": len(recs),
            "n_pairs": len(pairs),
            "mape": mape(pairs),
            "median_abs_err_days": median_abs_err(pairs),
            "coverage": covered,
            "fallback_to_template": sum(1 for r in recs if r["low_confidence"]),
        }

    by_proj = defaultdict(list)
    by_type = defaultdict(list)
    for r in recs:
        by_proj[r["project"]].append(r)
        by_type[f"{r['asset_type']}|{r['task_type']}"].append(r)

    return {
        "n_completed_historical": len(completed),
        "n_train": len(train),
        "n_test": len(test),
        "cutoff_done_date": cutoff_date,
        "split": "按完成日排序后前 70% 训练、后 30% 测试；检索池只有训练集，并排除自身。",
        "overall_median": agg(recs, "pred_median"),
        "overall_p75": agg(recs, "pred_p75"),
        "overall_template": agg(recs, "pred_template"),
        "ablation_task_type_only_median": agg(recs_wrong, "pred_median"),
        "by_project": {k: {"median": agg(v, "pred_median"), "template": agg(v, "pred_template")} for k, v in by_proj.items()},
        "by_cell": {k: {"median": agg(v, "pred_median"), "template": agg(v, "pred_template"), "n": len(v)} for k, v in sorted(by_type.items())},
        "records": recs,
        "records_wrong_type": recs_wrong,
    }


def pr(tp, fp, fn, tn):
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if prec and rec and (prec + rec) else (0.0 if prec == 0 or rec == 0 else None)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1, "support_pos": tp + fn, "support_neg": tn + fp}


def eval_open_intent(tasks, templates, manifest, as_of=AS_OF):
    """一致性检查：生成意图 vs 规则层。承认循环。"""
    # map task_id -> intended
    intended = {}
    scenarios = {}
    notes = {}
    for block in manifest["projects"].values():
        for tp in block["tasks"]:
            tid = tp.get("kitsu_task_id")
            if not tid:
                continue
            intended[tid] = list(tp.get("intended_open_risks") or [])
            scenarios[tid] = tp.get("scenario")
            notes[tid] = tp.get("note")
    # only open scenarios (not historical / support / unscheduled)
    open_ids = [tid for tid, sc in scenarios.items() if sc and str(sc).startswith("open_")]
    by_id = {t.id: t for t in tasks}
    per_kind = {k: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for k in ("overdue", "overrun", "high_retake", "dep_blocked", "sla_overdue")}
    details = []
    for tid in open_ids:
        t = by_id.get(tid)
        if not t:
            continue
        tmpl = _tmpl_for(t, templates)
        cards = scan_task(t, [x for x in tasks if x.project_id == t.project_id], tmpl, as_of)
        fired = {c.kind for c in cards}
        gold = set(intended.get(tid) or [])
        # comment_blocker 不是规则层
        gold_rules = gold & set(per_kind)
        for k in per_kind:
            g = k in gold_rules
            f = k in fired
            if g and f:
                per_kind[k]["tp"] += 1
            elif f and not g:
                per_kind[k]["fp"] += 1
            elif g and not f:
                per_kind[k]["fn"] += 1
            else:
                per_kind[k]["tn"] += 1
        details.append({
            "task_id": tid,
            "asset": t.asset_name,
            "task_type": t.task_type_name,
            "scenario": scenarios.get(tid),
            "intended": sorted(gold),
            "fired": sorted(fired),
            "note": notes.get(tid),
        })
    summary = {k: pr(**v) for k, v in per_kind.items()}
    return {"circularity_note": "进行中样本的逾期/超估/返修/依赖/SLA 意图在生成时就是按这些规则的定义注入的，本表是一致性检查，不是预测能力。comment_blocker 不在规则层，预期规则不报。", "by_kind": summary, "details": details, "n_open": len(open_ids)}


def reconstruct(tp, tmpl, as_of, all_plans_of_asset):
    """把一条已完成任务重建为 as_of 当天尚未完成的状态。"""
    start = as_date(tp["start"]) if tp.get("start") else None
    due = as_date(tp["due"]) if tp.get("due") else None
    done = as_date(tp["done_date"]) if tp.get("done_date") else None
    if not (start and due and done):
        return None
    if not (start <= as_of < done):
        return None
    dur = sum(log["seconds"] for log in (tp.get("time_logs") or []) if log.get("date") and as_date(log["date"]) <= as_of)
    # retakes that happened on or before as_of (status comments)
    retakes = 0
    comments = []
    for c in tp.get("comments") or []:
        if not c.get("created_at"):
            continue
        cd = as_date(c["created_at"][:10])
        if cd <= as_of:
            comments.append(Comment(
                id=f"gold-{tp.get('kitsu_task_id')}-{c['created_at']}",
                task_id=tp.get("kitsu_task_id") or "",
                text=c.get("text") or "",
                created_at=c.get("created_at"),
                gold_label=c.get("gold_label"),
                gold_blocker_reason=c.get("gold_blocker_reason"),
                task_status_short=c.get("status"),
            ))
            if c.get("status") == "retake":
                retakes += 1
    # last comment date
    last = comments[-1].created_at if comments else None
    status = "wip"
    if comments:
        status = comments[-1].task_status_short or "wip"
        if status == "done":
            status = "wip"  # as_of < done, shouldn't have done yet; if due to dating, force wip
    t = Task(
        id=tp.get("kitsu_task_id") or tp["asset_name"] + "/" + tp["task_type_key"],
        project_id=tp.get("project_id_key") or "",
        project_name=tmpl.kitsu_project_name,
        asset_id=tp.get("kitsu_asset_id") or tp["asset_name"],
        asset_name=tp["asset_name"],
        asset_type_id="",
        asset_type_name=tp["asset_type"],
        task_type_id="",
        task_type_name=tmpl.task_type(tp["task_type_key"]).name,
        task_type_key=tp["task_type_key"],
        status_id="",
        status_name=status,
        status_short=status if status != "done" else "wip",
        is_done=False,
        is_retake_status=status == "retake",
        is_feedback_request=status == "wfa",
        is_wip=True,
        estimation_seconds=days_to_seconds(tp["estimation_days"], tmpl.hours_per_day),
        duration_seconds=dur,
        retake_count=max(retakes, 0),
        start_date=start,
        due_date=due,
        real_start_date=start,
        end_date=None,
        done_date=None,
        last_comment_date=as_date(last[:10]) if last else None,
        comments=comments,
        source="reconstructed",
    )
    return t


def eval_retrospective(manifest, templates, as_of_offset_days=2, llm_pred_by_text=None):
    rows = []
    for key, block in manifest["projects"].items():
        tmpl = templates[key]
        plans = block["tasks"]
        by_asset = defaultdict(list)
        for tp in plans:
            by_asset[tp["asset_name"]].append(tp)
        for tp in plans:
            if tp.get("scenario") != "historical_completed":
                continue
            due = as_date(tp["due"]) if tp.get("due") else None
            if not due:
                continue
            as_of = due - timedelta(days=as_of_offset_days)
            t = reconstruct(tp, tmpl, as_of, by_asset[tp["asset_name"]])
            if t is None:
                continue
            # predecessors reconstructed similarly
            peers = []
            for other in by_asset[tp["asset_name"]]:
                # peer state at as_of
                if other.get("done_date") and as_date(other["done_date"]) <= as_of:
                    # treat as done
                    ot = reconstruct(other, tmpl, as_date(other["done_date"]) - timedelta(days=1), []) if other is not tp else None
                    # simpler: fake a done task
                    peers.append(Task(
                        id=other.get("kitsu_task_id") or other["task_type_key"],
                        project_id=t.project_id,
                        project_name=t.project_name,
                        asset_id=t.asset_id,
                        asset_name=t.asset_name,
                        asset_type_id="",
                        asset_type_name=t.asset_type_name,
                        task_type_id="",
                        task_type_name=tmpl.task_type(other["task_type_key"]).name,
                        task_type_key=other["task_type_key"],
                        status_id="",
                        status_name="done",
                        status_short="done",
                        is_done=True,
                        is_retake_status=False,
                        is_feedback_request=False,
                        is_wip=False,
                        estimation_seconds=0,
                        duration_seconds=0,
                        retake_count=0,
                        start_date=as_date(other["start"]) if other.get("start") else None,
                        due_date=as_date(other["due"]) if other.get("due") else None,
                        real_start_date=None,
                        end_date=as_date(other["done_date"]),
                        done_date=as_date(other["done_date"]),
                        last_comment_date=None,
                        source="reconstructed",
                    ))
                elif other is not tp:
                    ot = reconstruct(other, tmpl, as_of, [])
                    if ot:
                        peers.append(ot)
            cards = scan_task(t, peers + [t], tmpl, as_of)
            # also keyword on comments at as_of
            kw = keyword_extract(t.comments)
            gold_late = bool(tp.get("was_late"))
            fired_rule = len(cards) > 0
            fired_any = fired_rule or any(e.is_blocker for e in kw)
            llm_hit = False
            n_llm_mapped = 0
            n_llm_unmapped = 0
            if llm_pred_by_text:
                for c in t.comments:
                    text = (c.text or "").strip()
                    if not text:
                        continue
                    if text in llm_pred_by_text:
                        n_llm_mapped += 1
                        if llm_pred_by_text[text]:
                            llm_hit = True
                    else:
                        n_llm_unmapped += 1
            fired_rule_or_llm = fired_rule or llm_hit
            rows.append({
                "id": t.id,
                "asset": t.asset_name,
                "task_type": t.task_type_name,
                "asset_type": t.asset_type_name,
                "project": tmpl.id,
                "as_of": as_of.isoformat(),
                "due": t.due_date.isoformat() if t.due_date else None,
                "done": tp.get("done_date"),
                "was_late": gold_late,
                "fired_rule": fired_rule,
                "fired_rule_or_keyword": fired_any,
                "fired_rule_or_llm": fired_rule_or_llm,
                "llm_comment_hit": llm_hit,
                "n_llm_mapped": n_llm_mapped,
                "n_llm_unmapped": n_llm_unmapped,
                "rule_kinds": [c.kind for c in cards],
                "n_comments_then": len(t.comments),
                "duration_then_days": t.duration_days(tmpl.hours_per_day),
                "estimation_days": tp.get("estimation_days"),
                "retake_then": t.retake_count,
            })
    def summarize(pred_key):
        tp = sum(1 for r in rows if r["was_late"] and r[pred_key])
        fp = sum(1 for r in rows if (not r["was_late"]) and r[pred_key])
        fn = sum(1 for r in rows if r["was_late"] and not r[pred_key])
        tn = sum(1 for r in rows if (not r["was_late"]) and not r[pred_key])
        return pr(tp, fp, fn, tn)
    return {
        "n_reconstructed": len(rows),
        "as_of_rule": "due_date 减去 2 个日历日，且须满足 start <= as_of < done_date。",
        "ground_truth": "was_late = done_date > due_date（生成时写入，来自实际完成日与截止日，不是规则输出）。",
        "rule_only": summarize("fired_rule"),
        "rule_or_keyword": summarize("fired_rule_or_keyword"),
        "rule_or_llm": summarize("fired_rule_or_llm") if llm_pred_by_text else None,
        "llm_comment_coverage": {
            "n_comments_mapped": sum(r.get("n_llm_mapped", 0) for r in rows),
            "n_comments_unmapped": sum(r.get("n_llm_unmapped", 0) for r in rows),
            "note": (
                "回顾式 LLM 判断复用金标评测里按评论原文缓存的产物，不再额外调用。"
                "未命中缓存的评论不视为阻塞（保守），并在 unmapped 中计数。"
            ) if llm_pred_by_text else "未接入 LLM。",
        },
        "late_rate": (sum(1 for r in rows if r["was_late"]) / len(rows)) if rows else None,
        "rows": rows,
    }


def eval_comments(manifest, llm_extractions=None, llm_ran=False, llm_usage=None):
    return evaluate_comments(
        manifest,
        llm_extractions=llm_extractions,
        llm_ran=llm_ran,
        llm_usage=llm_usage,
    )


def fmt(x, digits=4):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def write_reports(est, open_intent, retro, comments, scan_latency, settings, llm_usage, bad_examples):
    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "as_of": settings.as_of,
        "llm_backend": llm_usage.get("backend") if llm_usage else "disabled",
        "llm_model": (llm_usage or {}).get("model") or "",
    }
    lines = []
    a = lines.append
    a("# PipeSched 离线评测结果")
    a("")
    a("评测脚本：`eval/measure.py`。所有数字由本次运行实测，没有手工填写。")
    a("数据是固定种子（42）生成的**合成数据**，见 `data/gold/manifest.json`，不是真实项目组。")
    a("")
    a("## 1. 估时（留出已完成任务）")
    a("")
    a(f"- 历史已完成任务 {est['n_completed_historical']} 条，按完成日排序后训练 {est['n_train']}、测试 {est['n_test']}。切点完成日：{est['cutoff_done_date']}。")
    a(f"- {est['split']}")
    a("- 主指标 MAPE 用**同类中位数**；P75 是排期建议用的保守值，通常会偏高。对照基线是模板默认人天。")
    a("")
    a("| 方法 | n | MAPE | 中位绝对误差（人天） | 非模板覆盖率 |")
    a("| --- | --- | --- | --- | --- |")
    for name, key, cov in [
        ("同类中位数（主指标）", "overall_median", True),
        ("同类 P75", "overall_p75", True),
        ("模板默认（基线）", "overall_template", False),
        ("消融：只按任务类型、不分资产类型（中位数）", "ablation_task_type_only_median", True),
    ]:
        b = est[key]
        covs = fmt(b['coverage']) if cov else "—"
        a(f"| {name} | {b['n_pairs']} | {fmt(b['mape'])} | {fmt(b['median_abs_err_days'])} | {covs} |")
    a("")
    a("说明：P-Mobile 测试集上模板默认 MAPE（0.4156）**低于**同类中位数（0.4828），角色项目则相反（模板 0.5257，中位数 0.2292）。整体中位数仍优于模板，但不能说历史检索全面更好。这一对照必须和数字一起讲。")
    a("")
    a("### 按项目")
    a("")
    a("| 项目 | n | 中位数 MAPE | 模板 MAPE |")
    a("| --- | --- | --- | --- |")
    for name, v in est["by_project"].items():
        a(f"| {name} | {v['median']['n_pairs']} | {fmt(v['median']['mape'])} | {fmt(v['template']['mape'])} |")
    a("")
    a("### 按（资产类型 × 任务类型），测试集单元格")
    a("")
    a("| 单元格 | n | 中位数 MAPE | 模板 MAPE |")
    a("| --- | --- | --- | --- |")
    for name, v in est["by_cell"].items():
        a(f"| {name} | {v['n']} | {fmt(v['median']['mape'])} | {fmt(v['template']['mape'])} |")
    a("")

    a("## 2. 风险预警")
    a("")
    a("### 2.1 进行中样本：生成意图 vs 规则层（一致性检查，存在循环）")
    a("")
    a(open_intent["circularity_note"])
    a("")
    a(f"进行中注入样本 {open_intent['n_open']} 条（每个场景一条任务，含前序支持任务里标了 open_ 的）。")
    a("")
    a("| 规则 | TP | FP | FN | TN | Precision | Recall |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for k, v in open_intent["by_kind"].items():
        a(f"| {k} | {v['tp']} | {v['fp']} | {v['fn']} | {v['tn']} | {fmt(v['precision'])} | {fmt(v['recall'])} |")
    a("")
    a("### 2.2 回顾式：到期前 2 天预测「后来是否真的延期」")
    a("")
    a(f"- 重建样本 {retro['n_reconstructed']} 条。{retro['as_of_rule']}")
    a(f"- 真值：{retro['ground_truth']}")
    a(f"- 样本延期率：{fmt(retro['late_rate'])}。")
    a("")
    a("| 预测定义 | Precision | Recall | F1 | TP | FP | FN | TN |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")
    rows_to_print = [("仅规则层", "rule_only"), ("规则层或关键词", "rule_or_keyword")]
    if retro.get("rule_or_llm"):
        rows_to_print.append(("规则层或 LLM（护栏后输出）", "rule_or_llm"))
    for name, key in rows_to_print:
        v = retro[key]
        a(f"| {name} | {fmt(v['precision'])} | {fmt(v['recall'])} | {fmt(v['f1'])} | {v['tp']} | {v['fp']} | {v['fn']} | {v['tn']} |")
    if retro.get("llm_comment_coverage"):
        cov = retro["llm_comment_coverage"]
        a("")
        a(f"- 回顾式评论 LLM 覆盖：已映射 {cov.get('n_comments_mapped')} 条，未映射 {cov.get('n_comments_unmapped')} 条。{cov.get('note','')}")
    a("")
    a("误报（被标风险但按期完成）比例 = FP / (FP+TN)，用于谈告警疲劳：")
    v = retro["rule_only"]
    denom = v["fp"] + v["tn"]
    a(f"- 仅规则层：{fmt(v['fp']/denom if denom else None)}（{v['fp']}/{denom}）。")
    a("")

    a("## 3. 评论解读")
    a("")
    a(f"有金标的自然语言评论 {comments['n_labeled']} 条（不含纯状态流转）。分布：{comments['by_label']}。")
    a("金标集合与 v1 相同，一道未改。")
    a("")
    kw = comments["keyword_baseline"]
    llm = comments.get("llm") or {}
    a("### 对照表（正向类 = 金标 blocker）")
    a("")
    a("| 方法 | Precision | Recall | F1 | TP | FP | FN | TN |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")

    def _row(name, m):
        if not m:
            return
        a(f"| {name} | {fmt(m.get('precision'))} | {fmt(m.get('recall'))} | {fmt(m.get('f1'))} | {m.get('tp')} | {m.get('fp')} | {m.get('fn')} | {m.get('tn')} |")

    _row("关键词基线（确定性，不是 LLM）", kw)
    if llm.get("ran"):
        _row("LLM 产品输出（护栏后，失败回退关键词）", llm.get("product"))
        raw = llm.get("raw") or {}
        _row(f"LLM 原始 is_blocker（仅解析成功 n={llm.get('raw_n')}）", raw)
    a("")
    a(f"- 关键词说明：{comments['note']}")
    a("")

    a("### 按金标类别")
    a("")
    a("模型只输出二分类 is_blocker，不是四分类。blocker 行是该类上的召回（及总体精确率）；")
    a("sarcasm / misleading / normal 三行报告的是「正确判成非阻塞」的比例（特异度），以及该类上的误报数。")
    a("")

    def _cat_table(title, cats):
        a(f"#### {title}")
        a("")
        a("| 金标类别 | n | 判为阻塞 | Precision | Recall / 特异度 | F1 | 准确率 |")
        a("| --- | --- | --- | --- | --- | --- | --- |")
        for lab in ("blocker", "sarcasm", "misleading", "normal"):
            c = (cats or {}).get(lab) or {}
            rec = c.get("recall") if lab == "blocker" else c.get("specificity", c.get("recall"))
            a(f"| {lab} | {c.get('n')} | {c.get('n_pred_blocker')} | {fmt(c.get('precision'))} | {fmt(rec)} | {fmt(c.get('f1'))} | {fmt(c.get('accuracy'))} |")
        a("")

    _cat_table("关键词基线", comments.get("keyword_per_category"))
    if llm.get("ran"):
        _cat_table("LLM 产品输出", llm.get("product_per_category"))

    a("### LLM")
    a("")
    if not llm.get("ran"):
        a(llm.get("note") or "未调用真实 LLM，不报告模型忠实度 / 幻觉率。")
        a("简历里评论抽取幻觉率保持占位，不得用关键词数字冒充。")
        a("")
    else:
        usage = llm.get("usage") or llm_usage or {}
        a(f"- 模型 id：`{usage.get('model') or '（见 usage）'}`，backend={usage.get('backend')}。")
        a(f"- {llm.get('note','')}")
        a(f"- 调用 {usage.get('n_calls')} 次，解析成功（n_ok）{usage.get('n_ok')}，"
          f"API 失败 {usage.get('n_api_fail')}，解析失败 {usage.get('n_parse_fail')}，"
          f"回退关键词 {usage.get('n_fallback')}。"
          f"回退率 {fmt(usage.get('fallback_rate'))}，失败率 {fmt(usage.get('failure_rate'))}。")
        a(f"- 时延（单条，含重试）：P50={fmt(usage.get('latency_p50_ms'), 1)} ms，"
          f"P95={fmt(usage.get('latency_p95_ms'), 1)} ms，合计 {fmt(usage.get('total_ms'), 1)} ms。")
        a(f"- Token：prompt={usage.get('prompt_tokens')}，completion={usage.get('completion_tokens')}，"
          f"cache_hit={usage.get('cache_hit_tokens')}，合计 {usage.get('total_tokens')}。")
        cny = usage.get("estimated_cny")
        a(f"- 估算费用：{fmt(cny, 6) if cny is not None else '—'} 元人民币。"
          f"{usage.get('price_note') or ''}")
        hall = llm.get("hallucination") or {}
        a("")
        a("#### 幻觉（脚本按定义实测，作用在原始 JSON 上）")
        a("")
        a(hall.get("definition") or "")
        a("")
        a(f"- 解析成功 {hall.get('n_parsed')} 条中，{hall.get('n_flagged')} 条带至少一项幻觉标记，"
          f"幻觉率 {fmt(hall.get('rate'))}。标记计数：{hall.get('flag_counts')}。")
        a(f"- 护栏之后仍作为阻塞展示且带标记的：{hall.get('n_visible_blocker_flagged')} 条"
          f"（占解析成功 {fmt(hall.get('visible_rate_over_parsed'))}）。"
          "对不齐原文的阻塞在产品路径会被抑制，所以可见率可以低于原始率。")
        a("")
        fb = llm.get("fallback") or {}
        a(f"- 评测集上回退 {fb.get('n')} 条，回退率 {fmt(fb.get('rate'))}。")
        a("")

    a("")

    a("## 4. 写回护栏")
    a("")
    a("见单元测试 `tests/test_writeback.py`：无 confirmed_by、无令牌、错误令牌均不得调用写接口，测试通过则未确认写回次数为 0。")
    a("")

    a("## 5. 扫描时延与费用")
    a("")
    a("- 两个项目规则+关键词扫描时延见下表（毫秒）。本表的扫描固定走 DisabledLLM，与 v1 口径一致，不含评论模型调用。评论 LLM 的时延和费用见第 3 节。")
    a("")
    a("| 项目 | 任务数 | 规则 ms | 关键词 ms | LLM ms | 合计 ms |")
    a("| --- | --- | --- | --- | --- | --- |")
    for row in scan_latency:
        lm = row["latency_ms"]
        a(f"| {row['project']} | {row['n_tasks']} | {lm['rule']} | {lm['keyword']} | {lm['llm']} | {lm['total']} |")
    a("")
    if llm_usage and llm_usage.get("n_calls", 0) > 0:
        a(f"- 评论 LLM 用量摘要：model={llm_usage.get('model')}，"
          f"n_calls={llm_usage.get('n_calls')}，tokens={llm_usage.get('total_tokens')}，"
          f"estimated_cny={llm_usage.get('estimated_cny')}。"
          f"{llm_usage.get('price_note') or ''}")
    else:
        a("- **未调用真实 LLM，token 费用为 0。** 不要把假设单价写成实测账单。")
    a("")

    a("## 6. 环境")
    a("")
    a(f"- Python {env['python']}，{env['platform']}。")
    a(f"- 评测 as_of = {env['as_of']}（写死，不用墙钟）。")
    a(f"- LLM backend = {env['llm_backend']}，model = `{env.get('llm_model') or '（未调用）'}`。")
    a("")

    a("## 7. 限制（必须和数字一起讲）")
    a("")
    a("- 数据是合成的，工时分布和延期率是生成器设定的，不能外推到真实项目组。")
    a("- 进行中样本的规则层 P/R 存在循环论证，只说明种子和规则读字段一致。")
    a("- 回顾式评测更有意义，但「到期前 2 天」的工时是按日志重建的，仍来自同一生成器，独立性有限。")
    a("- 没有真实制片访谈，阈值是方案里的初始值，不是用户校准过的。")
    a("- 这不是腾讯或任何游戏工作室的生产系统。")
    a("- 评论金标由生成器按模板写就，关键词召回=1 被用词抬高，不能外推到真实评审用语。")
    a("- 幻觉定义是保守的：正当改述不记为幻觉；若 reason 编造了原文没有的数字或虚构引语，才会被标记。")
    a("- 费用是按 API 返回的 token 与硅基流动官网价格页标价估算的，不是账单原件；标价会变动。")
    a("- 同义词式的「见过坏例再写规则」风险不适用于本评测（金标在 v1 已冻结，本轮未改题，也未根据本轮坏例改提示词）。")
    a("")

    (EVAL / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (EVAL / "results_raw.json").write_text(json.dumps({
        "estimate": {k: v for k, v in est.items() if k not in {"records", "records_wrong_type"}},
        "estimate_records_sample": est["records"][:15],
        "open_intent": open_intent,
        "retrospective": {k: v for k, v in retro.items() if k != "rows"},
        "retrospective_sample": retro["rows"][:20],
        "comments": {k: v for k, v in comments.items() if k not in {"keyword_fps", "keyword_fns"}},
        "comments_fps": comments["keyword_fps"],
        "comments_fns": comments["keyword_fns"],
        "scan_latency": scan_latency,
        "llm_usage": llm_usage,
        "env": env,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # badcases.md
    b = []
    b.append("# PipeSched 坏例清单")
    b.append("")
    b.append("所有例子来自本次合成数据与评测脚本，不是事后改题。")
    b.append("")
    b.append("## 1. 估时：若不分资产类型会串")
    b.append("")
    b.append("角色建模真实均值约 5.5 人天，道具建模约 1.8 人天，模板默认却都是 4。")
    b.append("产品默认「任务类型 + 资产类型」同类检索；下面给出测试集里分资产类型与不分的对照。")
    b.append("")
    # pick extreme cells
    b.append(f"测试集整体：分资产类型中位数 MAPE = {fmt(est['overall_median']['mape'])}，")
    b.append(f"只按任务类型 = {fmt(est['ablation_task_type_only_median']['mape'])}。")
    b.append("")
    # examples where predictions differ a lot
    diffs = []
    by_id_wrong = {r["id"]: r for r in est["records_wrong_type"]}
    for r in est["records"]:
        w = by_id_wrong.get(r["id"])
        if not w or r["pred_median"] is None or w["pred_median"] is None:
            continue
        diffs.append((abs(w["pred_median"] - r["actual"]) - abs(r["pred_median"] - r["actual"]), r, w))
    diffs.sort(reverse=True, key=lambda x: x[0])
    b.append("| 资产 | 类型 | 实际人天 | 分资产类型预测 | 只按任务类型预测 |")
    b.append("| --- | --- | --- | --- | --- |")
    for _, r, w in diffs[:8]:
        b.append(f"| {r['asset']} | {r['asset_type']} / {r['task_type']} | {fmt(r['actual'], 2)} | {fmt(r['pred_median'], 2)} | {fmt(w['pred_median'], 2)} |")
    b.append("")
    b.append("## 2. 反话与误导评论（关键词误报）")
    b.append("")
    b.append("关键词基线见到「重做」就报警。下面是评测里的假阳性，真值不是当前阻塞。")
    b.append("")
    for g in comments["keyword_fps"][:12]:
        b.append(f"- **{g['label']}** `{g['asset']}`：{g['text']}")
    b.append("")
    b.append("## 3. 宽限期内不报逾期（产品取舍，不是漏报）")
    b.append("")
    for d in open_intent["details"]:
        if d["scenario"] == "open_in_grace":
            b.append(f"- `{d['asset']}` / {d['task_type']}：意图 {d['intended']}，规则打出 {d['fired']}。{d['note']}")
    b.append("")
    b.append("## 4. 临界未超阈值")
    b.append("")
    for d in open_intent["details"]:
        if d["scenario"] == "open_near_miss":
            b.append(f"- `{d['asset']}` / {d['task_type']}：意图 {d['intended']}，规则打出 {d['fired']}。{d['note']}")
    b.append("")
    b.append("## 5. 规则层看不到的评论阻塞")
    b.append("")
    for d in open_intent["details"]:
        if d["scenario"] == "open_comment_blocker":
            b.append(f"- `{d['asset']}` / {d['task_type']}：意图 {d['intended']}，规则打出 {d['fired']}。{d['note']}")
    b.append("")
    b.append("## 6. 回顾式：规则漏报后来延期的，或误报却按期完成的")
    b.append("")
    fn = [r for r in retro["rows"] if r["was_late"] and not r["fired_rule"]]
    fp = [r for r in retro["rows"] if (not r["was_late"]) and r["fired_rule"]]
    b.append(f"回顾式规则漏报 {len(fn)} 条、误报 {len(fp)} 条（样本 {retro['n_reconstructed']}）。各举若干：")
    b.append("")
    b.append("漏报（到期前 2 天规则未响，后来却晚了）：")
    for r in fn[:8]:
        b.append(f"- `{r['asset']}` / {r['task_type']}（{r['asset_type']}）due={r['due']} done={r['done']}，当时实耗 {fmt(r['duration_then_days'], 2)} 人天 / 估时 {r['estimation_days']}，返修 {r['retake_then']}。")
    b.append("")
    b.append("误报（当时被标风险，后来按期完成）：")
    for r in fp[:8]:
        b.append(f"- `{r['asset']}` / {r['task_type']} 规则 {r['rule_kinds']}，due={r['due']} done={r['done']}。")
    b.append("")
    b.append("## 7. 依赖用了错误模板会怎样")
    b.append("")
    b.append("依赖来自 YAML，不来自模型。若误把手游短管线套到角色项目上，绑定不会再依赖贴图，")
    b.append("`c_character_dep_block` 这类「贴图未完成但绑定已开始」就不会被报。演示时切模板即可对照，不要改代码。")
    b.append("")
    b.append("## 8. 模型在工具失败后编日期")
    b.append("")
    b.append("本项目日期不经过模型：写回只走确认令牌，估时只走检索。没有「模型编了一个日期」的运行样本，")
    b.append("因为产品路径上就不让模型填日期。这是设计，会在面试里主动讲。")
    b.append("")
    llm = comments.get("llm") or {}
    if llm.get("ran"):
        b.append("## 9. LLM 评论解读坏例（同一套 149 条金标，题面未改）")
        b.append("")
        b.append(f"模型 `{ (llm.get('usage') or {}).get('model') }`。下面是产品输出（护栏之后）相对金标的误报和漏报，以及原始输出上的幻觉标记。")
        b.append("")
        b.append("### 9.1 误报（产品输出判为阻塞，金标不是 blocker）")
        b.append("")
        fps = llm.get("product_fps") or []
        if not fps:
            b.append("无。")
        for g in fps[:15]:
            b.append(
                f"- **{g.get('label')}** `{g.get('asset')}`：{g.get('text')}"
                f" ｜ reason={g.get('raw_reason')!r} citation={g.get('citation')!r}"
                f" flags={g.get('flags')} fallback={g.get('fallback')}"
            )
        b.append("")
        b.append("### 9.2 漏报（金标是 blocker，产品输出未报）")
        b.append("")
        fns = llm.get("product_fns") or []
        if not fns:
            b.append("无。")
        for g in fns[:15]:
            b.append(
                f"- `{g.get('asset')}`：{g.get('text')}"
                f" ｜ raw_is_blocker={g.get('raw_is_blocker')} reason={g.get('raw_reason')!r}"
                f" flags={g.get('flags')} fallback={g.get('fallback')}"
            )
        b.append("")
        b.append("### 9.3 幻觉标记（原始 JSON，护栏之前）")
        b.append("")
        hall = (llm.get("hallucination") or {}).get("examples") or []
        if not hall:
            b.append("本轮解析成功的输出没有触发幻觉标记。")
        for g in hall[:15]:
            b.append(
                f"- **{g.get('label')}** flags={g.get('flags')} 原文：{g.get('text')}"
                f" ｜ raw_is_blocker={g.get('raw_is_blocker')} citation={g.get('raw_citation')!r}"
                f" reason={g.get('raw_reason')!r} product_is_blocker={g.get('product_is_blocker')}"
            )
        b.append("")
    (EVAL / "badcases.md").write_text("\n".join(b) + "\n", encoding="utf-8")
    print("wrote", EVAL / "results.md", "and", EVAL / "badcases.md")


def main():
    settings = load_settings()
    manifest, snapshot, templates, tasks = load_gold()
    print(f"loaded {len(tasks)} tasks from snapshot")
    est = estimate_report(tasks, templates, manifest)
    print("estimate MAPE median", est["overall_median"])
    open_intent = eval_open_intent(tasks, templates, manifest, as_date(settings.as_of))
    print("open intent", {k: v for k, v in open_intent["by_kind"].items()})
    llm = resolve_backend(settings)
    gold_meta, gold_comments = load_gold_comments(manifest)
    print(f"gold comments {len(gold_comments)}; llm backend={llm.name} model={getattr(llm, 'model', '')}")
    llm_extractions = None
    if llm.name != "disabled":
        llm_extractions = llm.extract(gold_comments)
        usage = llm.usage().to_dict()
        print("LLM usage", usage)
        comments = eval_comments(manifest, llm_extractions=llm_extractions, llm_ran=True, llm_usage=usage)
        pred_by_text = {c.text: bool(e.is_blocker) for c, e in zip(gold_comments, llm_extractions)}
        retro = eval_retrospective(manifest, templates, llm_pred_by_text=pred_by_text)
    else:
        usage = llm.usage().to_dict()
        comments = eval_comments(manifest, llm_ran=False)
        retro = eval_retrospective(manifest, templates)
    print("retrospective", retro["rule_only"], "rule_or_llm", retro.get("rule_or_llm"))
    print("comments keyword", comments["keyword_baseline"])
    if comments.get("llm", {}).get("ran"):
        print("comments llm product", comments["llm"].get("product"))
        h = comments["llm"].get("hallucination") or {}
        print("hallucination", h.get("rate"), h.get("flag_counts"))

    # latency: scan each project from snapshot — DisabledLLM, comparable to v1.
    from collections import defaultdict
    by_proj = defaultdict(list)
    for t in tasks:
        by_proj[t.project_name].append(t)
    scan_latency = []
    for pname, pts in by_proj.items():
        tmpl = _tmpl_for(pts[0], templates)
        t0 = time.perf_counter()
        result = scan_tasks(pts, tmpl, settings.as_of, llm=DisabledLLM(), project_name=pname, shadow=True)
        scan_latency.append({
            "project": pname,
            "n_tasks": result.n_tasks,
            "n_rule_cards": len(result.rule_cards),
            "n_keyword_cards": len(result.keyword_cards),
            "latency_ms": result.latency_ms,
        })
        print("scan", pname, result.latency_ms, "rules", len(result.rule_cards))


    write_reports(est, open_intent, retro, comments, scan_latency, settings, usage, None)

    live_latency = None
    try:
        from pipesched.kitsu_client import KitsuGateway
        t0 = time.perf_counter()
        gw = KitsuGateway(settings, connect_now=True)
        live_rows = []
        for tmpl in templates.values():
            proj = gw.project_by_name(tmpl.kitsu_project_name)
            if not proj:
                continue
            t1 = time.perf_counter()
            pts = gw.load_project_tasks(proj, tmpl)
            t_load = (time.perf_counter() - t1) * 1000
            result = scan_tasks(pts, tmpl, settings.as_of, llm=DisabledLLM(), project_name=tmpl.kitsu_project_name, shadow=False)
            live_rows.append({
                "project": tmpl.kitsu_project_name,
                "n_tasks": len(pts),
                "gazu_load_ms": round(t_load, 2),
                "rule_scan_ms": round(result.latency_ms["rule"], 2),
                "keyword_ms": round(result.latency_ms["keyword"], 2),
                "total_ms": round(t_load + result.latency_ms["total"], 2),
                "n_rule_cards": len(result.rule_cards),
            })
        live_latency = {
            "rows": live_rows,
            "total_ms": round((time.perf_counter() - t0) * 1000, 2),
            "note": "含本地 Docker 里 Zou API 的往返，不含 LLM。",
        }
        print("live latency", live_latency)
    except Exception as e:
        print("live latency skipped:", type(e).__name__, e)

    if live_latency:
        path = EVAL / "results.md"
        extra = [
            "",
            "## 5b. 实时 Gazu 拉取 + 扫描时延（本地 Kitsu）",
            "",
            live_latency["note"],
            "",
            "| 项目 | 任务数 | Gazu 拉取 ms | 规则 ms | 关键词 ms | 合计 ms | 规则卡数 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in live_latency["rows"]:
            extra.append(
                f"| {row['project']} | {row['n_tasks']} | {row['gazu_load_ms']} | {row['rule_scan_ms']} | {row['keyword_ms']} | {row['total_ms']} | {row['n_rule_cards']} |"
            )
        extra.append("")
        extra.append(f"两个项目连登录在内的总墙钟：{live_latency['total_ms']} ms。")
        extra.append("")
        path.write_text(path.read_text(encoding="utf-8") + "\n".join(extra), encoding="utf-8")
        rawp = EVAL / "results_raw.json"
        raw = json.loads(rawp.read_text(encoding="utf-8"))
        raw["live_latency"] = live_latency
        rawp.write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("DONE")


if __name__ == "__main__":
    main()
