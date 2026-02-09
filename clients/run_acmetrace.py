#!/usr/bin/env python
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""CLI Entry Point for running AcmeTrace Kalos GPU Cluster RCA tasks."""

import argparse
import asyncio
import cmd
import json
import logging
import sys
from pathlib import Path
from clients.utils.llm import GPTClient

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticDatasetOrchestrator
from aiopslab.orchestrator.problems.acmetrace_registry import AcmeTraceProblemRegistry
from aiopslab.simulator.orchestrator import SimulationOrchestrator
from clients.react import Agent, trim_history_to_token_limit
from clients.utils.llm import vLLMClient
from clients.utils.acmetrace_templates import (
    ACMETRACE_DOCS,
    ACMETRACE_RESP_INSTR,
    CANDIDATE_CATEGORIES,
    CANDIDATE_REASONS,
)


class AcmeTraceAgent(Agent):
    """Agent specialized for AcmeTrace GPU cluster RCA tasks."""

    def __init__(self):
        self.history = []
        self.llm = GPTClient(auth_type="azure_key")

    def init_context(self, problem_desc: str, instructions: str, apis: dict):
        """Initialize the context for the AcmeTrace agent."""

        self.submit_api = self._filter_dict(apis, lambda k, _: "submit" in k)
        self.telemetry_apis = self._filter_dict(
            apis, lambda k, _: "submit" not in k
        )

        stringify_apis = lambda apis: "\n\n".join(
            [f"{k}\n{v}" for k, v in apis.items()]
        )

        self.system_message = ACMETRACE_DOCS.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(self.telemetry_apis),
            submit_api=stringify_apis(self.submit_api),
            candidate_categories=CANDIDATE_CATEGORIES,
            candidate_reasons=CANDIDATE_REASONS,
        )

        self.task_message = instructions

        print("\n" + "=" * 60)
        print("[PROMPT START - first 1000 chars]")
        print("=" * 60)
        print(self.system_message[:1000])
        print("=" * 60 + "\n")

        self.history.append({"role": "system", "content": self.system_message})
        self.history.append({"role": "user", "content": self.task_message})

    async def get_action(self, input) -> str:
        """Get the next action from the agent."""
        self.history.append({"role": "user", "content": self._add_instr(input)})
        trimmed_history = trim_history_to_token_limit(self.history)
        response = self.llm.run(trimmed_history)
        self.history.append({"role": "assistant", "content": response[0]})
        return response[0]

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}

    def _add_instr(self, input):
        return input + "\n\n" + ACMETRACE_RESP_INSTR


def setup_logging(log_file: str = None, verbose: bool = False):
    """Setup logging configuration.

    Args:
        log_file: Optional log file path
        verbose: Not used for log level (always INFO), only for action/response printing
    """
    level = logging.INFO
    handlers = [logging.StreamHandler()]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers
    )


def run_single(
    problem_id: str,
    max_steps: int = 30,
    results_dir: str = None,
    verbose: bool = False,
    simulate: bool = False,
    sim_start_time: str = None,
    sim_speed: float = 1.0,
) -> dict:
    """Run a single AcmeTrace problem.

    Args:
        problem_id: Problem identifier (e.g., "acmetrace-kalos-analysis-0")
        max_steps: Maximum number of agent steps
        results_dir: Directory to save results
        verbose: If True, print full actions and responses
        simulate: If True, use SimulationOrchestrator with clock control
        sim_start_time: Simulation start time (format: "YYYY-MM-DD HH:MM:SS")
        sim_speed: Simulation speed multiplier (default: 1.0 = real-time)

    Returns:
        dict: Evaluation results
    """
    logging.info(f"Starting problem: {problem_id}")

    if simulate:
        orchestrator = SimulationOrchestrator(results_dir=results_dir)
        orchestrator.dataset = "acmetrace"
        orchestrator.probs = AcmeTraceProblemRegistry()
        from aiopslab.orchestrator.parser import ResponseParser, ACMETRACE_EXAMPLES
        orchestrator.parser = ResponseParser(examples=ACMETRACE_EXAMPLES)
        orchestrator.verbose = verbose

        if sim_start_time:
            orchestrator.init_clock(sim_start_time, speed=sim_speed)
            orchestrator.start_clock()
            logging.info(f"Simulation clock: {orchestrator.get_clock_status()}")
        else:
            logging.warning("Simulation mode enabled but no --sim_start_time provided. "
                            "Time restriction will not be applied.")
    else:
        orchestrator = StaticDatasetOrchestrator(
            results_dir=results_dir,
            dataset="acmetrace",
            verbose=verbose
        )

    agent = AcmeTraceAgent()
    orchestrator.register_agent(agent, name="acmetrace-react")

    # Initialize problem
    task_desc, instructions, apis = orchestrator.init_problem(problem_id)
    agent.init_context(task_desc, instructions, apis)

    # Run problem
    result = asyncio.run(orchestrator.start_problem(max_steps))

    if simulate and orchestrator.clock:
        orchestrator.pause_clock()
        logging.info(f"Final simulation time: {orchestrator.get_clock_status()}")

    logging.info(f"Completed: {problem_id}")
    logging.info(f"Results: {result.get('results', {})}")

    return result


def run_batch(
    task_type: str = None,
    max_steps: int = 30,
    results_dir: str = None,
    limit: int = None,
    verbose: bool = False,
    simulate: bool = False,
    sim_start_time: str = None,
    sim_speed: float = 1.0,
) -> list:
    """Run multiple AcmeTrace problems.

    Args:
        task_type: Filter by task type (detection, localization, analysis)
        max_steps: Maximum number of agent steps
        results_dir: Directory to save results
        limit: Maximum number of problems to run (optional)
        verbose: If True, print full actions and responses
        simulate: If True, use SimulationOrchestrator with clock control
        sim_start_time: Simulation start time (format: "YYYY-MM-DD HH:MM:SS")
        sim_speed: Simulation speed multiplier (default: 1.0 = real-time)

    Returns:
        list: List of evaluation results
    """
    registry = AcmeTraceProblemRegistry()
    problem_ids = registry.get_problem_ids(cluster="kalos", task_type=task_type)

    if limit:
        problem_ids = problem_ids[:limit]

    logging.info(f"Running {len(problem_ids)} problems")

    results = []
    for i, pid in enumerate(problem_ids):
        logging.info(f"[{i+1}/{len(problem_ids)}] Running: {pid}")
        try:
            result = run_single(
                pid,
                max_steps=max_steps,
                results_dir=results_dir,
                verbose=verbose,
                simulate=simulate,
                sim_start_time=sim_start_time,
                sim_speed=sim_speed,
            )
            results.append({
                "problem_id": pid,
                "success": result.get("results", {}).get("success", False),
                "score": result.get("results", {}).get("score", 0.0),
                "results": result.get("results", {}),
            })
        except Exception as e:
            logging.error(f"Error running {pid}: {e}")
            results.append({
                "problem_id": pid,
                "success": False,
                "score": 0.0,
                "error": str(e),
            })

    # Print summary
    total = len(results)
    success_count = sum(1 for r in results if r.get("success", False))
    avg_score = sum(r.get("score", 0.0) for r in results) / total if total > 0 else 0.0

    logging.info("=" * 50)
    logging.info(f"Batch Summary:")
    logging.info(f"  Total: {total}")
    logging.info(f"  Success: {success_count} ({100*success_count/total:.1f}%)")
    logging.info(f"  Average Score: {avg_score:.3f}")
    logging.info("=" * 50)

    return results


def list_problems(task_type: str = None):
    """List available problems.

    Args:
        task_type: Filter by task type (optional)
    """
    registry = AcmeTraceProblemRegistry()
    problem_ids = registry.get_problem_ids(cluster="kalos", task_type=task_type)

    stats = registry.get_stats()
    print(f"Available problems ({stats['total']} total):")
    print(f"  Detection: {stats.get('detection', 0)}")
    print(f"  Localization: {stats.get('localization', 0)}")
    print(f"  Analysis: {stats.get('analysis', 0)}")
    print()

    for pid in problem_ids:
        print(f"  {pid}")


class AcmeTraceSimulatorShell(cmd.Cmd):
    """Interactive shell for AcmeTrace simulation."""

    intro = """
