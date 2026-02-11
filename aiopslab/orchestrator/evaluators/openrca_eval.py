"""OpenRCA evaluation logic.

Ported from microsoft/OpenRCA evaluate.py.
Scores predictions against scoring_points using regex matching.
"""

import re
import itertools
from datetime import datetime


def openrca_evaluate(prediction: str, scoring_points: str):
    """
    Evaluate a single prediction against scoring_points.

    Args:
        prediction: JSON-like string with root cause predictions.
        scoring_points: Ground truth string from query.csv.

    Returns:
        tuple: (passing_criteria, failing_criteria, score)
            - passing_criteria: list of matched items
            - failing_criteria: list of unmatched items
            - score: float 0.0~1.0 (1.0 = perfect)
    """
    # Parse prediction JSON
    predict_pattern = (
        r'{\s*'
        r'(?:"root cause occurrence datetime":\s*"(.*?)")?,?\s*'
        r'(?:"root cause component":\s*"(.*?)")?,?\s*'
        r'(?:"root cause reason":\s*"(.*?)")?\s*}'
    )
    predict_matches = re.findall(predict_pattern, prediction)

    predict_results = []
    for match in predict_matches:
        datetime_str, component, reason = match
        predict_results.append({
            "root cause occurrence datetime": datetime_str,
            "root cause component": component,
            "root cause reason": reason,
        })

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
        return [], [], 0.0

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
    return passing_criteria, failing_criteria, round(final_score, 2)


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
    num = int(task_type.split("_")[1])
    if num <= 3:
        return "easy"
    elif num <= 6:
        return "middle"
    else:
        return "hard"
