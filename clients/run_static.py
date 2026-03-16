"""Unified runner for static dataset problems with Controller + Executor architecture.

Merges run_rca_agent.py and react_static.py:
  - Controller + Executor LLM pattern (from run_rca_agent.py)
  - Score CSV tracking and eval_id (from react_static.py)
  - Dynamic telemetry guide based on enabled telemetry types

Usage:
    # Run single problem
    python clients/run_static.py --problem openrca_bank-task_1-0

    # Run all Bank problems
    python clients/run_static.py --dataset openrca_bank

    # Run specific task type
    python clients/run_static.py --dataset openrca_bank --task-type task_7

    # Custom eval ID and results dir
    python clients/run_static.py --dataset openrca_bank --eval-id my_exp_01
"""

import argparse
import asyncio
import csv
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from clients.openrca_rca.agent import OpenRCARCAAgent
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_static")

SCORE_FIELDS = [
    "timestamp", "eval_id", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
]


def extract_dataset_key(problem_id: str) -> str:
    """'openrca_bank-task_1-0' → 'openrca_bank'"""
    return problem_id.rsplit("-", 2)[0]


def append_score(scores_path: Path, eval_id: str, pid: str, results: dict):
    """Append one result row to the scores CSV."""
    is_new = not scores_path.exists()
    with open(scores_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_id": eval_id,
            "problem_id": pid,
            "task_type": results.get("task_type", ""),
            "difficulty": results.get("difficulty", ""),
            "score": results.get("score", ""),
            "success": results.get("success", ""),
            "steps": results.get("steps", ""),
            "TTA": round(results.get("TTA", 0), 2),
            "in_tokens": results.get("in_tokens", ""),
            "out_tokens": results.get("out_tokens", ""),
        })


def parse_args():
    parser = argparse.ArgumentParser(
        description="Unified static dataset runner (Controller + Executor)"
    )
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g., openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g., task_1, task_7)")
    parser.add_argument("--max-steps", type=int, default=25, help="Max orchestrator steps")
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--api-config", type=str, default=None, help="Path to api_config.yaml")
    parser.add_argument("--eval-id", type=str, default=None,
                        help="Eval run identifier (default: auto UUID)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]
    scores_path = results_dir / f"{eval_id}_scores.csv"

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)

    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    logger.info(f"Running {len(problem_ids)} problems | eval_id={eval_id}")
    logger.info(f"Results dir: {results_dir} | Scores: {scores_path}\n")

    total_score = 0
    completed = 0

    for pid in problem_ids:
        print(f"\n{'=' * 60}")
        print(f"Problem: {pid}")
        print(f"{'=' * 60}\n")

        agent = OpenRCARCAAgent(api_config_path=args.api_config)
        orchestrator.register_agent(agent, name="openrca-rca")

        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            problem = orchestrator.session.problem

            # Build executor telemetry guide filtered to enabled types
            enabled_types = getattr(problem._actions, "enabled_telemetry_types", None)
            problem.telemetry_guide = build_executor_telemetry_guide(enabled_types)
            problem_desc = problem.get_task_description()

            agent.init_context(problem_desc, instructs, apis)

            dataset_key = extract_dataset_key(pid)
            agent.set_actions(
                problem._actions, problem.namespace, dataset_key,
                max_steps=args.max_steps,
            )

            orchestrator.session.extra["executor_trajectory"] = agent.executor_trajectory
            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            full_output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = full_output.get("results", {})

            score = results.get("score", 0)
            total_score += score
            completed += 1

            append_score(scores_path, eval_id, pid, results)

            print(f"\nScore: {score}")
            print(f"Passing: {results.get('passing_criteria', 'N/A')}")
            print(f"Failing: {results.get('failing_criteria', 'N/A')}")

        except Exception as e:
            logger.error(f"Error running {pid}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            agent.cleanup()

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"Scores: {scores_path}")
    print(f"{'=' * 60}")
