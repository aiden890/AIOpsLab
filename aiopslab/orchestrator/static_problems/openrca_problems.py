"""OpenRCA static replay problem definitions.

Supports all OpenRCA datasets: Market (cloudbed-1/2), Telecom, Bank.
Uses original root cause fields (component, reason, datetime) for analysis evaluation.
"""

from typing import Any

from aiopslab.orchestrator.tasks.detection import DetectionTask
from aiopslab.orchestrator.tasks.localization import LocalizationTask
from aiopslab.orchestrator.tasks.analysis import AnalysisTask
from aiopslab.orchestrator.evaluators.quantitative import is_exact_match, is_subset
from aiopslab.service.apps.openrca import OpenRCAService
from aiopslab.generators.fault.inject_static import StaticFaultInjector
from aiopslab.session import SessionItem


class OpenRCAStaticBaseTask:
    """Base task for OpenRCA static replay problems.

    Configurable via constructor parameters to support all datasets and scenarios.
    """

    def __init__(self, config_file: str, scenario_id: int, dataset_type: str = "market"):
        self.app = OpenRCAService(config_file, scenario_id=scenario_id)
        self.namespace = self.app.namespace
        self.dataset_type = dataset_type

        # Set up static fault injector
        self.injector = StaticFaultInjector(
            self.app.dataset_path, scenario_id, dataset_type
        )

        # Ground truth from dataset
        self.fault_info = self.app.fault_info
        self.faulty_service = self.fault_info["faulty_service"]

    def inject_fault(self):
        """Select the pre-recorded fault scenario (no live injection)."""
        print("== Static Fault Selection ==")
        self.injector.inject_fault()
        self.fault_info = self.injector.get_ground_truth()
        self.faulty_service = self.fault_info["faulty_service"]
        print(f"  Component: {self.fault_info['component']}")
        print(f"  Reason: {self.fault_info['reason']}")
        print(f"  Level: {self.fault_info['level']}")

    def recover_fault(self):
        """No-op recovery for static faults."""
        self.injector.recover_fault()

    def start_workload(self):
        """No workload needed for static replay."""
        print("== Workload: Static replay (no live workload) ==")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class OpenRCAStaticDetection(OpenRCAStaticBaseTask, DetectionTask):
    """Detection task for OpenRCA static replay.

    Agent must determine if an anomaly is present (always "Yes" for fault scenarios).
    """

    def __init__(self, config_file: str, scenario_id: int, dataset_type: str = "market"):
        OpenRCAStaticBaseTask.__init__(self, config_file, scenario_id, dataset_type)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        print("== Evaluation ==")
        expected_solution = "Yes"

        if isinstance(soln, str):
            if soln.strip().lower() == expected_solution.lower():
                print(f"Correct detection: {soln}")
                self.add_result("Detection Accuracy", "Correct")
            else:
                print(f"Incorrect detection: {soln}")
                self.add_result("Detection Accuracy", "Incorrect")
        else:
            print("Invalid solution format")
            self.add_result("Detection Accuracy", "Invalid Format")

        return super().eval(soln, trace, duration)


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------

class OpenRCAStaticLocalization(OpenRCAStaticBaseTask, LocalizationTask):
    """Localization task for OpenRCA static replay.

    Agent must identify the faulty component(s).
    Ground truth: the original component from record.csv.
    """

    def __init__(self, config_file: str, scenario_id: int, dataset_type: str = "market"):
        OpenRCAStaticBaseTask.__init__(self, config_file, scenario_id, dataset_type)
        LocalizationTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        print("== Evaluation ==")

        if soln is None:
            print("Solution is None")
            self.add_result("Localization Accuracy", 0.0)
            self.results["success"] = False
            self.results["is_subset"] = False
            super().eval(soln, trace, duration)
            return self.results

        # Normalize solution to list
        if isinstance(soln, str):
            soln = [soln]

        # Check against faulty component (original from dataset)
        target = self.fault_info["component"]
        target_service = self.faulty_service

        # Accept either exact component match or service name match
        is_exact = is_exact_match(soln, target) or is_exact_match(soln, target_service)
        is_sub = is_subset([target], soln) or is_subset([target_service], soln)

        if is_exact:
            accuracy = 100.0
            print(f"Exact match: {soln} | Accuracy: {accuracy}%")
        elif is_sub:
            accuracy = (1 / len(soln)) * 100.0
            print(f"Subset match: {soln} | Accuracy: {accuracy:.2f}%")
        else:
            accuracy = 0.0
            print(f"No match: {soln} | Expected: {target} or {target_service} | Accuracy: {accuracy}%")

        self.add_result("Localization Accuracy", accuracy)
        super().eval(soln, trace, duration)

        self.results["success"] = is_exact or (is_sub and len(soln) == 1)
        self.results["is_subset"] = is_sub
        self.results["expected_component"] = target
        self.results["expected_service"] = target_service

        return self.results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

