# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Static Orchestrator for managing static log replayer experiments.

Manages the lifecycle of static RCA experiments: problem initialization,
replayer Docker management, agent interaction, solution evaluation, and cleanup.
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path

from aiopslab.utils.logging_config import setup_logging
from aiopslab.utils.results_writer import ResultsWriter

logger = logging.getLogger(__name__)


class Session:
    """
    Represents a single static RCA experiment session.

    Tracks session metadata, results directory, and interaction trace.
    """

    def __init__(self, problem_id: str, results_base_dir: Path):
        """
        Initialize a new session.

        Args:
            problem_id: Unique identifier for the problem (e.g., 'openrca_bank_task1')
            results_base_dir: Base directory for all results
        """
        self.session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.problem_id = problem_id
        self.start_time = time.time()
        self.end_time: Optional[float] = None

        # Create session directory
        # Structure: results_base_dir/problem_id/session_id/
        self.results_dir = results_base_dir / problem_id / self.session_id
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Interaction trace
        self.trace: list[Dict[str, Any]] = []

        logger.info(f"Session created: {self.session_id}")
        logger.info(f"Results directory: {self.results_dir}")

    def add_interaction(self, role: str, content: str, **kwargs):
        """
        Add an interaction to the session trace.

        Args:
            role: 'agent', 'environment', or 'system'
            content: Message content
            **kwargs: Additional metadata
        """
        self.trace.append({
            'timestamp': time.time(),
            'role': role,
            'content': content,
            **kwargs
        })

    def get_duration(self) -> float:
        """Get session duration in seconds."""
        if self.end_time:
            return self.end_time - self.start_time
        return time.time() - self.start_time

    def complete(self):
        """Mark session as complete."""
        self.end_time = time.time()
        logger.info(f"Session completed in {self.get_duration():.2f}s")


