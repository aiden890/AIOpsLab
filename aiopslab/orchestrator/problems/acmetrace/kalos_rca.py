# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Kalos GPU Cluster RCA Problems for AcmeTrace dataset."""

import textwrap
from typing import Any

from aiopslab.observer.acme_kalos import KalosDataset
from aiopslab.orchestrator.actions.acme_kalos import KalosActions
from aiopslab.session import SessionItem
from aiopslab.utils.status import InvalidActionError


# Error categories and reasons from AcmeTrace NSDI'24 paper Table 3
CANDIDATE_CATEGORIES = ["Infrastructure", "Framework", "Script"]

CANDIDATE_REASONS = {
    "Infrastructure": [
        "NVLink Error",
        "CUDA Error",
        "Node Failure",
        "ECC Error",
        "Network Error",
        "Connection Error",
        "S3 Storage Error",
        "NCCL Timeout",
        "NCCL Remote Error",
    ],
    "Framework": [
        "Dataloader Killed",
        "Attribute Error",
        "Out of Memory",
        "Runtime Error",
        "Assertion Error",
        "Value Error",
    ],
    "Script": [
        "File Not Found",
        "OS Error",
        "Type Error",
        "Name Error",
        "Permission Error",
        "Import Error",
        "Key Error",
        "Syntax Error",
        "Index Error",
    ],
}

ALL_REASONS = [r for reasons in CANDIDATE_REASONS.values() for r in reasons]


class KalosRCAProblemBase:
    """Base class for Kalos RCA problems.

    Provides common functionality for detection, localization, and analysis tasks.
    """

    def __init__(self, query_row: dict, sample_dir: str = "samples/kalos_rca"):
        """Initialize KalosRCAProblem.

        Args:
            query_row: A row from the query CSV containing job_id, instruction, expected values
            sample_dir: Path to sampled dataset directory
        """
        self.query_row = query_row
        self.job_id = query_row["job_id"]
        self.instruction = query_row["instruction"]
        self.start_time = query_row.get("start_time")
        self.end_time = query_row.get("end_time")

        self.dataset = KalosDataset(sample_dir=sample_dir)
        self.actions = KalosActions(self.dataset)

        # Results storage
        self.results = {}

    def get_available_actions(self) -> dict:
        """Get available actions for this problem."""
        actions = {}
        for method_name in dir(self.actions):
            method = getattr(self.actions, method_name)
            if callable(method) and hasattr(method, "is_action") and method.is_action:
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
        return "acmetrace"


class KalosDetectionProblem(KalosRCAProblemBase):
    """Detection Problem: Determine if a job failed.

    Given job telemetry, the agent must determine if the job experienced a failure.
    """

    def __init__(self, query_row: dict, sample_dir: str = "samples/kalos_rca"):
        super().__init__(query_row, sample_dir)
        self.expected_answer = query_row["expected_answer"]  # "Yes" or "No"

        self.task_desc = """\
            You are an expert GPU cluster engineer assigned to perform failure detection.

            A job has been submitted to the Kalos GPU cluster. Your task is to analyze the
            telemetry data (job traces, GPU metrics, XID errors) and determine if the job
            experienced a failure.

            ## JOB INFORMATION:
            - Job ID: {job_id}
            - Time Window: {start_time} to {end_time}
            """

        self.instructions = """\
            Analyze the job and cluster telemetry using the provided APIs.

            Submit your analysis using the submit() API with a dictionary:
            - "is_failure": bool (True if the job failed, False otherwise)

            Example submission:
            ```
            submit({{"is_failure": True}})
            ```

            IMPORTANT: Include EXACTLY ONE code block per response. Only ONE API call at a time.
            """

    def get_task_description(self) -> str:
        """Get the task description with problem context."""
        desc = textwrap.dedent(self.task_desc).format(
            job_id=self.job_id,
            start_time=self.start_time,
            end_time=self.end_time,
        )
        return desc + "\n\n## PROBLEM:\n" + self.instruction

    def get_instructions(self) -> str:
        """Get instructions for the agent."""
        return textwrap.dedent(self.instructions)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        """Evaluate the submitted solution.

        Args:
            soln: The submitted solution (dict with is_failure)
            trace: The session trace
            duration: Time taken to solve

        Returns:
            dict: Evaluation results
        """
        self.results = {
            "job_id": self.job_id,
            "task_type": "detection",
            "duration": duration,
            "steps": len([t for t in trace if t.role == "assistant"]),
        }

        if soln is None:
            self.results["score"] = 0.0
            self.results["error"] = "No solution submitted"
            return self.results

        pred_failure = soln.get("is_failure")
        gt_failure = self.expected_answer == "Yes"

        self.results["prediction"] = pred_failure
        self.results["ground_truth"] = gt_failure
        self.results["correct"] = pred_failure == gt_failure
        self.results["score"] = 1.0 if self.results["correct"] else 0.0
        self.results["success"] = self.results["correct"]

        return self.results