class OpenRCAStaticAnalysis(OpenRCAStaticBaseTask, AnalysisTask):
    """Analysis task for OpenRCA static replay.

    Agent must identify the root cause using original dataset fields:
        - component: the faulty component (e.g., "shippingservice-1", "docker_003")
        - reason: the fault reason (e.g., "container read I/O load", "CPU fault")

    Expected submission format:
        submit({"component": "...", "reason": "..."})
    """

    def __init__(self, config_file: str, scenario_id: int, dataset_type: str = "market"):
        OpenRCAStaticBaseTask.__init__(self, config_file, scenario_id, dataset_type)
        AnalysisTask.__init__(self, self.app)

        # Override task description to use original root cause fields
        self.task_desc = """\
            You are an expert DevOps engineer assigned to do root cause analysis in a deployed service.

            Service Details:
            {app_summary}

            You will begin by analyzing the service's state and telemetry, and then submit your analysis:
            1. dict[str, str]: A dictionary with two keys: 'component' and 'reason'.
                - component: The root cause component where the fault originated
                    (e.g., a service name, node name, or container name)
                - reason: The root cause reason describing what went wrong
                    (e.g., "container CPU load", "network latency", "high memory usage")

            2. str: `None` if no faults were detected
            """

        self.instructions = """\
            You will respond with one of the above APIs as your next action.
            Please respond in the following format in a markdown code block:
            ```\\n<API_NAME>(<API_PARAM1>, <API_PARAM2> ...)\\n```

            For instance, if you want to list files in current directory, your response must be exactly:

            ```\\nexec_shell("ls -l")\\n```

            When submitting your analysis, use the following format:

            ```\\nsubmit({{"component": "your_component", "reason": "your_reason"}})\\n```

            Replace "your_component" and "your_reason" with the actual root cause component and reason.

            Or, if no fault is detected, you should respond with:

            ```\\nsubmit()\\n```

            Please respond with only a single API call (a.k.a., action) per turn without any additional words, labels, or prefixes.
            """

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        print("== Evaluation ==")

        expected_component = self.fault_info["component"]
        expected_reason = self.fault_info["reason"]

        if isinstance(soln, dict):
            provided_component = soln.get("component", "").strip()
            provided_reason = soln.get("reason", "").strip()

            # Component check: accept exact match or service name match
            is_component_correct = (
                provided_component.lower() == expected_component.lower()
                or provided_component.lower() == self.faulty_service.lower()
            )

            # Reason check: case-insensitive match
            is_reason_correct = (
                provided_reason.lower() == expected_reason.lower()
            )

            self.results["component_correct"] = is_component_correct
            self.results["reason_correct"] = is_reason_correct
            self.results["success"] = is_component_correct and is_reason_correct
            self.results["expected_component"] = expected_component
            self.results["expected_reason"] = expected_reason

            print(f"  Component: {'correct' if is_component_correct else 'incorrect'} "
                  f"(expected: {expected_component}, got: {provided_component})")
            print(f"  Reason: {'correct' if is_reason_correct else 'incorrect'} "
                  f"(expected: {expected_reason}, got: {provided_reason})")
        else:
            print("Error: soln is not a dictionary. Expected: {'component': '...', 'reason': '...'}")
            self.results["component_correct"] = False
            self.results["reason_correct"] = False
            self.results["success"] = False

        super().eval(soln, trace, duration)

        return self.results
