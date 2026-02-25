"""OpenRCA unified task.

All 7 OpenRCA task types (task_1~task_7) use the same submit format
and evaluation logic. This replaces per-type Detection/Localization/Analysis.
"""

import json
import re
import textwrap
import inspect
from typing import Any

from aiopslab.orchestrator.tasks.base import Task
from aiopslab.service.apps.base import Application
from aiopslab.session import SessionItem
from aiopslab.utils.status import InvalidActionError
from aiopslab.orchestrator.evaluators.openrca_eval import (
    openrca_evaluate,
    get_task_difficulty,
)


def build_telemetry_guide(namespace: str, enabled_types=None) -> str:
    """Build the telemetry access guide based on which types are enabled.

    Args:
        namespace: AIOpsLab namespace string for example commands.
        enabled_types: frozenset of enabled type strings ("log", "metric", "trace"),
                       or None to include all three.
    """
    all_types = enabled_types is None

    fetch_parts, read_parts, examples = [], [], []

    if all_types or "log" in enabled_types:
        fetch_parts.append("get_logs")
        read_parts.append("read_logs")
        examples.append(f'get_logs("{namespace}") or get_logs("{namespace}", "<service>")')
    if all_types or "metric" in enabled_types:
        fetch_parts.append("get_metrics")
        read_parts.append("read_metrics")
        examples.append(f'get_metrics("{namespace}")')
    if all_types or "trace" in enabled_types:
        fetch_parts.append("get_traces")
        read_parts.append("read_traces")
        examples.append(f'get_traces("{namespace}")')

    fetch_cmds = "/".join(fetch_parts) if fetch_parts else "(none)"
    read_cmds = "/".join(read_parts) if read_parts else "(none)"
    examples_str = "\n  e.g., ".join(examples)

    lines = [
        "How to access telemetry data:",
        f"Step 1 - Fetch: Use {fetch_cmds} to save data locally.",
    ]
    if examples_str:
        lines.append(f"  e.g., {examples_str}")
    lines += [
        "Step 2 - Read or Filter:",
        f'  - {read_cmds}("<path>/file.csv") → returns full file contents',
        '  - exec_shell("grep <pattern> <path>/file.csv") → filtered results only',
        "",
        "Submit your root cause analysis as a JSON dict. Each root cause should be",
        'a numbered key ("1", "2", ...) with the relevant fields:',
        '- "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS"',
        '- "root cause component": "component_name"',
        '- "root cause reason": "fault_reason"',
        "Include only the fields requested in the task above.",
    ]
    return "\n".join(lines)


class OpenRCATask(Task):
    """Unified task for all OpenRCA task types (task_1~task_7)."""

    def __init__(self, app: Application, query_row: dict, task_type: str):
        """
        Args:
            app: StaticDataset instance.
            query_row: A row from query.csv as dict with keys:
                       instruction, scoring_points, task_index.
            task_type: "task_1" ~ "task_7".
        """
        super().__init__()
        self.app = app
        self.helm_configs = getattr(app, "helm_configs", {})
        self.instruction = query_row["instruction"]
        self.scoring_points = query_row["scoring_points"]
        self.task_type = task_type
        self.app_summary = self.app.get_app_summary()
        self.services = getattr(self.app, "get_services", lambda: [])()

        # Actions are set by the problem class that inherits this
        self.actions = None

        # Telemetry guide: built dynamically by default; can be overridden by agents
        self._telemetry_guide: str | None = None  # None → build dynamically
        self._guide_overridden = False

        services_str = ", ".join(self.services) if self.services else "unknown"

        self.task_desc = """\
            You are a DevOps engineer analyzing telemetry from a real production incident.

            Service Details:
            {app_summary}

            Namespace: {namespace}
            Available services/components: {services}

            {instruction}

            {telemetry_guide}
            """

        self._services_str = services_str

        self.instructions = """\
            Respond with API calls in the following format within markdown code blocks:
            ```\\n<API_NAME>(<PARAM1>, <PARAM2>, ...)\\n```

            When you have completed your analysis, submit your findings:
            ```\\nsubmit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", "root cause component": "component_name", "root cause reason": "reason"}}}})\n```

            Provide one API call per response.
            """

    @property
    def telemetry_guide(self) -> str:
        return self._telemetry_guide or build_telemetry_guide(self.app.namespace)

    @telemetry_guide.setter
    def telemetry_guide(self, value: str):
        self._telemetry_guide = value
        self._guide_overridden = True

    def get_task_description(self):
        # Build dynamically from enabled types unless an agent has set a custom guide
        if self._guide_overridden:
            guide = self._telemetry_guide
        else:
            enabled_types = getattr(self.actions, "enabled_telemetry_types", None)
            guide = build_telemetry_guide(self.app.namespace, enabled_types)

        return textwrap.dedent(self.task_desc).format(
            app_summary=self.app_summary,
            services=self._services_str,
            instruction=self.instruction,
            namespace=self.app.namespace,
            telemetry_guide=guide,
        )

    def get_instructions(self):
        return textwrap.dedent(self.instructions)

    def get_available_actions(self):
        if self.actions is None:
            return {}
        enabled_types = getattr(self.actions, "enabled_telemetry_types", None)
        result = {}
        for method in dir(self.actions):
            fn = getattr(self.actions, method)
            if not (callable(fn) and getattr(fn, "is_action", False)):
                continue
            telemetry_type = getattr(fn, "telemetry_type", None)
            required_types = getattr(fn, "required_telemetry_types", None)
            if enabled_types is not None:
                if required_types is not None:
                    # Action requires multiple telemetry types — all must be enabled
                    if not required_types.issubset(enabled_types):
                        continue
                elif telemetry_type is not None and telemetry_type not in enabled_types:
                    continue
            sig = inspect.signature(fn)
            doc = (fn.__doc__ or "").strip()
            result[method] = f"{sig}\n{doc}"
        return result

    def perform_action(self, action_name, *args, **kwargs):
        if self.actions is None:
            raise InvalidActionError(action_name)
        action_method = getattr(self.actions, action_name, None)
        if action_method is not None and callable(action_method):
            return action_method(*args, **kwargs)
        raise InvalidActionError(action_name)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        soln_str = json.dumps(soln) if not isinstance(soln, str) else soln
        passing, failing, score = openrca_evaluate(soln_str, self.scoring_points)
        self.add_result("score", score)
        self.add_result("passing_criteria", passing)
        self.add_result("failing_criteria", failing)
        self.add_result("ground_truth", self.scoring_points)
        query_info = getattr(self.app, "query_info", None)
        if query_info and query_info.faults:
            self.add_result("record", query_info.faults)
        self.add_result("task_type", self.task_type)
        self.add_result("difficulty", get_task_difficulty(self.task_type))
        self.add_result("TTA", duration)
        self.results["success"] = score == 1.0
        self.common_eval(trace)
        return self.results