class KalosLocalizationProblem(KalosRCAProblemBase):
    """Localization Problem: Identify which node/GPU is affected.

    Given a failed job, the agent must identify the affected node and/or GPU.
    """

    def __init__(self, query_row: dict, sample_dir: str = "samples/kalos_rca"):
        super().__init__(query_row, sample_dir)
        self.expected_node = query_row.get("expected_node")
        self.expected_gpu = query_row.get("expected_gpu")

        self.task_desc = """\
            You are an expert GPU cluster engineer assigned to perform failure localization.

            A job has failed on the Kalos GPU cluster. Your task is to analyze the
            telemetry data and identify which node and/or GPU experienced the failure.

            ## JOB INFORMATION:
            - Job ID: {job_id}
            - Time Window: {start_time} to {end_time}

            ## GPU NAMING CONVENTION:
            - GPUs are identified as {{IP}}-{{GPU_INDEX}} (e.g., 172.31.15.112-6)
            - Each node has 8 GPUs (index 0-7)
            """

        self.instructions = """\
            Analyze the job and cluster telemetry using the provided APIs.
            Look for XID errors, abnormal GPU metrics, or other indicators.

            Submit your analysis using the submit() API with a dictionary:
            - "affected_node": str - Node IP address (e.g., "172.31.15.112")
            - "affected_gpu": str - GPU ID (e.g., "172.31.15.112-6")

            Note: You may provide either or both fields depending on what you can determine.

            Example submission:
            ```
            submit({{"affected_node": "172.31.15.112", "affected_gpu": "172.31.15.112-6"}})
            ```

            IMPORTANT: Include EXACTLY ONE code block per response. Only ONE API call at a time.
            """

    def get_task_description(self) -> str:
        """Get the task description with problem context."""
        desc = textwrap.dedent(self.task_desc).format(
            job_id=self.job_id,
            start_time=self.start_time,
            end_time=self.end_time,
        )
        return desc + "\n\n## PROBLEM:\n" + self.instruction

    def get_instructions(self) -> str:
        """Get instructions for the agent."""
        return textwrap.dedent(self.instructions)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        """Evaluate the submitted solution.

        Args:
            soln: The submitted solution (dict with affected_node, affected_gpu)
            trace: The session trace
            duration: Time taken to solve

        Returns:
            dict: Evaluation results
        """
        self.results = {
            "job_id": self.job_id,
            "task_type": "localization",
            "duration": duration,
            "steps": len([t for t in trace if t.role == "assistant"]),
        }

        if soln is None:
            self.results["score"] = 0.0
            self.results["error"] = "No solution submitted"
            return self.results

        correct_count = 0
        total_fields = 0

        # Evaluate node if ground truth exists
        if self.expected_node:
            total_fields += 1
            pred_node = soln.get("affected_node")
            node_correct = pred_node == self.expected_node
            self.results["node_match"] = node_correct
            if node_correct:
                correct_count += 1

        # Evaluate GPU if ground truth exists
        if self.expected_gpu:
            total_fields += 1
            pred_gpu = soln.get("affected_gpu")
            gpu_correct = pred_gpu == self.expected_gpu
            self.results["gpu_match"] = gpu_correct
            if gpu_correct:
                correct_count += 1

        self.results["score"] = correct_count / total_fields if total_fields > 0 else 0.0
        self.results["success"] = self.results["score"] == 1.0

        return self.results