=============================================
  AcmeTrace RCA Simulator - Interactive Mode
=============================================
Type 'help' for available commands.
"""
    prompt = "acmetrace>>> "

    def __init__(self):
        super().__init__()
        self.orch = SimulationOrchestrator(results_dir=None)
        self.orch.dataset = "acmetrace"
        self.orch.probs = AcmeTraceProblemRegistry()
        from aiopslab.orchestrator.parser import ResponseParser, ACMETRACE_EXAMPLES
        self.orch.parser = ResponseParser(examples=ACMETRACE_EXAMPLES)
        self.orch.verbose = True

        self.agent = None
        self.problem_id = "acmetrace-kalos-analysis-0"  # Default problem
        self._initialized = False

    def _ensure_initialized(self):
        """Initialize problem and agent if not already done."""
        if self._initialized:
            return True

        if self.orch.current_time is None:
            print("Error: Set simulation time first with 'time' command")
            return False

        self.agent = AcmeTraceAgent()
        self.orch.register_agent(self.agent, name="acmetrace-react")

        try:
            task_desc, instructions, apis = self.orch.init_problem(self.problem_id)
        except Exception as e:
            print(f"Error initializing problem: {e}")
            return False

        self.agent.init_context(task_desc, instructions, apis)

        self._initialized = True
        print(f"Initialized: {self.problem_id}")
        return True

    # === Time Commands ===

    def do_time(self, arg):
        """time [YYYY-MM-DD HH:MM:SS] - Get or set simulation time

        Examples:
            time                           - Show current time and status
            time 2023-08-15 10:00:00      - Set time (clock paused)
        """
        arg = arg.strip()
        if arg:
            if self.orch.set_time(arg):
                print(f"Time set: {self.orch.get_clock_status()}")
                self._initialized = False
            else:
                print(f"Invalid time format: {arg}")
                print("Use: YYYY-MM-DD HH:MM:SS")
        else:
            print(f"Clock: {self.orch.get_clock_status()}")

    # === Clock Control Commands ===

    def do_start(self, arg):
        """start - Start the simulation clock"""
        if self.orch.clock is None:
            print("Error: Set time first with 'time' command")
            return
        self.orch.start_clock()
        print(f"Clock started: {self.orch.get_clock_status()}")

    def do_pause(self, arg):
        """pause - Pause the simulation clock"""
        if self.orch.clock is None:
            print("Error: Clock not initialized")
            return
        self.orch.pause_clock()
        print(f"Clock paused: {self.orch.get_clock_status()}")

    def do_resume(self, arg):
        """resume - Resume the simulation clock"""
        if self.orch.clock is None:
            print("Error: Clock not initialized")
            return
        self.orch.resume_clock()
        print(f"Clock resumed: {self.orch.get_clock_status()}")

    def do_speed(self, arg):
        """speed [N] - Get or set clock speed multiplier

        Examples:
            speed       - Show current speed
            speed 1     - Real-time (1 real sec = 1 sim sec)
            speed 60    - Fast (1 real sec = 1 sim min)
            speed 3600  - Very fast (1 real sec = 1 sim hour)
        """
        arg = arg.strip()
        if arg:
            try:
                speed = float(arg)
                if self.orch.set_speed(speed):
                    print(f"Speed set: {speed}x")
                else:
                    print("Error: Initialize clock first with 'time' command")
            except ValueError:
                print(f"Invalid speed: {arg}")
        else:
            if self.orch.clock:
                print(f"Current speed: {self.orch.clock.speed}x")
            else:
                print("Clock not initialized")

    # === Run Commands ===

    def do_run(self, arg):
        """run [max_steps] - Run the agent

        Examples:
            run      - Run with default 30 steps
            run 10   - Run with max 10 steps

        Note: Clock continues running during agent execution.
              Data access is restricted to before current simulation time.
        """
        if not self._ensure_initialized():
            return

        max_steps = 30
        if arg.strip():
            try:
                max_steps = int(arg.strip())
            except ValueError:
                print("Invalid step count. Using default: 30")

        print(f"\nRunning agent...")
        print(f"Clock: {self.orch.get_clock_status()}")
        print(f"Data access limited to before current simulation time")
        print("-" * 40)

        try:
            result = asyncio.run(self.orch.start_problem(max_steps))
            print("-" * 40)
            print(f"Final time: {self.orch.get_clock_status()}")
            print(f"Result: {result.get('results', {})}")
        except Exception as e:
            print(f"Error: {e}")

        self._initialized = False

    # === Status Commands ===

    def do_status(self, arg):
        """status - Show current simulation status"""
        print(f"Clock:   {self.orch.get_clock_status()}")
        print(f"Problem: {self.problem_id}")
        print(f"Agent:   {'acmetrace-react' if self.agent else 'Not deployed'}")
        print(f"Ready:   {'Yes' if self._initialized else 'No'}")

    def do_problem(self, arg):
        """problem [id] - Get or set problem ID

        Examples:
            problem                               - Show current problem
            problem acmetrace-kalos-analysis-5    - Set problem
            problem acmetrace-kalos-detection-0   - Set problem
        """
        arg = arg.strip()
        if arg:
            self.problem_id = arg
            self._initialized = False
            print(f"Problem set: {self.problem_id}")
        else:
            print(f"Current problem: {self.problem_id}")

    def do_problems(self, arg):
        """problems [task_type] - List available problems

        Examples:
            problems             - List all problems
            problems analysis    - List only analysis problems
        """
        task_type = arg.strip() or None
        list_problems(task_type=task_type)

    # === Exit Commands ===

    def do_quit(self, arg):
        """quit - Exit the simulator"""
        if self.orch.clock and self.orch.clock.is_running:
            self.orch.pause_clock()
        print("Goodbye!")
        return True

    def do_exit(self, arg):
        """exit - Exit the simulator"""
        return self.do_quit(arg)

    def do_q(self, arg):
        """q - Exit the simulator"""
        return self.do_quit(arg)

    # === Help ===

    def do_help(self, arg):
        """Show help for commands"""
        if arg:
            super().do_help(arg)
        else:
            print("""
