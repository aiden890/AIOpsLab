# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Base Orchestrator class with common logic for agent communication and session management."""

from aiopslab.session import Session
from aiopslab.orchestrator.parser import ResponseParser
from aiopslab.utils.status import InvalidActionError, ResponseParsingError


class BaseOrchestrator:
    """Base class with common orchestrator logic.

    Provides:
    - Agent registration and communication (ask_agent)
    - Environment interaction (ask_env)
    - Session management

    Subclasses must implement:
    - init_problem(): Setup problem instance
    - start_problem(): Main execution loop
    """

    def __init__(self, results_dir=None):
        self.agent = None
        self.agent_name = None
        self.session = None
        self.parser = ResponseParser()
        self.results_dir = results_dir

    def register_agent(self, agent, name="agent"):
        """Register the agent for the current session.

        Args:
            agent: The agent to register.
            name: The name of the agent (default: "agent").
        """
        self.agent = agent
        self.agent_name = name

    async def ask_agent(self, input):
        """Ask the agent for the next action given the current context.

        Args:
            input: The input/feedback from the environment.

        Returns:
            str: The agent's response (action).
        """
        assert self.session is not None, "Session not initialized. Call init_problem first."
        assert self.agent is not None, "Agent not registered. Call register_agent first."

        agent_response = await self.agent.get_action(input)
        self.session.add({"role": "assistant", "content": agent_response})

        return agent_response

    async def ask_env(self, input):
        """Ask the environment for the observation given the current action.

        Args:
            input: The agent's action string to parse and execute.

        Returns:
            str: The environment's response.
        """
        assert self.session is not None, "Session not initialized. Call init_problem first."

        try:
            resp = self.parser.parse(input)
        except ResponseParsingError as e:
            self.session.add({"role": "env", "content": str(e)})
            return str(e)

        api, args, kwargs = resp["api_name"], resp["args"], resp["kwargs"]

        # if submit, save solution for eval
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

    def init_problem(self, problem_id: str):
        """Initialize a problem instance for the agent to solve.

        Must be implemented by subclasses.

        Args:
            problem_id (str): The problem instance identifier.

        Returns:
            tuple: (task_description, instructions, available_actions)
        """
        raise NotImplementedError("Subclasses must implement init_problem()")

    async def start_problem(self, max_steps: int):
        """Start the task and run for a specified number of steps.

        Must be implemented by subclasses.

        Args:
            max_steps (int): The maximum number of steps to run the task.

        Returns:
            dict: The final state of the session.
        """
        raise NotImplementedError("Subclasses must implement start_problem()")
