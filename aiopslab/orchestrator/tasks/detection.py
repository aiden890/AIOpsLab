# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Define and query information about an AIOps Detection task."""

import textwrap
from typing import Any


from aiopslab.orchestrator.tasks.base import Task
from aiopslab.orchestrator.actions.detection import DetectionActions
from aiopslab.service.apps.base import Application
from aiopslab.session import SessionItem
from aiopslab.utils.actions import get_actions
from aiopslab.utils.status import InvalidActionError


class DetectionTask(Task):
    """An AIOps anomaly detection task."""

    def __init__(self, app: Application):
        super().__init__()
        self.app = app
        self.helm_configs = self.app.helm_configs
        self.app_summary = self.app.get_app_summary()
        self.actions = DetectionActions()

        self.task_desc = """\
            You are a DevOps engineer analyzing a service for potential anomalies.

            Service Details:
            {app_summary}

            Your task is to analyze the service's state and telemetry, then provide your assessment:
            - Response: "Yes" if anomalies are detected
            - Response: "No" if the service appears healthy
            """

        self.instructions = """\
            Respond with API calls in the following format within markdown code blocks:
            ```\n<API_NAME>(<PARAM1>, <PARAM2>, ...)\n```

            When you have completed your analysis, submit your findings:
            - If no anomalies were detected: ```\nsubmit(\"No\")\n```
            - If anomalies were found: ```\nsubmit(\"Yes\")\n```

            Provide one API call per response.
            """

    def get_task_description(self):
        return textwrap.dedent(self.task_desc).format(app_summary=self.app_summary)

    def get_instructions(self):
        return textwrap.dedent(self.instructions)

    def get_available_actions(self):
        return get_actions(task="detection")

    def perform_action(self, action_name, *args, **kwargs):
        action_method = getattr(self.actions, action_name, None)

        if action_method is not None and callable(action_method):
            return action_method(*args, **kwargs)
        else:
            raise InvalidActionError(action_name)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        self.add_result("TTD", duration)
        self.common_eval(trace)
        return self.results
