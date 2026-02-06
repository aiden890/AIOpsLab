#!/usr/bin/env python
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""CLI Entry Point for running OpenRCA RCA tasks."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticDatasetOrchestrator
from aiopslab.orchestrator.problems.openrca_registry import OpenRCAProblemRegistry
from clients.react import Agent, trim_history_to_token_limit
from clients.utils.llm import vLLMClient
from clients.utils.openrca_templates import OPENRCA_DOCS, OPENRCA_RESP_INSTR


class OpenRCAAgent(Agent):
    """Agent specialized for OpenRCA tasks with proper prompt templates."""

    def __init__(self):
        self.history = []
        self.llm = vLLMClient()

    def init_context(self, problem_desc: str, instructions: str, apis: str,
                     candidate_components: str = "", candidate_reasons: str = ""):
        """Initialize the context for the OpenRCA agent."""

        self.submit_api = self._filter_dict(apis, lambda k, _: "submit" in k)
        self.telemetry_apis = self._filter_dict(
            apis, lambda k, _: "submit" not in k
        )

        stringify_apis = lambda apis: "\n\n".join(
            [f"{k}\n{v}" for k, v in apis.items()]
        )

        self.system_message = OPENRCA_DOCS.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(self.telemetry_apis),
            submit_api=stringify_apis(self.submit_api),
            candidate_components=candidate_components or "Not specified",
            candidate_reasons=candidate_reasons or "Not specified",
        )

        self.task_message = instructions

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
        return input + "\n\n" + OPENRCA_RESP_INSTR


def setup_logging(log_file: str = None, verbose: bool = False):
    """Setup logging configuration.

    Args:
        log_file: Optional log file path
        verbose: Enable verbose (DEBUG) logging
    """
    level = logging.DEBUG if verbose else logging.INFO
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
    results_dir: str = None
) -> dict:
    """Run a single OpenRCA problem.

    Args:
        problem_id: Problem identifier (e.g., "openrca-bank-task_3-0")
        max_steps: Maximum number of agent steps
        results_dir: Directory to save results

    Returns:
        dict: Evaluation results
    """
    logging.info(f"Starting problem: {problem_id}")

    orchestrator = StaticDatasetOrchestrator(results_dir=results_dir)
    agent = OpenRCAAgent()
    orchestrator.register_agent(agent, name="openrca-react")

    # Initialize problem
    task_desc, instructions, apis = orchestrator.init_problem(problem_id)
    agent.init_context(task_desc, instructions, apis)

    # Run problem
    result = asyncio.run(orchestrator.start_problem(max_steps))

    logging.info(f"Completed: {problem_id}")
    logging.info(f"Results: {result.get('results', {})}")

    return result


def run_batch(
    domain: str,
    task_index: str = None,
    max_steps: int = 30,
    results_dir: str = None,
    limit: int = None
) -> list:
    """Run multiple OpenRCA problems.

    Args:
        domain: Domain name (bank, market, telecom)
        task_index: Filter by task type (optional)
        max_steps: Maximum number of agent steps
        results_dir: Directory to save results
        limit: Maximum number of problems to run (optional)

    Returns:
        list: List of evaluation results
    """
    registry = OpenRCAProblemRegistry()
    problem_ids = registry.get_problem_ids(domain=domain, task_index=task_index)

    if limit:
        problem_ids = problem_ids[:limit]

    logging.info(f"Running {len(problem_ids)} problems from {domain} domain")

    results = []
    for i, pid in enumerate(problem_ids):
        logging.info(f"[{i+1}/{len(problem_ids)}] Running: {pid}")
        try:
            result = run_single(pid, max_steps=max_steps, results_dir=results_dir)
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


def list_problems(domain: str = None, task_index: str = None):
    """List available problems.

    Args:
        domain: Filter by domain (optional)
        task_index: Filter by task type (optional)
    """
    registry = OpenRCAProblemRegistry()
    problem_ids = registry.get_problem_ids(domain=domain, task_index=task_index)

    print(f"Available problems ({len(problem_ids)} total):")
    for pid in problem_ids:
        print(f"  {pid}")


def main():
    parser = argparse.ArgumentParser(
        description="Run OpenRCA RCA tasks with AIOpsLab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List available problems
  python run_openrca.py --list --domain bank

  # Run a single problem
  python run_openrca.py --domain bank --task task_3 --query_id 0

  # Run all task_3 problems in bank domain
  python run_openrca.py --domain bank --task task_3 --batch

  # Run all problems in bank domain (limited to 5)
  python run_openrca.py --domain bank --batch --limit 5
        """
    )

    parser.add_argument(
        "--domain",
        choices=["bank", "market", "telecom"],
        required=True,
        help="OpenRCA domain to use"
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Task type filter (e.g., task_1, task_3, task_7)"
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

    # Setup logging
    setup_logging(log_file=args.log_file, verbose=args.verbose)

    # List mode
    if args.list:
        list_problems(domain=args.domain, task_index=args.task)
        return

    # Batch mode
    if args.batch:
        results = run_batch(
            domain=args.domain,
            task_index=args.task,
            max_steps=args.max_steps,
            results_dir=args.results_dir,
            limit=args.limit
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

    problem_id = f"openrca-{args.domain}-{args.task}-{args.query_id}"
    result = run_single(
        problem_id=problem_id,
        max_steps=args.max_steps,
        results_dir=args.results_dir
    )

    print(f"\nResult: {json.dumps(result.get('results', {}), indent=2)}")


if __name__ == "__main__":
    main()