class StaticOrchestrator:
    """
    Orchestrator for static log replayer experiments.

    Manages the full lifecycle of static RCA experiments:
    - Problem initialization and replayer startup
    - Agent interaction coordination
    - Solution evaluation
    - Results persistence
    - Cleanup
    """

    def __init__(
        self,
        results_base_dir: str = "results/static_rca",
        log_dir: Optional[str] = None
    ):
        """
        Initialize static orchestrator.

        Args:
            results_base_dir: Base directory for experiment results
            log_dir: Optional directory for log files
        """
        self.results_base_dir = Path(results_base_dir)
        self.results_base_dir.mkdir(parents=True, exist_ok=True)

        self.log_dir = Path(log_dir) if log_dir else None
        self.session: Optional[Session] = None
        self.problem: Optional[Any] = None
        self.results_writer: Optional[ResultsWriter] = None
        self.agent: Optional[Any] = None
        self.agent_name: str = "agent"

        logger.info("StaticOrchestrator initialized")
        logger.info(f"Results base directory: {self.results_base_dir}")

    def register_agent(self, agent, name="agent"):
        """
        Register the agent for the current session.

        Args:
            agent: The agent to register
            name: The name of the agent (default: "agent")
        """
        self.agent = agent
        self.agent_name = name
        logger.info(f"Agent registered: {name}")

    async def start_problem(self, max_steps: int = 30):
        """
        Start the problem-solving loop with the registered agent.

        Args:
            max_steps: Maximum number of interaction steps

        Returns:
            Dictionary with results and final solution
        """
        if not self.agent:
            raise RuntimeError("No agent registered. Call register_agent() first.")

        if not self.problem or not self.session:
            raise RuntimeError("Problem not initialized. Call init_static_problem() first.")

        logger.info("=" * 80)
        logger.info(f"STARTING PROBLEM SOLVING (max_steps={max_steps})")
        logger.info("=" * 80)

        observation = "Begin your RCA investigation."
        solution = None

        for step in range(max_steps):
            logger.info(f"\n--- Step {step + 1}/{max_steps} ---")

            # Get action from agent
            agent_response = await self.agent.get_action(observation)
            logger.debug(f"Agent response: {agent_response}")

            # Record agent response
            self.session.add_interaction('agent', agent_response)

            # Parse action from agent response
            action = self._parse_action(agent_response)

            # Check if agent wants to submit solution
            if action and action.get('name') == 'submit_solution':
                logger.info("Agent submitting solution")
                # Try to get solution from action args first
                solution = action.get('args', {}).get('solution')
                if not solution:
                    # If not in args, try to parse JSON from the response
                    solution = self._parse_solution(agent_response)
                break
            elif "submit" in agent_response.lower() and ("root_cause" in agent_response.lower() or "{" in agent_response):
                # Agent mentioned submit and has JSON-like content
                logger.info("Agent submitting solution (detected from response)")
                solution = self._parse_solution(agent_response)
                break

            # Execute action
            if action:
                logger.info(f"Executing action: {action['name']}")
                step_result = self.step(agent_response, action=action)
            else:
                # No action parsed, just acknowledge
                step_result = self.step(agent_response, action=None)

            observation = str(step_result['observation'])

        if solution is None:
            logger.warning("No solution submitted, using empty solution")
            solution = {}

        # Evaluate solution
        eval_result = self.submit_solution(solution)

        return {
            'results': eval_result,
            'solution': solution,
            'num_steps': step + 1
        }

    def _parse_action(self, agent_response: str) -> Optional[Dict[str, Any]]:
        """
        Parse action from agent response.

        Handles formats:
            Action: action_name(arg1="value1", arg2="value2")
            Action: action_name("value1", "value2")
            Action: action_name(start_time="...", end_time="...")

        Args:
            agent_response: Agent's response text

        Returns:
            Dictionary with 'name' and 'args', or None if no action found
        """
        import re

        # Look for "Action: function_name(...)"
        action_pattern = r'Action:\s*(\w+)\s*\((.*?)\)(?:\s|$)'
        match = re.search(action_pattern, agent_response, re.DOTALL)

        if not match:
            return None

        action_name = match.group(1)
        args_str = match.group(2).strip()

        # Parse arguments
        args = {}
        if args_str:
            # Try keyword argument format: key="value" or key='value'
            keyword_pattern = r'(\w+)\s*=\s*["\']([^"\']*)["\']'
            keyword_matches = list(re.finditer(keyword_pattern, args_str))

            if keyword_matches:
                # Keyword arguments found
                for arg_match in keyword_matches:
                    key, value = arg_match.groups()
                    args[key] = value
            else:
                # Try positional arguments: "value1", "value2"
                positional_pattern = r'["\']([^"\']+)["\']'
                positional_matches = re.findall(positional_pattern, args_str)

                if positional_matches:
                    # Map to expected parameter names based on action
                    if action_name in ['query_static_traces', 'query_static_logs', 'query_static_metrics']:
                        if len(positional_matches) >= 2:
                            args['start_time'] = positional_matches[0]
                            args['end_time'] = positional_matches[1]
                        if len(positional_matches) >= 3:
                            args['cmdb_id'] = positional_matches[2]
                        if len(positional_matches) >= 4 and action_name == 'query_static_logs':
                            args['keyword'] = positional_matches[3]

        logger.debug(f"Parsed action: {action_name} with args: {args}")
        return {'name': action_name, 'args': args}

    def _parse_solution(self, agent_response: str) -> Dict[str, Any]:
        """
        Parse solution from agent response.

        Handles various formats:
        - JSON object: {"root_cause_time": "..."}
        - Code block: ```json {...} ```
        - Inline: solution={"key": "value"}

        Args:
            agent_response: Agent's response text

        Returns:
            Solution dictionary
        """
        import re
        import json

        # Try to find JSON in markdown code block first
        code_block_match = re.search(r'```(?:json)?\s*(\{[^`]+\})\s*```', agent_response, re.DOTALL)
        if code_block_match:
            try:
                return json.loads(code_block_match.group(1))
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse code block JSON: {e}")

        # Try to find JSON object (handles nested braces better)
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', agent_response)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse JSON: {e}")

        # Try to find solution={"key": "value"} format
        solution_match = re.search(r'solution\s*=\s*(\{[^}]+\})', agent_response)
        if solution_match:
            try:
                return json.loads(solution_match.group(1))
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse solution parameter: {e}")

        logger.warning("Could not parse solution from agent response")
        logger.debug(f"Response was: {agent_response[:500]}")
        return {}

    def init_static_problem(
        self,
        problem_id: str,
        agent_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Initialize a static problem and start the replayer.

        Args:
            problem_id: Problem identifier (e.g., 'openrca_bank_task1')
            agent_config: Optional agent configuration

        Returns:
            Dictionary with:
                - task_description: Problem description for agent
                - session_id: Unique session identifier
                - problem_config: Problem configuration

        Raises:
            ValueError: If problem_id is invalid
            TimeoutError: If replayer doesn't start within timeout
        """
        logger.info("=" * 80)
        logger.info(f"INITIALIZING STATIC PROBLEM: {problem_id}")
        logger.info("=" * 80)

        # Create session
        self.session = Session(problem_id, self.results_base_dir)

        # Setup logging for this session
        if self.log_dir:
            log_file = self.log_dir / f"{self.session.session_id}.log"
            setup_logging(log_file=log_file, session_id=self.session.session_id)

        try:
            # Load problem from registry
            from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
            registry = StaticProblemRegistry()

            self.problem = registry.get_problem_instance(problem_id)
            logger.info(f"✓ Problem loaded: {problem_id}")

            # Start replayer and wait for ready
            logger.info("Starting replayer...")
            self.problem.start_replayer(timeout=300)
            logger.info("✓ Replayer ready")

            # Get task description
            task_description = self.problem.get_task_description()
            logger.info("✓ Task description generated")

            # Initialize results writer
            self.results_writer = ResultsWriter(
                self.session.results_dir,
                self.results_base_dir / "experiments.csv"
            )

            # Record initialization
            self.session.add_interaction(
                'system',
                f'Problem initialized: {problem_id}',
                problem_id=problem_id,
                session_id=self.session.session_id
            )

            # Get available actions/APIs using decorator pattern
            from aiopslab.orchestrator.actions.static_actions import StaticActions

            # Extract all methods marked with @action decorator
            apis_dict = {}
            for method_name in dir(StaticActions):
                method = getattr(StaticActions, method_name)
                if callable(method) and getattr(method, 'is_action', False):
                    # Get docstring which includes signature and description
                    docstring = method.__doc__.strip() if method.__doc__ else f"{method_name}()"
                    apis_dict[method_name] = docstring

            # Add submit_solution manually (not part of StaticActions class)
            apis_dict['submit_solution'] = """submit_solution(solution)

Submit your root cause analysis solution.

Args:
    solution: Dictionary with root cause findings (format varies by task)

Returns:
    Evaluation results"""

            # Get instructions
            instructions = self.problem.get_instructions()

            logger.info("=" * 80)
            logger.info("✓ PROBLEM INITIALIZED")
            logger.info("=" * 80)

            # Return in both formats for compatibility
            self._init_result = {
                'task_description': task_description,
                'session_id': self.session.session_id,
                'problem_config': self.problem.get_config() if hasattr(self.problem, 'get_config') else {},
                'instructions': instructions,
                'apis': apis_dict
            }

            return task_description, instructions, apis_dict

        except Exception as e:
            logger.error(f"Failed to initialize problem: {e}", exc_info=True)
            self._cleanup_after_problem()
            raise

    def step(
        self,
        agent_response: str,
        action: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Execute one step of agent interaction.

        Args:
            agent_response: Agent's response message
            action: Optional action to execute
                   Format: {'name': 'query_static_traces', 'args': {...}}

        Returns:
            Dictionary with:
                - observation: Environment observation (action result or acknowledgment)
                - done: Whether the episode is complete

        Raises:
            RuntimeError: If problem is not initialized
        """
        if not self.problem or not self.session:
            raise RuntimeError("Problem not initialized. Call init_static_problem() first.")

        logger.info(f"Agent step (session: {self.session.session_id})")

        # Record agent response
        self.session.add_interaction('agent', agent_response)

        try:
            # Execute action if provided
            if action:
                action_name = action.get('name')
                action_args = action.get('args', {})

                logger.info(f"Executing action: {action_name}")
                logger.debug(f"Action args: {action_args}")

                result = self.problem.perform_action(action_name, **action_args)

                observation = {
                    'type': 'action_result',
                    'action': action_name,
                    'result': result
                }

                logger.info(f"✓ Action executed: {action_name}")
            else:
                # No action, just acknowledge
                observation = {
                    'type': 'acknowledgment',
                    'message': 'Response recorded'
                }

            # Record environment response
            self.session.add_interaction('environment', str(observation))

            return {
                'observation': observation,
                'done': False
            }

        except Exception as e:
            logger.error(f"Error during step: {e}", exc_info=True)
            error_observation = {
                'type': 'error',
                'message': str(e)
            }
            self.session.add_interaction('environment', str(error_observation))

            return {
                'observation': error_observation,
                'done': False
            }

    def submit_solution(
        self,
        solution: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Submit agent's solution and evaluate it.

        Args:
            solution: Agent's proposed solution
                     Format varies by problem type

        Returns:
            Dictionary with evaluation results:
                - success: Whether solution is correct
                - metrics: Evaluation metrics
                - feedback: Human-readable feedback

        Raises:
            RuntimeError: If problem is not initialized
        """
        if not self.problem or not self.session or not self.results_writer:
            raise RuntimeError("Problem not initialized. Call init_static_problem() first.")

        logger.info("=" * 80)
        logger.info("EVALUATING SOLUTION")
        logger.info("=" * 80)

        # Mark session as complete
        self.session.complete()

        # Record solution submission
        self.session.add_interaction('agent', 'Solution submitted', solution=solution)

        try:
            # Evaluate solution
            logger.info("Running evaluation...")
            eval_results = self.problem.eval(
                soln=solution,
                trace=self.session.trace,
                duration=self.session.get_duration()
            )

            logger.info(f"✓ Evaluation complete")
            logger.info(f"  Success: {eval_results.get('success', False)}")

            # Save results
            self.results_writer.save_results(
                session_id=self.session.session_id,
                problem_id=self.session.problem_id,
                solution=solution,
                eval_results=eval_results,
                trace=self.session.trace,
                duration=self.session.get_duration()
            )

            logger.info(f"✓ Results saved to: {self.session.results_dir}")
            logger.info("=" * 80)

            return eval_results

        except Exception as e:
            logger.error(f"Error during evaluation: {e}", exc_info=True)
            raise
        finally:
            # Always cleanup after evaluation
            self._cleanup_after_problem()

    def _cleanup_after_problem(self):
        """
        Cleanup resources after problem completion.

        Stops replayer container and clears volume data.
        Always attempts cleanup even if errors occurred.
        """
        if not self.problem:
            return

        logger.info("Starting cleanup...")

        try:
            # Stop replayer and cleanup
            if hasattr(self.problem, 'cleanup'):
                self.problem.cleanup()
                logger.info("✓ Replayer cleanup complete")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)
            # Don't re-raise - cleanup is best-effort

        finally:
            self.problem = None
            self.session = None
            self.results_writer = None
            logger.info("Cleanup finished")
