"""Base orchestrator class with common methods shared by all orchestrator variants."""

from aiopslab.session import Session
from aiopslab.orchestrator.parser import ResponseParser
from aiopslab.utils.status import *
from aiopslab.utils.critical_section import CriticalSection
import time
import inspect
import asyncio
import atexit
import os


def exit_cleanup_fault(prob):
    print("Recovering fault before exit...")
    prob.recover_fault()


class BaseOrchestrator:
    """Base class for orchestrators. Contains agent-environment loop and session management."""

    def __init__(self, results_dir=None, eval_id=None):
        self.agent = None
        self.session = None
        self.parser = ResponseParser()
        self.sprint = SessionPrint()
        self.execution_start_time = None
        self.execution_end_time = None
        self.use_wandb = os.getenv("USE_WANDB", "false").lower() == "true"
        self.results_dir = results_dir
        self.eval_id = eval_id

    def register_agent(self, agent, name="agent"):
        """Register the agent for the current session."""
        self.agent = agent
        self.agent_name = name
        # Get model name from agent if available
        if hasattr(agent, 'get_model_name'):
            self.model_name = agent.get_model_name()
        else:
            self.model_name = "model"

    async def ask_agent(self, input):
        """Ask the agent for the next action given the current context."""
        assert self.session is not None
        assert self.agent is not None

        agent_response = await self.agent.get_action(input)
        self.session.add({"role": "assistant", "content": agent_response})
        return agent_response

    async def ask_env(self, input):
        """Ask the environment for the observation given the current action."""
        assert self.session is not None

        try:
            resp = self.parser.parse(input)
        except ResponseParsingError as e:
            self.session.add({"role": "env", "content": str(e)})
            return str(e)

        api, args, kwargs = resp["api_name"], resp["args"], resp["kwargs"]

        if api == "submit":
            self.session.set_solution(args[0] if len(args) == 1 else args)

        try:
            env_response = self.session.problem.perform_action(api, *args, **kwargs)
            if hasattr(env_response, "error"):
                env_response = str(env_response)
                print("An error occurred:", env_response)
        except InvalidActionError as e:
            env_response = str(e)
        except Exception as e:
            env_response = str(e)
            print("Unhandled exception:", e)

        self.session.add({"role": "env", "content": env_response})
        return env_response

    def _setup_environment(self, prob, deployment):
        """Setup environment before running the problem. Override in subclasses."""
        raise NotImplementedError

    def _teardown_environment(self, prob):
        """Teardown environment after running the problem. Override in subclasses."""
        raise NotImplementedError

    def init_problem(self, problem_id: str):
        """Initialize a problem instance for the agent to solve."""
        self.execution_start_time = time.time()

        self.session = Session(results_dir=self.results_dir, eval_id=self.eval_id)
        print(f"Session ID: {self.session.session_id}")

        prob = self.probs.get_problem_instance(problem_id)
        deployment = self.probs.get_problem_deployment(problem_id)
        self.session.set_problem(prob, pid=problem_id)
        self.session.set_agent(self.agent_name)
        self.session.set_model(self.model_name)

        # Subclass-specific environment setup
        self._setup_environment(prob, deployment)

        # Deploy service
        prob.app.delete()
        prob.app.deploy()

        # Inject fault
        with CriticalSection():
            prob.inject_fault()
            atexit.register(exit_cleanup_fault, prob=prob)

        # Start workload
        if inspect.iscoroutinefunction(prob.start_workload):
            asyncio.create_task(prob.start_workload())
        else:
            prob.start_workload()

        task_desc = prob.get_task_description()
        instructions = prob.get_instructions()
        actions = prob.get_available_actions()

        self._problem_init_info = (task_desc, instructions, actions)

        return task_desc, instructions, actions

    async def start_problem(self, max_steps: int):
        """Start the task and run for a specified number of steps."""
        assert self.session is not None
        action_instr = "Please take the next action"
        action, env_response, results = "", "", {}
        self.session.start()

        # Initialize log file for session
        log_filepath = self.session.get_filepath(file_type="log")
        self.sprint.init_log_file(str(log_filepath))

        # Print problem initialization info
        if hasattr(self, '_problem_init_info'):
            task_desc, instructions, actions = self._problem_init_info
            self.sprint.problem_init(task_desc, instructions, actions)

        # Print full system prompt sent to the agent
        if hasattr(self, '_system_message'):
            self.sprint.system_prompt(self._system_message)

        self.sprint.step_count = 0

        try:
            for step in range(max_steps):
                action = await self.ask_agent(action_instr)
                self.sprint.agent(action)

                env_response = await self.ask_env(action)
                self.sprint.service(env_response)

                if env_response == SubmissionStatus.VALID_SUBMISSION:
                    break
                elif env_response == SubmissionStatus.INVALID_SUBMISSION:
                    raise ValueError("Invalid submission!")

                action_instr = env_response + "\n" + "Please take the next action"
        except Exception as e:
            with CriticalSection():
                print("Some exception happened. Recovering the injected fault...")
                self.session.problem.recover_fault()
                atexit.unregister(exit_cleanup_fault)
            raise e

        self.session.end()

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

        self.session.problem.app.cleanup()

        # Close log file
        self.sprint.close_log_file()

        # Subclass-specific teardown
        self._teardown_environment(self.session.problem)

        self.execution_end_time = time.time()
        total_execution_time = self.execution_end_time - self.execution_start_time
        time_keys = ["TTD", "TTL", "TTA", "TTM"]
        key = next((k for k in time_keys if k in results), None)
        framework_overhead = total_execution_time - results[key] if key else 0
        print(f"Framework overhead: {framework_overhead}")

        return {
            "history": self.session.history,
            "final_state": env_response,
            "results": results,
            "framework_overhead": framework_overhead,
        }
