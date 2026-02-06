# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bank RCA Problem for OpenRCA dataset."""

import re
import textwrap
from typing import Any
from datetime import datetime

from aiopslab.observer.openrca_bank import BankDataset
from aiopslab.orchestrator.actions.openrca_bank import OpenrcaBankActions
from aiopslab.session import SessionItem
from aiopslab.utils.actions import get_actions
from aiopslab.utils.status import InvalidActionError


# Mapping from task_index to evaluation fields
TASK_EVAL_FIELDS = {
    "task_1": ["time"],
    "task_2": ["reason"],
    "task_3": ["component"],
    "task_4": ["time", "reason"],
    "task_5": ["time", "component"],
    "task_6": ["component", "reason"],
    "task_7": ["time", "component", "reason"],
}


class BankRCAProblem:
    """RCA Problem for OpenRCA Bank domain.

    Attributes:
        CANDIDATE_COMPONENTS: List of possible root cause components
        CANDIDATE_REASONS: List of possible failure reasons
    """

    CANDIDATE_COMPONENTS = [
        "apache01", "apache02",
        "Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04",
        "MG01", "MG02",
        "IG01", "IG02",
        "Mysql01", "Mysql02",
        "Redis01", "Redis02",
    ]

    CANDIDATE_REASONS = [
        "high CPU usage",
        "high memory usage",
        "network latency",
        "network packet loss",
        "disk I/O bottleneck",
        "connection pool exhaustion",
        "thread pool exhaustion",
        "garbage collection pause",
        "database lock contention",
        "cache miss spike",
    ]

    def __init__(self, query_row: dict, date: str):
        """Initialize BankRCAProblem.

        Args:
            query_row: A row from query.csv containing task_index, instruction, scoring_points
            date: Date folder for telemetry data (e.g., "2021_03_04")
        """
        self.task_index = query_row["task_index"]
        self.instruction = query_row["instruction"]
        self.scoring_points = query_row["scoring_points"]
        self.date = date

        self.dataset = BankDataset(date=date)
        self.actions = OpenrcaBankActions(self.dataset)

        # Parse ground truth from scoring_points
        self.ground_truth = self._parse_scoring_points()

        # Results storage
        self.results = {}

        # Task description
        self.task_desc = """\
            You are an expert DevOps engineer assigned to perform Root Cause Analysis (RCA) on a Bank system.

            A failure has been detected in the system. Your task is to analyze the telemetry data
            (metrics, traces, logs) and identify the root cause.

            ## AVAILABLE COMPONENTS:
            {components}

            ## POSSIBLE FAILURE REASONS:
            {reasons}
            """

        # Instructions for the agent
        self.instructions = """\
            Analyze the telemetry data using the provided APIs and identify the root cause.

            Submit your analysis using the submit() API with a dictionary containing:
            - "root cause occurrence datetime": The exact time when the root cause occurred (format: "YYYY-MM-DD HH:MM:SS")
            - "root cause component": The component where the root cause occurred
            - "root cause reason": The reason for the failure

            Note: Depending on the task type, not all fields may be required.

            Example submission:
            ```
            submit({{"root cause occurrence datetime": "2021-03-04 14:57:00", "root cause component": "Redis02", "root cause reason": "high memory usage"}})
            ```

            IMPORTANT: All API calls must be written inside a markdown code block.
            """

    def _parse_scoring_points(self) -> dict:
        """Parse scoring_points to extract ground truth values.

        Returns:
            dict with keys: time, component, reason (as applicable)
        """
        ground_truth = {}

        # Extract time (format: "YYYY-MM-DD HH:MM:SS")
        time_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", self.scoring_points)
        if time_match:
            ground_truth["time"] = time_match.group(1)

        # Extract component from records based on the date
        # The scoring_points typically references the record, so we parse the known patterns
        component_patterns = [
            r"root cause component is\s+(\w+)",
            r"root cause is\s+(\w+)",
            r"(\w+)\s+is the root cause",
            r"component[:\s]+(\w+)",
        ]
        for pattern in component_patterns:
            match = re.search(pattern, self.scoring_points, re.IGNORECASE)
            if match:
                ground_truth["component"] = match.group(1)
                break

        # Extract reason
        reason_patterns = [
            r"reason[:\s]+(.+?)(?:\.|$)",
            r"due to\s+(.+?)(?:\.|$)",
            r"caused by\s+(.+?)(?:\.|$)",
        ]
        for pattern in reason_patterns:
            match = re.search(pattern, self.scoring_points, re.IGNORECASE)
            if match:
                ground_truth["reason"] = match.group(1).strip()
                break

        return ground_truth

    def get_task_description(self) -> str:
        """Get the task description with problem context."""
        desc = textwrap.dedent(self.task_desc).format(
            components=", ".join(self.CANDIDATE_COMPONENTS),
            reasons=", ".join(self.CANDIDATE_REASONS),
        )
        return desc + "\n\n## PROBLEM:\n" + self.instruction

    def get_instructions(self) -> str:
        """Get instructions for the agent."""
        return textwrap.dedent(self.instructions)

    def get_available_actions(self) -> dict:
        """Get available actions for this problem."""
        # Get actions from the actions class
        actions = {}
        for method_name in dir(self.actions):
            method = getattr(self.actions, method_name)
            if callable(method) and getattr(method, "is_action", False):
                actions[method_name] = method.__doc__.strip() if method.__doc__ else ""
        return actions

    def perform_action(self, action_name: str, *args, **kwargs):
        """Execute an action.

        Args:
            action_name: Name of the action to perform
            *args, **kwargs: Arguments for the action

        Returns:
            Result of the action
        """
        action_method = getattr(self.actions, action_name, None)

        if action_method is not None and callable(action_method):
            return action_method(*args, **kwargs)
        else:
            raise InvalidActionError(action_name)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        """Evaluate the submitted solution.

        Args:
            soln: The submitted solution (dict with RCA results)
            trace: The session trace
            duration: Time taken to solve

        Returns:
            dict: Evaluation results
        """
        self.results = {
            "task_index": self.task_index,
            "duration": duration,
            "steps": len([t for t in trace if t.role == "assistant"]),
        }

        if soln is None:
            self.results["score"] = 0.0
            self.results["error"] = "No solution submitted"
            return self.results

        eval_fields = TASK_EVAL_FIELDS.get(self.task_index, ["time", "component", "reason"])
        correct_count = 0
        total_fields = len(eval_fields)

        # Evaluate each required field
        if "time" in eval_fields:
            pred_time = soln.get("root cause occurrence datetime")
            gt_time = self.ground_truth.get("time")
            time_correct = self._eval_time(pred_time, gt_time)
            self.results["time_match"] = time_correct
            if time_correct:
                correct_count += 1

        if "component" in eval_fields:
            pred_component = soln.get("root cause component")
            gt_component = self.ground_truth.get("component")
            component_correct = (pred_component == gt_component)
            self.results["component_match"] = component_correct
            if component_correct:
                correct_count += 1

        if "reason" in eval_fields:
            pred_reason = soln.get("root cause reason")
            gt_reason = self.ground_truth.get("reason")
            # Normalize for comparison
            reason_correct = self._eval_reason(pred_reason, gt_reason)
            self.results["reason_match"] = reason_correct
            if reason_correct:
                correct_count += 1

        self.results["score"] = correct_count / total_fields if total_fields > 0 else 0.0
        self.results["success"] = self.results["score"] == 1.0

        return self.results

    def _eval_time(self, pred: str, gt: str) -> bool:
        """Evaluate time prediction with ±1 minute tolerance.

        Args:
            pred: Predicted time string
            gt: Ground truth time string

        Returns:
            bool: True if within 1 minute tolerance
        """
        if pred is None or gt is None:
            return False

        try:
            pred_dt = datetime.strptime(pred, "%Y-%m-%d %H:%M:%S")
            gt_dt = datetime.strptime(gt, "%Y-%m-%d %H:%M:%S")
            diff_seconds = abs((pred_dt - gt_dt).total_seconds())
            return diff_seconds <= 60  # ±1 minute tolerance
        except ValueError:
            return False

    def _eval_reason(self, pred: str, gt: str) -> bool:
        """Evaluate reason with normalized comparison.

        Args:
            pred: Predicted reason
            gt: Ground truth reason

        Returns:
            bool: True if reasons match (case-insensitive)
        """
        if pred is None or gt is None:
            return False

        # Normalize: lowercase and remove extra whitespace
        pred_norm = " ".join(pred.lower().split())
        gt_norm = " ".join(gt.lower().split())

        return pred_norm == gt_norm

    # Stub methods for compatibility with existing orchestrator
    def inject_fault(self):
        """No-op: Static dataset doesn't require fault injection."""
        pass

    def recover_fault(self):
        """No-op: Static dataset doesn't require fault recovery."""
        pass

    def start_workload(self):
        """No-op: Static dataset doesn't require workload generation."""
        pass

    @property
    def namespace(self):
        """Return a placeholder namespace for compatibility."""
        return "openrca"
