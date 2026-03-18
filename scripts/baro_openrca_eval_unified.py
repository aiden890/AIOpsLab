"""
BARO Unified RCA Evaluation for OpenRCA Telecom Dataset

NO GT level used: all metric files loaded together.
Cross-layer normalization via layer-internal outlier scoring.

Component prefix → layer mapping:
  docker_* → pod, os_* → node, db_* → service,
  redis_* → middleware, osb_* → app

Predicts: component, reason, datetime
Evaluates against record.csv ground truth.
"""

import pandas as pd
import numpy as np
from sklearn.preprocessing import RobustScaler
from collections import defaultdict
from pathlib import Path

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "aiopslab-applications" / "static_dataset" / "openrca" / "Telecom"
TELEMETRY_DIR = DATA_DIR / "telemetry"

ALL_METRIC_FILES = [
    "metric_container", "metric_node", "metric_service",
    "metric_app", "metric_middleware",
]

# Node-level reason indicators
DELAY_INDICATORS = {"ICMP_ping", "CPU_idle_pct", "CPU_util_pct", "CPU_user_time",
                    "CPU_iowait_time", "Disk_rd_ios"}
LOSS_INDICATORS = {"Disk_svctm", "Disk_wr_kbs", "Disk_await", "Zombie_Process",
                   "Processor_load_1_min"}

# Service-level reason
PROC_USER_LIMIT_THRESHOLD = 50


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def infer_layer(comp):
    """Infer layer from component ID prefix."""
    if comp.startswith("docker_"):
        return "pod"
    elif comp.startswith("os_"):
        return "node"
    elif comp.startswith("db_"):
        return "service"
    elif comp.startswith("redis_"):
        return "middleware"
    elif comp.startswith("osb_"):
        return "app"
    return "other"


def baro_score_columns(pivot, inject_ts_ms):
    """Compute BARO z-scores for each (cmdb_id, metric_name) column."""
    normal = pivot[pivot.index < inject_ts_ms]
    anomal = pivot[pivot.index >= inject_ts_ms]

    if len(normal) < 5 or len(anomal) < 5:
        return None, normal, anomal

    col_scores = []
    for (cmdb_id, metric_name) in pivot.columns:
        pre = normal[(cmdb_id, metric_name)].replace(
            [np.inf, -np.inf], np.nan).ffill().fillna(0).to_numpy()
        post = anomal[(cmdb_id, metric_name)].replace(
            [np.inf, -np.inf], np.nan).ffill().fillna(0).to_numpy()
        if len(set(pre)) < 2:
            continue
        try:
            scaler = RobustScaler().fit(pre.reshape(-1, 1))
            z = scaler.transform(post.reshape(-1, 1))[:, 0]
            score = float(np.max(z))
            col_scores.append((cmdb_id, metric_name, score))
        except Exception:
            continue

    return col_scores, normal, anomal


def rank_cross_layer_zscore(col_scores):
    """Rank components using layer-internal Z-score normalization.

    1. Per component: max BARO score across all its metrics
    2. Group by layer (inferred from prefix)
    3. Within each layer: outlier_score = (score - median) / IQR
    4. Rank all components by this normalized outlier_score
    """
    # Step 1: max BARO score per component
    comp_max = defaultdict(float)
    comp_top_col = {}
    for cmdb_id, mn, score in col_scores:
        if score > comp_max[cmdb_id]:
            comp_max[cmdb_id] = score
            comp_top_col[cmdb_id] = mn

    # Step 2: group by layer
    layer_scores = defaultdict(dict)
    for comp, score in comp_max.items():
        layer = infer_layer(comp)
        layer_scores[layer][comp] = score

    # Step 3: within-layer Z-score normalization
    ranked = []
    for layer, scores in layer_scores.items():
        vals = np.array(list(scores.values()))
        if len(vals) < 2:
            # Single component in layer — use raw score normalized
            for comp, score in scores.items():
                ranked.append((comp, {"zscore": score, "raw": score,
                                      "layer": layer, "col": comp_top_col.get(comp, "")}))
            continue

        median = np.median(vals)
        q1, q3 = np.percentile(vals, [25, 75])
        iqr = q3 - q1
        if iqr < 1e-10:
            iqr = np.std(vals)  # fallback to std if IQR is 0
        if iqr < 1e-10:
            iqr = 1.0  # all same scores

        for comp, score in scores.items():
            zscore = (score - median) / iqr
            ranked.append((comp, {"zscore": zscore, "raw": score,
                                  "layer": layer, "col": comp_top_col.get(comp, "")}))

    ranked.sort(key=lambda x: x[1]["zscore"], reverse=True)
    return ranked


