"""OpenRCA unified task.

All 7 OpenRCA task types (task_1~task_7) use the same submit format
and evaluation logic. This replaces per-type Detection/Localization/Analysis.
"""

import textwrap
from typing import Any

from aiopslab.orchestrator.tasks.base import Task
from aiopslab.service.apps.base import Application
from aiopslab.session import SessionItem
from aiopslab.utils.status import InvalidActionError
from aiopslab.orchestrator.evaluators.openrca_eval import (
    openrca_evaluate,
    get_task_difficulty,
)


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

        services_str = ", ".join(self.services) if self.services else "unknown"

        self.task_desc = """\
            You are a DevOps engineer analyzing telemetry from a real production incident.

            Service Details:
            {app_summary}

            Namespace: {namespace}
            Available services/components: {services}

            {instruction}

            Your workspace is /agent/. Telemetry data is at /agent/telemetry/{namespace}/:
              - logs/     : log CSV files
              - metrics/  : metric CSV files
              - traces/   : trace CSV files
            Use the provided APIs or exec_shell to explore these files.

            IMPORTANT: When calling APIs, use namespace="{namespace}".
            For get_logs(), you can optionally filter by service name (e.g., get_logs("{namespace}", "Mysql02")).

            Submit your root cause analysis as a JSON dict. Each root cause should be
            a numbered key ("1", "2", ...) with the relevant fields:
            - "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS"
            - "root cause component": "component_name"
            - "root cause reason": "fault_reason"
            Include only the fields requested in the task above.
            """

        self._services_str = services_str

        self.instructions = """\
            Respond with API calls in the following format within markdown code blocks:
            ```\\n<API_NAME>(<PARAM1>, <PARAM2>, ...)\\n```

            When you have completed your analysis, submit your findings:
            ```\\nsubmit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", "root cause component": "component_name", "root cause reason": "reason"}}}})\n```

            Provide one API call per response.
            """

    def get_task_description(self):
        return textwrap.dedent(self.task_desc).format(
            app_summary=self.app_summary,
            services=self._services_str,
            instruction=self.instruction,
            namespace=self.app.namespace,
        )

    def get_instructions(self):
        return textwrap.dedent(self.instructions)

    def get_available_actions(self):
        if self.actions is None:
            return {}
        return {
            method: getattr(self.actions, method).__doc__.strip()
            for method in dir(self.actions)
            if callable(getattr(self.actions, method))
            and getattr(getattr(self.actions, method), "is_action", False)
        }

    def perform_action(self, action_name, *args, **kwargs):
        if self.actions is None:
            raise InvalidActionError(action_name)
        action_method = getattr(self.actions, action_name, None)
        if action_method is not None and callable(action_method):
            return action_method(*args, **kwargs)
        raise InvalidActionError(action_name)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        passing, failing, score = openrca_evaluate(str(soln), self.scoring_points)
        self.add_result("score", score)
        self.add_result("passing_criteria", passing)
        self.add_result("failing_criteria", failing)
        self.add_result("task_type", self.task_type)
        self.add_result("difficulty", get_task_difficulty(self.task_type))
        self.add_result("TTA", duration)
        self.results["success"] = score == 1.0
        self.common_eval(trace)
        return self.results