Available commands:

  Time & Clock:
    time [TIME]      - Get/set simulation time (YYYY-MM-DD HH:MM:SS)
    start            - Start the clock (time advances)
    pause            - Pause the clock
    resume           - Resume the clock
    speed [N]        - Get/set speed multiplier (60 = 1 real sec = 1 sim min)

  Execution:
    run [STEPS]      - Run agent (default: 30 steps)
    status           - Show current status
    problem [ID]     - Get/set problem ID
    problems [TYPE]  - List available problems

  Other:
    quit/exit/q      - Exit simulator

Quick start:
  >>> time 2023-08-15 10:00:00
  >>> speed 60
  >>> start
  >>> run
""")

    def emptyline(self):
        """Do nothing on empty line."""
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Run AcmeTrace Kalos GPU Cluster RCA tasks with AIOpsLab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List available problems
  python clients/run_acmetrace.py --list

  # List only analysis problems
  python clients/run_acmetrace.py --list --task analysis

  # Run a single problem
  python clients/run_acmetrace.py --task analysis --query_id 0

  # Run all detection problems
  python clients/run_acmetrace.py --task detection --batch

  # Run all problems (limited to 10)
  python clients/run_acmetrace.py --batch --limit 10

  # Run with simulation clock (time-restricted data access)
  python clients/run_acmetrace.py --task analysis --query_id 0 --simulate --sim_start_time "2023-08-15 10:00:00" --sim_speed 60

  # Launch interactive simulator shell
  python clients/run_acmetrace.py --shell

Prerequisites:
  # First, generate the sample dataset
  python acme_cluster_dataset/sample_kalos_rca.py --start-date 2023-08-15 --end-date 2023-08-16
        """
    )

    parser.add_argument(
        "--task",
        choices=["detection", "localization", "analysis"],
        default=None,
        help="Task type filter"
    )
    parser.add_argument(
        "--query_id",
        type=int,
        default=None,
        help="Specific query ID to run"
    )
    parser.add_argument(
        "--max_steps",
        type=int,
        default=30,
        help="Maximum number of agent steps (default: 30)"
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run all matching problems in batch mode"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of problems in batch mode"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available problems and exit"
    )
    parser.add_argument(
        "--results_dir",
        type=str,
        default=None,
        help="Directory to save results"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file for batch results (JSON)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--log_file",
        type=str,
        default=None,
        help="Log file path"
    )
    # Simulation arguments
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Enable simulation mode with clock control and time-restricted data access"
    )
    parser.add_argument(
        "--sim_start_time",
        type=str,
        default=None,
        help="Simulation start time (format: 'YYYY-MM-DD HH:MM:SS')"
    )
    parser.add_argument(
        "--sim_speed",
        type=float,
        default=1.0,
        help="Simulation speed multiplier (default: 1.0 = real-time, 60 = 1 real sec = 1 sim min)"
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Launch interactive simulator shell"
    )

    args = parser.parse_args()
    print(f"Arguments: {args}")

    # Setup logging
    setup_logging(log_file=args.log_file, verbose=args.verbose)

    # Interactive shell mode
    if args.shell:
        shell = AcmeTraceSimulatorShell()
        try:
            shell.cmdloop()
        except KeyboardInterrupt:
            print("\nGoodbye!")
        return

    # List mode
    if args.list:
        list_problems(task_type=args.task)
        return

    # Batch mode
    if args.batch:
        print(f"Running batch mode with task type: {args.task}, max steps: {args.max_steps}, results dir: {args.results_dir}, limit: {args.limit}, verbose: {args.verbose}")
        results = run_batch(
            task_type=args.task,
            max_steps=args.max_steps,
            results_dir=args.results_dir,
            limit=args.limit,
            verbose=args.verbose,
            simulate=args.simulate,
            sim_start_time=args.sim_start_time,
            sim_speed=args.sim_speed,
        )

        if args.output:
            with open(args.output, 'w') as f:
                json.dump(results, f, indent=2)
            logging.info(f"Results saved to: {args.output}")

        return

    # Single run mode
    if args.query_id is None:
        parser.error("--query_id is required for single run mode (or use --batch or --shell)")

    if args.task is None:
        parser.error("--task is required for single run mode")

    problem_id = f"acmetrace-kalos-{args.task}-{args.query_id}"
    result = run_single(
        problem_id=problem_id,
        max_steps=args.max_steps,
        results_dir=args.results_dir,
        verbose=args.verbose,
        simulate=args.simulate,
        sim_start_time=args.sim_start_time,
        sim_speed=args.sim_speed,
    )

    print(f"\nResult: {json.dumps(result.get('results', {}), indent=2)}")


if __name__ == "__main__":
    main()
