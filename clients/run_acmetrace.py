#!/usr/bin/env python
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""CLI Entry Point for running AcmeTrace Kalos GPU Cluster RCA tasks."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from clients.utils.llm import GPTClient

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticDatasetOrchestrator
from aiopslab.orchestrator.problems.acmetrace_registry import AcmeTraceProblemRegistry
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
    verbose: bool = False
) -> dict:
    """Run a single AcmeTrace problem.

    Args:
        problem_id: Problem identifier (e.g., "acmetrace-kalos-analysis-0")
        max_steps: Maximum number of agent steps
        results_dir: Directory to save results
        verbose: If True, print full actions and responses

    Returns:
        dict: Evaluation results
    """
    logging.info(f"Starting problem: {problem_id}")

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

    logging.info(f"Completed: {problem_id}")
    logging.info(f"Results: {result.get('results', {})}")

    return result


def run_batch(
    task_type: str = None,
    max_steps: int = 30,
    results_dir: str = None,
    limit: int = None,
    verbose: bool = False
) -> list:
    """Run multiple AcmeTrace problems.

    Args:
        task_type: Filter by task type (detection, localization, analysis)
        max_steps: Maximum number of agent steps
        results_dir: Directory to save results
        limit: Maximum number of problems to run (optional)
        verbose: If True, print full actions and responses

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
            result = run_single(pid, max_steps=max_steps, results_dir=results_dir, verbose=verbose)
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

    args = parser.parse_args()
    print(f"Arguments: {args}")

    # Setup logging
    setup_logging(log_file=args.log_file, verbose=args.verbose)

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
            verbose=args.verbose
        )

        if args.output:
            with open(args.output, 'w') as f:
                json.dump(results, f, indent=2)
            logging.info(f"Results saved to: {args.output}")

        return

    # Single run mode
    if args.query_id is None:
        parser.error("--query_id is required for single run mode (or use --batch)")

    if args.task is None:
        parser.error("--task is required for single run mode")

    problem_id = f"acmetrace-kalos-{args.task}-{args.query_id}"
    result = run_single(
        problem_id=problem_id,
        max_steps=args.max_steps,
        results_dir=args.results_dir,
        verbose=args.verbose
    )

    print(f"\nResult: {json.dumps(result.get('results', {}), indent=2)}")


if __name__ == "__main__":
    main()
