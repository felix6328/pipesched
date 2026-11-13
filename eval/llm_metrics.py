"""Comment-extraction metrics for keyword vs LLM, including hallucination.

Used by eval/measure.py. Gold construction must stay identical to v1:
every comment whose gold_label is one of blocker/sarcasm/misleading/normal.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from pipesched.comments import keyword_extract
from pipesched.hallucination import hallucination_flags
from pipesched.models import BlockerExtraction, Comment


LABELS = ("blocker", "sarcasm", "misleading", "normal")


def pr(tp, fp, fn, tn) -> dict:
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    if prec is not None and rec is not None and (prec + rec):
        f1 = 2 * prec * rec / (prec + rec)
    else:
        f1 = None
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": prec, "recall": rec, "f1": f1,
        "n": tp + fp + fn + tn,
    }


def percentile(xs, p: float) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(ys) - 1)
    return ys[f] + (ys[c] - ys[f]) * (k - f)


def load_gold_comments(manifest) -> tuple[list[dict], list[Comment]]:
    """Same 149-comment gold set as v1. Do not filter or rewrite labels."""
    gold = []
    for block in manifest["projects"].values():
        for tp in block["tasks"]:
            for c in tp.get("comments") or []:
                if c.get("gold_label") in set(LABELS):
                    gold.append({
                        "task_id": tp.get("kitsu_task_id"),
                        "asset": tp["asset_name"],
                        "text": c["text"],
                        "label": c["gold_label"],
                        "reason": c.get("gold_blocker_reason"),
                        "scenario": tp.get("scenario"),
                    })
    comments = [
        Comment(
            id=str(i),
            task_id=g["task_id"] or "",
            text=g["text"],
            gold_label=g["label"],
            gold_blocker_reason=g["reason"],
        )
        for i, g in enumerate(gold)
    ]
    return gold, comments


def _confusion(gold: list[dict], pred_blocker: list[bool]) -> dict:
    tp = fp = fn = tn = 0
    fps, fns, tps, tns = [], [], [], []
    for i, g in enumerate(gold):
        pred = bool(pred_blocker[i])
        truth = g["label"] == "blocker"
        row = {**g, "index": i, "pred": pred}
        if truth and pred:
            tp += 1
            tps.append(row)
        elif pred and not truth:
            fp += 1
            fps.append(row)
        elif truth and not pred:
            fn += 1
            fns.append(row)
        else:
            tn += 1
            tns.append(row)
    out = pr(tp, fp, fn, tn)
    out["fps"] = fps
    out["fns"] = fns
    out["tps"] = tps
    out["tns"] = tns
    return out


def per_category(gold: list[dict], pred_blocker: list[bool]) -> dict:
    """Per gold-label slice.

    The model only outputs a binary is_blocker, so this is not a 4-way
    classifier F1. For gold=blocker: precision/recall/F1 equal the overall
    positive-class metrics computed on the full set for recall (TP / n_blocker),
    and precision is overall precision (shared). We also report, on each slice:
    n, n_pred_blocker, accuracy (recall on blocker, specificity on the three
    negative classes), and a one-vs-rest binary F1 where the 'positive' for a
    negative class is 'correctly predicted as not-blocker'.
    """
    by = {}
    for lab in LABELS:
        idx = [i for i, g in enumerate(gold) if g["label"] == lab]
        n = len(idx)
        n_pred = sum(1 for i in idx if pred_blocker[i])
        if lab == "blocker":
            tp = n_pred
            fn = n - n_pred
            fp = sum(1 for i, g in enumerate(gold) if g["label"] != "blocker" and pred_blocker[i])
            tn = sum(1 for i, g in enumerate(gold) if g["label"] != "blocker" and not pred_blocker[i])
            metrics = pr(tp, fp, fn, tn)
            metrics["accuracy"] = (tp / n) if n else None  # recall
        else:
            # positive event = correctly rejected (pred not blocker)
            tn = n - n_pred  # correct rejects
            fp = n_pred
            # one-vs-rest F1 treating "is this category" is not identifiable
            # from a binary model. Report specificity and slice accuracy.
            metrics = pr(tp=tn, fp=0, fn=fp, tn=0)
            # The pr() above is a bit forced. Override with clear fields:
            metrics = {
                "tp": tn,  # correctly predicted non-blocker
                "fp": fp,  # false alarms on this slice
                "fn": 0,
                "tn": 0,
                "precision": (tn / n) if n else None,  # equals specificity / accuracy here
                "recall": (tn / n) if n else None,
                "f1": (tn / n) if n else None,  # on a pure-negative slice, F1 of "not-blocker" = accuracy
                "n": n,
                "accuracy": (tn / n) if n else None,
                "specificity": (tn / n) if n else None,
                "false_positive_rate": (fp / n) if n else None,
            }
        metrics["n"] = n
        metrics["n_pred_blocker"] = n_pred
        if lab != "blocker":
            metrics.setdefault("specificity", (n - n_pred) / n if n else None)
            metrics.setdefault("false_positive_rate", n_pred / n if n else None)
            metrics["accuracy"] = (n - n_pred) / n if n else None
        else:
            metrics["accuracy"] = metrics["recall"]
        by[lab] = metrics
    return by


def evaluate_comments(
    manifest,
    llm_extractions: Optional[list[BlockerExtraction]] = None,
    llm_ran: bool = False,
    llm_usage: Optional[dict] = None,
) -> dict:
    gold, comments = load_gold_comments(manifest)
    kw = keyword_extract(comments)
    kw_ids = {int(e.comment_id) for e in kw if e.is_blocker}
    kw_pred = [i in kw_ids for i in range(len(gold))]
    kw_conf = _confusion(gold, kw_pred)
    kw_cat = per_category(gold, kw_pred)

    out = {
        "n_labeled": len(gold),
        "by_label": {lab: sum(1 for g in gold if g["label"] == lab) for lab in LABELS},
        "keyword_baseline": {k: v for k, v in kw_conf.items() if k not in {"fps", "fns", "tps", "tns"}},
        "keyword_per_category": kw_cat,
        "keyword_fps": kw_conf["fps"][:20],
        "keyword_fns": kw_conf["fns"][:20],
        "note": "关键词基线，不是 LLM。反话和「上次重做」是预期误报来源。召回=1 被生成用词抬高。",
    }

    if not (llm_ran and llm_extractions is not None):
        out["llm"] = {"ran": False, "note": "未调用真实 LLM，不报告模型忠实度 / 幻觉率。"}
        return out

    by_id = {str(e.comment_id): e for e in llm_extractions}
    missing = [i for i in range(len(gold)) if str(i) not in by_id]
    product_pred = []
    raw_pred = []
    raw_mask = []  # True where we have a parsed raw model output
    hallu_rows = []
    fallback_rows = []
    for i, g in enumerate(gold):
        e = by_id.get(str(i))
        if e is None:
            product_pred.append(False)
            raw_pred.append(False)
            raw_mask.append(False)
            continue
        product_pred.append(bool(e.is_blocker))
        if e.raw_parsed_ok and e.raw_is_blocker is not None:
            raw_pred.append(bool(e.raw_is_blocker))
            raw_mask.append(True)
        else:
            raw_pred.append(bool(e.is_blocker))
            raw_mask.append(False)
        if e.fallback:
            fallback_rows.append({"index": i, "label": g["label"], "text": g["text"], "failure": e.failure})
        if e.hallucination_flags:
            hallu_rows.append({
                "index": i,
                "label": g["label"],
                "text": g["text"],
                "flags": list(e.hallucination_flags),
                "raw_is_blocker": e.raw_is_blocker,
                "raw_citation": e.raw_citation,
                "raw_reason": e.raw_reason,
                "product_is_blocker": e.is_blocker,
            })

    prod = _confusion(gold, product_pred)
    raw_gold = [g for i, g in enumerate(gold) if raw_mask[i]]
    raw_preds_only = [raw_pred[i] for i in range(len(gold)) if raw_mask[i]]
    raw_conf = _confusion(raw_gold, raw_preds_only) if raw_gold else pr(0, 0, 0, 0)

    n_parsed = sum(raw_mask)
    n_hallu = len(hallu_rows)
    # hallucination rate over successfully parsed raw outputs
    n_hallu_parsed = sum(
        1 for i, g in enumerate(gold)
        if raw_mask[i] and by_id[str(i)].hallucination_flags
    )
    # product-visible: predicted blocker that still carries a hallu flag
    # (should be rare: guardrail drops ungrounded blockers; invented numbers could remain)
    n_visible = sum(
        1 for i, g in enumerate(gold)
        if by_id.get(str(i)) and by_id[str(i)].is_blocker and by_id[str(i)].hallucination_flags
    )
    flag_counts = defaultdict(int)
    for row in hallu_rows:
        for f in row["flags"]:
            flag_counts[f] += 1

    latencies = [e.latency_ms for e in llm_extractions if e.latency_ms and not (e.latency_ms == 0 and e.prompt_tokens == 0)]
    # include failed attempts too (they still have latency)
    latencies = [e.latency_ms for e in llm_extractions if e.latency_ms is not None and (e.fallback or e.raw_parsed_ok or e.latency_ms > 0)]

    def slim(conf):
        return {k: v for k, v in conf.items() if k not in {"fps", "fns", "tps", "tns"}}

    llm_block = {
        "ran": True,
        "n_extractions": len(llm_extractions),
        "n_missing": len(missing),
        "product": slim(prod),
        "product_per_category": per_category(gold, product_pred),
        "raw": slim(raw_conf),
        "raw_n": n_parsed,
        "raw_per_category": per_category(raw_gold, raw_preds_only) if raw_gold else {},
        "hallucination": {
            "definition": (
                "作用于模型原始 JSON（护栏之前）。"
                "citation_ungrounded：citation 非空但对不齐原文；"
                "blocker_without_grounded_citation：声称阻塞但没有可对齐的引用；"
                "reason_invented_number：reason 出现原文没有的阿拉伯数字；"
                "reason_invented_quote：reason 里引号中的片段对不齐原文。"
                "正当改述（不含数字、不含虚构引语）不记为幻觉。"
            ),
            "n_parsed": n_parsed,
            "n_flagged": n_hallu_parsed,
            "rate": (n_hallu_parsed / n_parsed) if n_parsed else None,
            "n_visible_blocker_flagged": n_visible,
            "visible_rate_over_parsed": (n_visible / n_parsed) if n_parsed else None,
            "flag_counts": dict(flag_counts),
            "examples": hallu_rows[:20],
        },
        "fallback": {
            "n": len(fallback_rows),
            "rate": (len(fallback_rows) / len(gold)) if gold else None,
            "examples": fallback_rows[:10],
        },
        "product_fps": [
            {**r, "raw_reason": (by_id[str(r["index"])].raw_reason if str(r["index"]) in by_id else None),
             "citation": (by_id[str(r["index"])].citation_text if str(r["index"]) in by_id else None),
             "flags": (by_id[str(r["index"])].hallucination_flags if str(r["index"]) in by_id else []),
             "fallback": (by_id[str(r["index"])].fallback if str(r["index"]) in by_id else False)}
            for r in prod["fps"]
        ],
        "product_fns": [
            {**r, "raw_is_blocker": (by_id[str(r["index"])].raw_is_blocker if str(r["index"]) in by_id else None),
             "raw_reason": (by_id[str(r["index"])].raw_reason if str(r["index"]) in by_id else None),
             "flags": (by_id[str(r["index"])].hallucination_flags if str(r["index"]) in by_id else []),
             "fallback": (by_id[str(r["index"])].fallback if str(r["index"]) in by_id else False)}
            for r in prod["fns"]
        ],
        "usage": llm_usage or {},
        "note": (
            "product 是护栏之后的对外输出（对不齐原文的阻塞被抑制；"
            "API/解析失败回退关键词）。raw 是模型原始 is_blocker，只在解析成功的样本上计算。"
        ),
    }
    out["llm"] = llm_block
    return out
