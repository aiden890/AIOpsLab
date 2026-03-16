"""OpenRCA evaluation logic.

Ported from microsoft/OpenRCA evaluate.py.
Scores predictions against scoring_points using regex matching.
"""

import json
import re
import itertools
from datetime import datetime


def _parse_prediction_json(prediction: str) -> list[dict]:
    """Parse prediction string to list of {root cause component, reason, datetime}.
    Prefer json.loads to avoid regex key-order issues; fall back to regex.
    """
    prediction = (prediction or "").strip()
    # Try JSON first (handles {"1": {"root cause component": ..., "root cause reason": ..., "root cause occurrence datetime": ...}})
    try:
        data = json.loads(prediction)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and v:
                    return [{
                        "root cause component": (v.get("root cause component") or ""),
                        "root cause reason": (v.get("root cause reason") or ""),
                        "root cause occurrence datetime": (v.get("root cause occurrence datetime") or ""),
                    }]
        return []
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: regex (key order in pattern: datetime, component, reason)
    q = r'["\']'
    predict_pattern = (
        r'{\s*'
        rf'(?:{q}root cause occurrence datetime{q}:\s*{q}(.*?){q})?,?\s*'
        rf'(?:{q}root cause component{q}:\s*{q}(.*?){q})?,?\s*'
        rf'(?:{q}root cause reason{q}:\s*{q}(.*?){q})?\s*'
        r'}'
    )
    predict_matches = re.findall(predict_pattern, prediction)
    out = []
    for match in predict_matches:
        datetime_str, component, reason = match
        out.append({
            "root cause occurrence datetime": datetime_str or "",
            "root cause component": component or "",
            "root cause reason": reason or "",
        })
    return out


def openrca_evaluate(prediction: str, scoring_points: str):
    """
    Evaluate a single prediction against scoring_points.

    Args:
        prediction: JSON-like string with root cause predictions.
        scoring_points: Ground truth string from query.csv.

    Returns:
        tuple: (passing_criteria, failing_criteria, score, detail)
            - passing_criteria: list of matched items
            - failing_criteria: list of unmatched items
            - score: float 0.0~1.0 (1.0 = perfect)
            - detail: dict with per-field results:
                correct_component (bool), correct_reason (bool),
                correct_time (bool), time_diff_min (float|None),
                pred_component, pred_reason, pred_time,
                gt_component, gt_reason, gt_time
    """
    # Parse prediction JSON (prefer json.loads so key order doesn't break extraction)
    predict_results = _parse_prediction_json(prediction)

    prediction_length = len(predict_results)

    # Parse scoring_points
    component_pattern = r"The (?:\d+-th|only) predicted root cause component is ([^\n]+)"
    reason_pattern = r"The (?:\d+-th|only) predicted root cause reason is ([^\n]+)"
    time_pattern = (
        r"The (?:\d+-th|only) root cause occurrence time is within "
        r"1 minutes \(i\.e\., <=1min\) of ([^\n]+)"
    )

    components = re.findall(component_pattern, scoring_points)
    reasons = re.findall(reason_pattern, scoring_points)
    times = re.findall(time_pattern, scoring_points)

    scoringpoints_length = max(len(components), len(reasons), len(times))
    scores_num = len(components) + len(reasons) + len(times)

    if scores_num == 0:
        return [], [], 0.0, {}

    scores_get = 0
    passing_criteria = []
    failing_criteria = []

    if scoringpoints_length == prediction_length:
        best_score = -1
        for perm in itertools.permutations(predict_results):
            current_score = 0
            current_passing = []
            for i in range(scoringpoints_length):
                if len(components) == scoringpoints_length:
                    if perm[i]["root cause component"] == components[i]:
                        current_score += 1
                        current_passing.append(components[i])
                if len(reasons) == scoringpoints_length:
                    if perm[i]["root cause reason"] == reasons[i]:
                        current_score += 1
                        current_passing.append(reasons[i])
                if len(times) == scoringpoints_length:
                    if _time_within_1min(
                        times[i], perm[i]["root cause occurrence datetime"]
                    ):
                        current_score += 1
                        current_passing.append(times[i])
            if current_score > best_score:
                best_score = current_score
                passing_criteria = current_passing
        scores_get = best_score

    failing_criteria = list(
        set(components + reasons + times) - set(passing_criteria)
    )

    final_score = scores_get / scores_num

    # --- Per-field detail (first prediction vs first ground truth) ---
    pred = predict_results[0] if predict_results else {}
    gt_comp = components[0] if components else ""
    gt_reason = reasons[0] if reasons else ""
    gt_time = times[0] if times else ""
    pred_comp = pred.get("root cause component", "")
    pred_reason = pred.get("root cause reason", "")
    pred_time = pred.get("root cause occurrence datetime", "")

    detail = {
        "correct_component": pred_comp == gt_comp if gt_comp else None,
        "correct_reason": pred_reason == gt_reason if gt_reason else None,
        "correct_time": _time_within_1min(gt_time, pred_time) if gt_time else None,
        "time_diff_min": _time_diff_minutes(gt_time, pred_time),
        "pred_component": pred_comp,
        "pred_reason": pred_reason,
        "pred_time": pred_time,
        "gt_component": gt_comp,
        "gt_reason": gt_reason,
        "gt_time": gt_time,
    }

    return passing_criteria, failing_criteria, round(final_score, 2), detail


def _time_diff_minutes(expected_str: str, predicted_str: str):
    """Return absolute difference in minutes between two datetime strings, or None."""
    time_format = "%Y-%m-%d %H:%M:%S"
    try:
        t1 = datetime.strptime(expected_str.strip(), time_format)
        t2 = datetime.strptime(predicted_str.strip(), time_format)
        return round(abs((t1 - t2).total_seconds()) / 60.0, 1)
    except (ValueError, AttributeError):
        return None


def _time_within_1min(expected_str: str, predicted_str: str) -> bool:
    """Check if two datetime strings are within 1 minute of each other."""
    time_format = "%Y-%m-%d %H:%M:%S"
    try:
        t1 = datetime.strptime(expected_str.strip(), time_format)
        t2 = datetime.strptime(predicted_str.strip(), time_format)
        return abs((t1 - t2).total_seconds()) <= 60
    except ValueError:
        return False


def get_task_difficulty(task_type: str) -> str:
    """Map task type to difficulty level."""
    try:
        num = int(task_type.split("_")[1])
        if num <= 3:
            return "easy"
        elif num <= 6:
            return "middle"
        else:
            return "hard"
    except (IndexError, ValueError):
        # Non-standard task types (e.g., "task_rca") → hard (full RCA)
        return "hard"
