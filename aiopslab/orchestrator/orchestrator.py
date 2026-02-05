# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Orchestrator class that interfaces with the agent and the environment."""

from aiopslab.service.helm import Helm
from aiopslab.service.kubectl import KubeCtl
from aiopslab.session import Session
from aiopslab.orchestrator.problems.registry import ProblemRegistry
from aiopslab.orchestrator.base_orchestrator import BaseOrchestrator
from aiopslab.utils.status import *
from aiopslab.utils.critical_section import CriticalSection
from aiopslab.service.telemetry.prometheus import Prometheus
import time
import inspect
import asyncio
import atexit
import os


class Orchestrator(BaseOrchestrator):
    def __init__(self, results_dir=None):
        super().__init__(results_dir)
        self.probs = ProblemRegistry()
        self.sprint = SessionPrint()
        self.execution_start_time = None
        self.execution_end_time = None
        self.kubectl = KubeCtl()
        self.use_wandb = os.getenv("USE_WANDB", "false").lower() == "true"

    def init_problem(self, problem_id: str):
        """Initialize a problem instance for the agent to solve.

        Args:
            problem_id (str): The problem instance identifier.

        Returns:
            tuple: A tuple containing the problem description, task message, and session object.
        """
        # Start timer
        self.execution_start_time = time.time()

        self.session = Session(results_dir=self.results_dir)
        print(f"Session ID: {self.session.session_id}")
        prob = self.probs.get_problem_instance(problem_id)
        deployment = self.probs.get_problem_deployment(problem_id)
        self.session.set_problem(prob, pid=problem_id)
        self.session.set_agent(self.agent_name)

        if deployment != "docker":
            print("Setting up OpenEBS...")

            # Install OpenEBS
            self.kubectl.exec_command(
                "kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml"
            )
            self.kubectl.exec_command(
                "kubectl patch storageclass openebs-hostpath -p '{\"metadata\": {\"annotations\":{\"storageclass.kubernetes.io/is-default-class\":\"true\"}}}'"
            )
            self.kubectl.wait_for_ready("openebs")
            print("OpenEBS setup completed.")

            # Setup and deploy Prometheus
            self.prometheus = Prometheus()
            self.prometheus.deploy()

        # deploy service
        prob.app.delete()
        prob.app.deploy()

        # make sure is_fault_injected is correct to apply appropriate
        # function with atexit to recover fault
        with CriticalSection():
            # inject fault
            prob.inject_fault()
            atexit.register(exit_cleanup_fault, prob=prob)

        # Check if start_workload is async or sync
        if inspect.iscoroutinefunction(prob.start_workload):
            asyncio.create_task(prob.start_workload())
        else:
            prob.start_workload()

        task_desc = prob.get_task_description()
        instructions = prob.get_instructions()
        actions = prob.get_available_actions()

        return task_desc, instructions, actions

    async def start_problem(self, max_steps: int):
        """Start the task and run for a specified number of steps.

        Args:
            max_steps (int): The maximum number of steps to run the task.

        Returns:
            dict: The final state of the session.
        """
        assert self.session is not None
        action_instr = "Please take the next action"
        action, env_response, results = "", "", {}
        self.session.start()

        # catch any exception and recover fault before the users catch it
        try:
            for step in range(max_steps):
                action = await self.ask_agent(action_instr)
                self.sprint.agent(action)

                env_response = await self.ask_env(action)
                self.sprint.service(env_response)

                if env_response == SubmissionStatus.VALID_SUBMISSION:
                    break
                elif env_response == SubmissionStatus.INVALID_SUBMISSION:
                    raise ValueError("Invalid submission!")  # TODO (@manish): ask to retry?

                action_instr = env_response + "\n" + "Please take the next action"
        except Exception as e:
            # Make sure the fault cleanup function is unregistered
            # after recovering fault ahead because of exceptions
            with CriticalSection():
                print("Some exception happened. Recovering the injected fault...")
                self.session.problem.recover_fault()
                atexit.unregister(exit_cleanup_fault)
            raise e

        self.session.end()

        # A valid submission was made (or) max_steps reached
        if env_response != SubmissionStatus.INVALID_SUBMISSION:
            results = self.session.problem.eval(
                self.session.solution, self.session.history, self.session.get_duration()
            )
            self.sprint.result(results)

        self.session.set_results(results)
        self.session.to_json()
        if self.use_wandb:
            self.session.to_wandb()

        with CriticalSection():
            self.session.problem.recover_fault()
            atexit.unregister(exit_cleanup_fault)
            
        # Beyond recovering from fault,
        # I feel sometimes it is safer to delete the whole namespace.
        # But this will take more time.
        # if not self.session.problem.sys_status_after_recovery():
        self.session.problem.app.cleanup()
        
        if self.session.problem.namespace != "docker":
            self.prometheus.teardown()
            print("Uninstalling OpenEBS...")
            self.kubectl.exec_command("kubectl delete sc openebs-hostpath openebs-device --ignore-not-found")
            self.kubectl.exec_command(
                "kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml"
            )
            self.kubectl.wait_for_namespace_deletion("openebs")

        self.execution_end_time = time.time()
        total_execution_time = self.execution_end_time - self.execution_start_time
        time_keys = ["TTD", "TTL", "TTA", "TTM"]
        key = next((k for k in time_keys if k in results), None)
        framework_overhead = (
            total_execution_time - results[key]
        )  # Time spent doing everything besides running the agent
        print(f"Framework overhead: {framework_overhead}")

        return {
            "history": self.session.history,
            "final_state": env_response,
            "results": results,
            "framework_overhead": framework_overhead,
        }


def exit_cleanup_fault(prob):
    print("Recovering fault before exit...")
    prob.recover_fault()
