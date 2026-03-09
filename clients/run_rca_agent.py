"""Runner for OpenRCA RCA Agent on static dataset problems.

Uses the standard orchestrator.start_problem() loop:
  - ask_agent() → Controller LLM step → returns execute("instruction") or submit({answer})
  - ask_env()   → Executor runs via execute action callback

Usage:
    # Run single problem
    python clients/run_rca_agent.py --problem openrca_bank-task_1-0

    # Run all Bank problems
    python clients/run_rca_agent.py --dataset openrca_bank

    # Run specific task type
    python clients/run_rca_agent.py --dataset openrca_bank --task-type task_7
"""

import argparse
import asyncio
import csv
import glob
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from clients.openrca_rca.agent import OpenRCARCAAgent
from clients.openrca_rca.prompts.telemetry_guide import build_telemetry_guide

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("openrca_rca")

SCORE_FIELDS = [
    "timestamp", "eval_id", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
]


def append_score(scores_path: Path, eval_id: str, pid: str, results: dict):
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
    parser = argparse.ArgumentParser(description="OpenRCA RCA Agent runner")
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g., openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g., task_1, task_7)")
    parser.add_argument("--max-steps", type=int, default=25, help="Max orchestrator steps")
    parser.add_argument("--results-dir", type=str, default="results/rca_agent")
    parser.add_argument("--api-config", type=str, default=None, help="Path to api_config.yaml")
    parser.add_argument("--eval-id", type=str, default=None,
                        help="Eval run identifier (default: auto UUID)")
    parser.add_argument("--condition", type=str, default="all",
                        choices=["all", "no_log", "no_metric", "no_trace"],
                        help="Telemetry ablation condition (default: all)")
    parser.add_argument("--work-dir", type=str, default=None,
                        help="Directory for telemetry CSV files (unique per parallel run)")
    parser.add_argument("--start-index", type=int, default=0,
                        help="Skip the first N problems and start from this index (default: 0)")
    return parser.parse_args()


def extract_dataset_key(problem_id):
    """Extract dataset key from problem ID.

    'openrca_bank-task_1-0' → 'openrca_bank'
    'openrca_market_cb1-task_7-5' → 'openrca_market_cb1'
    """
    parts = problem_id.rsplit("-", 2)
    return parts[0]


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)

    # Select problems to run
    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    if args.start_index > 0:
        logger.info(f"Skipping first {args.start_index} problems (--start-index={args.start_index})")
        problem_ids = problem_ids[args.start_index:]

    api_config_path = args.api_config or str(
        Path(__file__).parent / "openrca_rca" / "api_config.yaml"
    )

    logger.info(f"Running {len(problem_ids)} problems | eval_id={eval_id}")
    logger.info(f"Results dir: {results_dir}\n")

    # W&B: create one run for the entire experiment
    _tmp_agent = OpenRCARCAAgent(api_config_path=api_config_path)
    model_name = _tmp_agent.get_model_name()
    orchestrator.init_wandb(
        run_name=f"openrca-rca/{model_name}/{args.dataset}/{eval_id}",
        config={"agent": "openrca-rca", "model": model_name, "eval_id": eval_id,
                "dataset": args.dataset, "total_problems": len(problem_ids)},
    )

    total_score = 0
    completed = 0

    for pid in problem_ids:
        print(f"\n{'=' * 60}")
        print(f"Problem: {pid}")
        print(f"{'=' * 60}\n")

        agent = OpenRCARCAAgent(api_config_path=api_config_path)
        orchestrator.register_agent(agent, name="openrca-rca")

        try:
            # 1. Initialize problem (deploy, setup)
            #    Inject RCA-specific telemetry guide before getting task description
            problem_desc, instructs, apis = orchestrator.init_problem(pid, work_dir=args.work_dir, condition=args.condition)

            # All outputs for this problem go into the session's save directory
            save_dir = orchestrator.session.get_save_dir()
            scores_path = save_dir / "scores.csv"

            # Override telemetry guide with RCA agent's version and regenerate
            problem = orchestrator.session.problem
            dataset_key = extract_dataset_key(pid)
            problem.telemetry_guide = build_telemetry_guide(args.condition, dataset_key=dataset_key)
            problem_desc = problem.get_task_description()

            # Original RCA agent only uses execute and submit (not pre-built analysis APIs)
            apis = {k: v for k, v in apis.items() if k in ("execute", "submit")}

            # Sync corrected info back so start_problem()'s session log reflects the override
            orchestrator._problem_init_info = (problem_desc, instructs, apis)

            agent.init_context(problem_desc, instructs, apis)

            # 2. Inject Executor callback into the actions object
            agent.set_actions(problem._actions, problem.namespace, dataset_key,
                              max_steps=args.max_steps, condition=args.condition)

            # Expose system prompt to session log (includes background + candidates)
            orchestrator._system_message = agent.controller_prompt[0]["content"]

            # 3. Link executor trajectory and condition to session
            orchestrator.session.extra["executor_trajectory"] = agent.executor_trajectory
            orchestrator.session.extra["condition"] = args.condition

            # 4. Print initial problem setup
            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            # 5. Run orchestrator loop (ask_agent → ask_env → repeat)
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

    orchestrator.finish_wandb()

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"{'=' * 60}")