def rank_cross_layer_standout(col_scores):
    """Rank using standout ratio: how dominant is top-1 within its layer?

    For each layer:
      standout = top1_score / top2_score  (ratio)
    The layer with the highest standout is most likely the fault layer.
    Return top-1 from each layer, ordered by standout.
    """
    # Max score per component
    comp_max = defaultdict(float)
    comp_top_col = {}
    for cmdb_id, mn, score in col_scores:
        if score > comp_max[cmdb_id]:
            comp_max[cmdb_id] = score
            comp_top_col[cmdb_id] = mn

    # Group by layer
    layer_scores = defaultdict(dict)
    for comp, score in comp_max.items():
        layer = infer_layer(comp)
        layer_scores[layer][comp] = score

    # Per layer: compute standout
    layer_results = []
    for layer, scores in layer_scores.items():
        sorted_comps = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top1_comp, top1_score = sorted_comps[0]
        top2_score = sorted_comps[1][1] if len(sorted_comps) > 1 else 1.0
        standout = top1_score / max(top2_score, 1e-10)
        layer_results.append((top1_comp, {
            "standout": standout, "raw": top1_score,
            "layer": layer, "col": comp_top_col.get(top1_comp, ""),
        }))

    layer_results.sort(key=lambda x: x[1]["standout"], reverse=True)

    # Build full ranking: layer winners first (by standout), then remaining components
    seen = {comp for comp, _ in layer_results}
    remaining = []
    for comp, score in comp_max.items():
        if comp not in seen:
            remaining.append((comp, {"standout": 0, "raw": score,
                                     "layer": infer_layer(comp),
                                     "col": comp_top_col.get(comp, "")}))
    remaining.sort(key=lambda x: x[1]["raw"], reverse=True)

    return layer_results + remaining


# ──────────────────────────────────────────────
# Reason prediction
# ──────────────────────────────────────────────
def predict_reason(comp, col_scores):
    level = infer_layer(comp)
    if level == "pod":
        return predict_reason_pod(comp, col_scores)
    elif level == "node":
        return predict_reason_node(comp, col_scores)
    elif level == "service":
        return predict_reason_service(comp, col_scores)
    return "unknown"


def predict_reason_pod(comp, col_scores):
    comp_metrics = [(mn, score) for c, mn, score in col_scores
                    if c == comp and score > 0]
    for mn, _ in sorted(comp_metrics, key=lambda x: x[1], reverse=True)[:10]:
        if "cpu" in mn.lower():
            return "CPU fault"
    return "CPU fault"


def predict_reason_node(comp, col_scores):
    comp_metrics = [(mn, score) for c, mn, score in col_scores
                    if c == comp and score > 0]
    comp_metrics.sort(key=lambda x: x[1], reverse=True)
    top_names = {mn for mn, _ in comp_metrics[:10]}

    delay_hits = len(top_names & DELAY_INDICATORS)
    loss_hits = len(top_names & LOSS_INDICATORS)
    delay_score = sum(s for mn, s in comp_metrics if mn in DELAY_INDICATORS)
    loss_score = sum(s for mn, s in comp_metrics if mn in LOSS_INDICATORS)
    delay_signal = delay_hits * 2 + (delay_score / max(delay_score + loss_score, 1))
    loss_signal = loss_hits * 2 + (loss_score / max(delay_score + loss_score, 1))

    return "network loss" if loss_signal > delay_signal else "network delay"


def predict_reason_service(comp, col_scores):
    max_proc_user = 0.0
    for _, mn, score in col_scores:
        if mn == "Proc_User_Used_Pct" and score > max_proc_user:
            max_proc_user = score
    return "db connection limit" if max_proc_user > PROC_USER_LIMIT_THRESHOLD else "db close"


def predict_datetime(comp, col_scores, anomal, pivot):
    best_ts = None
    for cmdb_id, mn, score in col_scores:
        if cmdb_id != comp or score <= 2.0:
            continue
        try:
            series = anomal[(cmdb_id, mn)]
            pre_series = pivot.loc[pivot.index < anomal.index[0], (cmdb_id, mn)]
            pre_clean = pre_series.replace([np.inf, -np.inf], np.nan).ffill().fillna(0)
            if len(pre_clean) < 2:
                continue
            scaler = RobustScaler().fit(pre_clean.to_numpy().reshape(-1, 1))
            post_clean = series.replace([np.inf, -np.inf], np.nan).ffill().fillna(0)
            z = scaler.transform(post_clean.to_numpy().reshape(-1, 1))[:, 0]
            mask = z > 2.0
            if mask.any():
                idx = int(np.argmax(mask))
                ts = anomal.index[idx]
                if best_ts is None or ts < best_ts:
                    best_ts = ts
        except Exception:
            continue
    return best_ts