class KalosAnalysisProblem(KalosRCAProblemBase):
    """Analysis Problem: Determine the root cause category and reason.

    Given a failed job, the agent must identify the error category and specific reason.
    """

    def __init__(self, query_row: dict, sample_dir: str = "samples/kalos_rca"):
        super().__init__(query_row, sample_dir)
        self.expected_category = query_row["expected_category"]
        self.expected_reason = query_row["expected_reason"]

        self.task_desc = """\
            You are an expert GPU cluster engineer assigned to perform root cause analysis.

            A job has failed on the Kalos GPU cluster. Your task is to analyze the
            telemetry data and determine the root cause of the failure.

            ## JOB INFORMATION:
            - Job ID: {job_id}
            - Time Window: {start_time} to {end_time}

            ## ERROR CATEGORIES:
            {categories}

            ## POSSIBLE REASONS BY CATEGORY:
            {reasons}
            """

        self.instructions = """\
            Analyze the job and cluster telemetry using the provided APIs.
            Consider job state, XID errors, GPU metrics, and timing patterns.

            Submit your analysis using the submit() API with a dictionary:
            - "category": str - Error category (Infrastructure, Framework, or Script)
            - "reason": str - Specific error reason (e.g., "NVLink Error", "Out of Memory")

            Example submission:
            ```
            submit({{"category": "Infrastructure", "reason": "NVLink Error"}})
            ```

            IMPORTANT: Include EXACTLY ONE code block per response. Only ONE API call at a time.
            """

    def get_task_description(self) -> str:
        """Get the task description with problem context."""
        # Format reasons by category
        reasons_str = ""
        for cat, reasons in CANDIDATE_REASONS.items():
            reasons_str += f"\n### {cat}:\n"
            reasons_str += ", ".join(reasons)

        desc = textwrap.dedent(self.task_desc).format(
            job_id=self.job_id,
            start_time=self.start_time,
            end_time=self.end_time,
            categories=", ".join(CANDIDATE_CATEGORIES),
            reasons=reasons_str,
        )
        return desc + "\n\n## PROBLEM:\n" + self.instruction

    def get_instructions(self) -> str:
        """Get instructions for the agent."""
        return textwrap.dedent(self.instructions)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float) -> dict:
        """Evaluate the submitted solution.

        Args:
            soln: The submitted solution (dict with category, reason)
            trace: The session trace
            duration: Time taken to solve

        Returns:
            dict: Evaluation results
        """
        self.results = {
            "job_id": self.job_id,
            "task_type": "analysis",
            "duration": duration,
            "steps": len([t for t in trace if t.role == "assistant"]),
        }

        if soln is None:
            self.results["score"] = 0.0
            self.results["error"] = "No solution submitted"
            return self.results

        correct_count = 0

        # Evaluate category
        pred_category = soln.get("category")
        category_correct = pred_category == self.expected_category
        self.results["category_match"] = category_correct
        if category_correct:
            correct_count += 1

        # Evaluate reason (case-insensitive, normalized)
        pred_reason = soln.get("reason")
        reason_correct = self._eval_reason(pred_reason, self.expected_reason)
        self.results["reason_match"] = reason_correct
        if reason_correct:
            correct_count += 1

        self.results["score"] = correct_count / 2
        self.results["success"] = self.results["score"] == 1.0

        return self.results

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
