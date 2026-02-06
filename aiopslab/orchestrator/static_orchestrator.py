# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Static Dataset Orchestrator for pre-collected datasets like OpenRCA and AcmeTrace."""

import logging
import time
import os

from aiopslab.session import Session
from aiopslab.orchestrator.base_orchestrator import BaseOrchestrator
from aiopslab.orchestrator.problems.openrca_registry import OpenRCAProblemRegistry
from aiopslab.orchestrator.problems.acmetrace_registry import AcmeTraceProblemRegistry
from aiopslab.orchestrator.parser import ResponseParser, ACMETRACE_EXAMPLES
from aiopslab.utils.status import SubmissionStatus


class StaticDatasetOrchestrator(BaseOrchestrator):
    """Orchestrator for static datasets (OpenRCA, AcmeTrace, etc.).

    Unlike the K8s-based Orchestrator, this class:
    - Does not deploy/manage infrastructure
    - Loads pre-collected CSV data
    - Does not need fault injection/recovery

    Supported datasets:
    - openrca: OpenRCA Bank/Market/Telecom RCA tasks
    - acmetrace: AcmeTrace Kalos GPU cluster RCA tasks
    """

    def __init__(self, results_dir=None, dataset: str = "openrca", verbose: bool = False):
        """Initialize the orchestrator.

        Args:
            results_dir: Directory to save results
            dataset: Dataset to use ("openrca" or "acmetrace")
            verbose: If True, print full actions and responses
        """
        super().__init__(results_dir)
        self.dataset = dataset.lower()
        self.verbose = verbose

        if self.dataset == "openrca":
            self.probs = OpenRCAProblemRegistry()
        elif self.dataset == "acmetrace":
            self.probs = AcmeTraceProblemRegistry()
            # Use AcmeTrace-specific parser examples
            self.parser = ResponseParser(examples=ACMETRACE_EXAMPLES)
        else:
            raise ValueError(f"Unknown dataset: {dataset}. Supported: openrca, acmetrace")

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
            print("\n" + "=" * 60)
            print(f"  STEP {step + 1}/{max_steps}")
            print("=" * 60)

            action = await self.ask_agent(action_instr)

            print("\n[AGENT RESPONSE]")
            print("-" * 60)
            print(action)
            print("-" * 60)

            # Show parsed API call
            try:
                parsed = self.parser.parse(action)
                api_name = parsed["api_name"]
                args = parsed["args"]
                kwargs = parsed["kwargs"]
                context = parsed["context"]
                print(f"\n[PARSED] API: {api_name}")
                if args:
                    print(f"[PARSED] Args: {args}")
                if kwargs:
                    print(f"[PARSED] Kwargs: {kwargs}")
                if context:
                    print(f"[PARSED] Agent reasoning: {context}")
            except Exception as e:
                print(f"\n[PARSE ERROR] {e}")

            env_response = await self.ask_env(action)

            print(f"\n[ENV RESPONSE]")
            print("-" * 60)
            resp_str = str(env_response)
            print(resp_str[:2000] + "..." if len(resp_str) > 2000 else resp_str)
            print("-" * 60)

            if env_response == SubmissionStatus.VALID_SUBMISSION:
                print(f"\n>>> VALID SUBMISSION at step {step + 1} <<<")
                break
            elif env_response == SubmissionStatus.INVALID_SUBMISSION:
                print(f"\n>>> INVALID SUBMISSION at step {step + 1} <<<")
                raise ValueError("Invalid submission!")

            action_instr = str(env_response) + "\n" + "Please take the next action"

        self.session.end()

        # Evaluate the submission
        if env_response != SubmissionStatus.INVALID_SUBMISSION:
            print("\n" + "=" * 60)
            print("  EVALUATION")
            print("=" * 60)
            print(f"[EVAL] Solution submitted: {self.session.solution}")
            results = self.session.problem.eval(
                self.session.solution, self.session.history, self.session.get_duration()
            )
            print(f"[EVAL] Results:")
            for k, v in results.items():
                print(f"  {k}: {v}")
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
