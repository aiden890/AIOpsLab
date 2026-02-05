# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Static Dataset Orchestrator for pre-collected datasets like OpenRCA."""

import logging
import time
import os

from aiopslab.session import Session
from aiopslab.orchestrator.base_orchestrator import BaseOrchestrator
from aiopslab.orchestrator.problems.openrca_registry import OpenRCAProblemRegistry
from aiopslab.utils.status import SubmissionStatus


class StaticDatasetOrchestrator(BaseOrchestrator):
    """Orchestrator for static datasets (OpenRCA, etc.).

    Unlike the K8s-based Orchestrator, this class:
    - Does not deploy/manage infrastructure
    - Loads pre-collected CSV data
    - Does not need fault injection/recovery
    """

    def __init__(self, results_dir=None):
        super().__init__(results_dir)
        self.probs = OpenRCAProblemRegistry()
        self.use_wandb = os.getenv("USE_WANDB", "false").lower() == "true"

    def init_problem(self, problem_id: str):
        """Initialize a problem instance from static dataset.

        Args:
            problem_id (str): Format: "openrca-{domain}-{task_index}-{query_id}"
                              Example: "openrca-bank-task_3-5"

        Returns:
            tuple: (task_description, instructions, available_actions)
        """
        self.execution_start_time = time.time()

        self.session = Session(results_dir=self.results_dir)
        print(f"Session ID: {self.session.session_id}")

        prob = self.probs.get_problem_instance(problem_id)
        self.session.set_problem(prob, pid=problem_id)
        self.session.set_agent(self.agent_name)

        task_desc = prob.get_task_description()
        instructions = prob.get_instructions()
        actions = prob.get_available_actions()

        return task_desc, instructions, actions

    async def start_problem(self, max_steps: int):
        """Start the task and run for a specified number of steps.

        Args:
            max_steps (int): The maximum number of steps to run the task.

        Returns:
            dict: The final state of the session including results.
        """
        assert self.session is not None, "Call init_problem() first"

        action_instr = "Please take the next action"
        action, env_response, results = "", "", {}
        self.session.start()

        logging.info(f"Starting problem: {self.session.pid}")

        for step in range(max_steps):
            logging.info(f"Step {step + 1}/{max_steps}")

            action = await self.ask_agent(action_instr)
            logging.debug(f"Agent action: {action[:200]}..." if len(action) > 200 else f"Agent action: {action}")

            env_response = await self.ask_env(action)
            logging.debug(f"Env response: {str(env_response)[:200]}..." if len(str(env_response)) > 200 else f"Env response: {env_response}")

            if env_response == SubmissionStatus.VALID_SUBMISSION:
                logging.info(f"Valid submission received at step {step + 1}")
                break
            elif env_response == SubmissionStatus.INVALID_SUBMISSION:
                logging.warning("Invalid submission received")
                raise ValueError("Invalid submission!")

            action_instr = str(env_response) + "\n" + "Please take the next action"

        self.session.end()

        # Evaluate the submission
        if env_response != SubmissionStatus.INVALID_SUBMISSION:
            results = self.session.problem.eval(
                self.session.solution, self.session.history, self.session.get_duration()
            )
            logging.info(f"Evaluation results: {results}")

        self.session.set_results(results)
        self.session.to_json()

        if self.use_wandb:
            self.session.to_wandb()

        self.execution_end_time = time.time()
        total_execution_time = self.execution_end_time - self.execution_start_time

        return {
            "history": self.session.history,
            "final_state": env_response,
            "results": results,
            "total_time": total_execution_time,
        }