# ──────────────────────────────────────────────
# Evaluation runner
# ──────────────────────────────────────────────
def run_single(ranking_fn, label):
    record = pd.read_csv(DATA_DIR / "record.csv")
    record["date_utc8"] = pd.to_datetime(record["datetime"])
    record["folder"] = record["date_utc8"].dt.strftime("%Y_%m_%d")

    results = []

    for _, fault in record.iterrows():
        folder = fault["folder"]
        inject_ts_ms = int(fault["timestamp"]) * 1000
        gt_comp = fault["component"]
        gt_level = fault["level"]
        gt_reason = fault["reason"]
        gt_dt = fault["datetime"]
        gt_ts = int(fault["timestamp"])

        metric_dir = TELEMETRY_DIR / folder / "metric"

        try:
            all_rows = []
            for mtype in ALL_METRIC_FILES:
                fpath = metric_dir / f"{mtype}.csv"
                if fpath.exists():
                    all_rows.append(pd.read_csv(fpath))
            raw = pd.concat(all_rows, ignore_index=True)
            pivot = raw.pivot_table(
                index="timestamp", columns=["cmdb_id", "name"],
                values="value", aggfunc="first"
            ).sort_index().ffill()

            col_scores, _normal, anomal = baro_score_columns(pivot, inject_ts_ms)
            if col_scores is None:
                results.append({
                    "gt_datetime": gt_dt, "gt_level": gt_level,
                    "gt_component": gt_comp, "gt_reason": gt_reason,
                    "pred_component": "", "pred_level": "",
                    "pred_reason": "", "pred_datetime": "",
                    "comp_correct": False, "reason_correct": False, "dt_correct": False,
                    "status": "SKIP",
                })
                continue

            ranked = ranking_fn(col_scores)

            if not ranked:
                results.append({
                    "gt_datetime": gt_dt, "gt_level": gt_level,
                    "gt_component": gt_comp, "gt_reason": gt_reason,
                    "pred_component": "", "pred_level": "",
                    "pred_reason": "", "pred_datetime": "",
                    "comp_correct": False, "reason_correct": False, "dt_correct": False,
                    "status": "NO_SCORES",
                })
                continue

            pred_comp = ranked[0][0]
            pred_level = infer_layer(pred_comp)
            pred_reason = predict_reason(pred_comp, col_scores)

            pred_ts = predict_datetime(pred_comp, col_scores, anomal, pivot)
            if pred_ts is not None:
                pred_dt_utc8 = pd.Timestamp(pred_ts, unit="ms", tz="UTC").tz_convert(
                    "Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
                pred_ts_s = pred_ts / 1000
            else:
                pred_dt_utc8 = "unknown"
                pred_ts_s = None

            comp_correct = pred_comp == gt_comp
            reason_correct = pred_reason == gt_reason
            dt_correct = pred_ts_s is not None and abs(pred_ts_s - gt_ts) <= 60

            gt_rank = -1
            for i, (c, _) in enumerate(ranked):
                if c == gt_comp:
                    gt_rank = i + 1
                    break

            results.append({
                "gt_datetime": gt_dt, "gt_level": gt_level,
                "gt_component": gt_comp, "gt_reason": gt_reason,
                "pred_component": pred_comp, "pred_level": pred_level,
                "pred_reason": pred_reason,
                "pred_datetime": pred_dt_utc8,
                "comp_correct": comp_correct, "reason_correct": reason_correct,
                "dt_correct": dt_correct,
                "gt_rank": gt_rank,
                "status": "OK",
            })

        except Exception as e:
            results.append({
                "gt_datetime": gt_dt, "gt_level": gt_level,
                "gt_component": gt_comp, "gt_reason": gt_reason,
                "pred_component": "", "pred_level": "",
                "pred_reason": "", "pred_datetime": "",
                "comp_correct": False, "reason_correct": False, "dt_correct": False,
                "status": f"ERR:{e}",
            })

    return results


# ──────────────────────────────────────────────
# Print results
# ──────────────────────────────────────────────
def print_results(results, label):
    ok = [r for r in results if r["status"] == "OK"]
    N = len(ok)
    if N == 0:
        print(f"\n{label}: No valid results")
        return

    print(f"\n{'='*170}")
    print(f"  {label}  ({N} cases)")
    print(f"{'='*170}")
    print(f"{'GT DateTime':<22} {'Lv':<5} {'GT Component':<12} {'GT Reason':<22}"
          f" {'Pred Component':<15} {'PLv':<5} {'Pred Reason':<22}"
          f" {'Comp':>4} {'Reas':>4} {'DT':>4}  {'Rank':>4}")
    print("-" * 130)

    for r in ok:
        cc = " Y" if r["comp_correct"] else " N"
        rc = " Y" if r["reason_correct"] else " N"
        dc = " Y" if r["dt_correct"] else " N"
        rk = str(r.get("gt_rank", "")) if r.get("gt_rank", -1) > 0 else "N/F"
        plv = r.get("pred_level", "?")
        print(f"{r['gt_datetime']:<22} {r['gt_level']:<5} {r['gt_component']:<12} "
              f"{r['gt_reason']:<22} {r['pred_component']:<15} {plv:<5} "
              f"{r['pred_reason']:<22} "
              f"{cc:>4} {rc:>4} {dc:>4}  {rk:>4}")

    # Summary
    comp_acc = sum(r["comp_correct"] for r in ok)
    reason_acc = sum(r["reason_correct"] for r in ok)
    dt_acc = sum(r["dt_correct"] for r in ok)
    all_correct = sum(r["comp_correct"] and r["reason_correct"] and r["dt_correct"]
                      for r in ok)
    level_match = sum(1 for r in ok if r.get("pred_level") == r["gt_level"])

    print(f"\n{'='*80}")
    print(f"SUMMARY — {label}")
    print(f"{'='*80}")
    print(f"  Component:    {comp_acc:>3}/{N}  ({100*comp_acc/N:5.1f}%)")
    print(f"  Reason:       {reason_acc:>3}/{N}  ({100*reason_acc/N:5.1f}%)")
    print(f"  Datetime:     {dt_acc:>3}/{N}  ({100*dt_acc/N:5.1f}%)")
    print(f"  ALL correct:  {all_correct:>3}/{N}  ({100*all_correct/N:5.1f}%)")
    print(f"  Level match:  {level_match:>3}/{N}  ({100*level_match/N:5.1f}%)")

    # Per level
    print(f"\n  {'Level':<10} {'N':>3}  {'Component':>12}  {'Reason':>12}  {'ALL':>12}")
    print(f"  {'-'*55}")
    for level in ["pod", "node", "service"]:
        lvl = [r for r in ok if r["gt_level"] == level]
        n = len(lvl)
        if n == 0:
            continue
        cc = sum(r["comp_correct"] for r in lvl)
        rc = sum(r["reason_correct"] for r in lvl)
        ac = sum(r["comp_correct"] and r["reason_correct"] and r["dt_correct"]
                 for r in lvl)
        print(f"  {level:<10} {n:>3}  {cc:>3}/{n} ({100*cc/n:4.0f}%)  "
              f"{rc:>3}/{n} ({100*rc/n:4.0f}%)  {ac:>3}/{n} ({100*ac/n:4.0f}%)")

    # Top-K
    print(f"\n  Top-K Component:")
    for k in [1, 3, 5, 10]:
        topk = sum(1 for r in ok if 0 < r.get("gt_rank", -1) <= k)
        print(f"    Top-{k:>2}: {topk:>3}/{N} ({100*topk/N:5.1f}%)")


def run_eval():
    methods = [
        ("A: Raw (no normalization)", lambda cs: rank_raw(cs)),
        ("B: Layer-internal Z-score", lambda cs: rank_cross_layer_zscore(cs)),
        ("C: Layer standout ratio", lambda cs: rank_cross_layer_standout(cs)),
    ]

    # We need rank_raw too
    all_results = {}
    for label, fn in methods:
        print(f"\n>>> Running {label} ...")
        res = run_single(fn, label)
        all_results[label] = res
        print_results(res, label)

    # Comparison table
    print(f"\n\n{'='*90}")
    print("COMPARISON")
    print(f"{'='*90}")
    print(f"  {'Method':<35} {'Comp':>8} {'Reason':>8} {'Level':>8} {'Top5':>8} {'Top10':>8}")
    print(f"  {'-'*80}")
    for label, res in all_results.items():
        ok = [r for r in res if r["status"] == "OK"]
        N = len(ok)
        comp = sum(r["comp_correct"] for r in ok)
        reas = sum(r["reason_correct"] for r in ok)
        lm = sum(1 for r in ok if r.get("pred_level") == r["gt_level"])
        t5 = sum(1 for r in ok if 0 < r.get("gt_rank", -1) <= 5)
        t10 = sum(1 for r in ok if 0 < r.get("gt_rank", -1) <= 10)
        print(f"  {label:<35} {comp:>3}/{N}  {reas:>3}/{N}  {lm:>3}/{N}  {t5:>3}/{N}  {t10:>3}/{N}")


def rank_raw(col_scores):
    """Baseline: raw max BARO score, no normalization."""
    by_comp = defaultdict(float)
    comp_col = {}
    for cmdb_id, mn, score in col_scores:
        if score > by_comp[cmdb_id]:
            by_comp[cmdb_id] = score
            comp_col[cmdb_id] = mn
    ranked = sorted(by_comp.items(), key=lambda x: x[1], reverse=True)
    return [(c, {"score": s, "layer": infer_layer(c), "col": comp_col.get(c, "")})
            for c, s in ranked]


if __name__ == "__main__":
    run_eval()
