"""Runner for ReAct RCA Agent with self-contained Executor action.

Uses:
  - ReactRCAAgent: structured JSON ReAct agent (1 LLM call/step)
  - StaticRCAActionsWithExecutor: execute() owns IPython kernel + Executor LLM
  - Score CSV tracking and eval_id

Usage:
    # Run single problem
    python clients/run_react_rca.py --problem openrca_bank-task_1-0

    # Run all Bank problems
    python clients/run_react_rca.py --dataset openrca_bank

    # Run specific task type
    python clients/run_react_rca.py --dataset openrca_bank --task-type task_3

    # Custom eval ID
    python clients/run_react_rca.py --dataset openrca_bank --eval-id trace_only_01
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
from aiopslab.orchestrator.static_actions.rca_executor import StaticRCAActionsWithExecutor
from clients.openrca_rca.react_rca_agent import ReactRCAAgent
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_react_rca")

MAX_STEPS = 40

SCORE_FIELDS = [
    "timestamp", "eval_id", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
]


def extract_dataset_key(problem_id: str) -> str:
    """'openrca_bank-task_1-0' → 'openrca_bank'"""
    return problem_id.rsplit("-", 2)[0]


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
    parser = argparse.ArgumentParser(
        description="ReAct RCA Agent runner (structured JSON + self-contained Executor)"
    )
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g., openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g., task_1)")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS,
                        help=f"Max orchestrator steps (default: {MAX_STEPS})")
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--api-config", type=str, default=None,
                        help="Path to api_config.yaml")
    parser.add_argument("--eval-id", type=str, default=None,
                        help="Eval run identifier (default: auto UUID)")
    parser.add_argument("--start-index", type=int, default=0,
                        help="Skip the first N problems and start from this index (default: 0)")
    parser.add_argument("--problems-file", type=str, default=None,
                        help="Path to a text file listing problem IDs (one per line, # comments ignored)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]

    api_config_path = args.api_config or str(
        Path(__file__).parent / "openrca_rca" / "api_config.yaml"
    )

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)

    if args.problems_file:
        with open(args.problems_file) as f:
            problem_ids = [
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    elif args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    if args.start_index > 0:
        logger.info(f"Skipping first {args.start_index} problems (--start-index={args.start_index})")
        problem_ids = problem_ids[args.start_index:]

    logger.info(f"Running {len(problem_ids)} problems | eval_id={eval_id}")
    logger.info(f"Base results dir: {results_dir}\n")

    # W&B: create one run for the entire experiment
    _tmp_agent = ReactRCAAgent(api_config_path=api_config_path)
    model_name = _tmp_agent.get_model_name()
    orchestrator.init_wandb(
        run_name=f"react-rca/{model_name}/{args.dataset}/{eval_id}",
        config={"agent": "react-rca", "model": model_name, "eval_id": eval_id,
                "dataset": args.dataset, "total_problems": len(problem_ids)},
    )

    total_score = 0
    completed = 0

    for pid in problem_ids:
        print(f"\n{'=' * 60}")
        print(f"Problem: {pid}")
        print(f"{'=' * 60}\n")

        dataset_key = extract_dataset_key(pid)
        agent = ReactRCAAgent(api_config_path=api_config_path)
        orchestrator.register_agent(agent, name="react-rca")

        actions: StaticRCAActionsWithExecutor | None = None

        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            problem = orchestrator.session.problem

            # All outputs for this problem go into the session's save directory
            save_dir = orchestrator.session.get_save_dir()
            scores_path = save_dir / "scores.csv"

            # Replace default actions with self-contained executor variant
            dataset_config = problem.app.dataset_config
            use_executor = dataset_config.get("executor", {}).get("enable", True)
            use_hypothesis = dataset_config.get("hypothesis", {}).get("enable", False)

            basic_prompt = get_basic_prompt(dataset_key)

            actions = StaticRCAActionsWithExecutor(
                raw_dataset_path=str(problem.app.get_raw_dataset_path()),
                raw_data_mapping=dataset_config.get("data_mapping"),
                raw_dataset_type=dataset_config.get("dataset_type"),
                default_start_time=problem.app.get_default_time_window()[0],
                default_end_time=problem.app.get_default_time_window()[1],
                possible_root_causes=dataset_config.get("possible_root_causes"),
                telemetry_flags=dataset_config.get("telemetry"),
                use_executor=use_executor,
                use_hypothesis=use_hypothesis,
                work_dir=str(save_dir),
            )

            if use_executor:
                task_part = pid.split('-', maxsplit=1)[1] if '-' in pid else pid
                notebook_path = save_dir / f"{task_part}_executor.ipynb"
                query_time_range = (
                    problem.app.query_info.time_range
                    if problem.app.query_info else None
                )
                actions.setup_executor(
                    background=basic_prompt.schema,
                    api_config_path=api_config_path,
                    namespace=problem.namespace,
                    logger=logger,
                    notebook_save_path=str(notebook_path),
                    query_time_range=query_time_range,
                )

            # Swap actions on the problem so get_available_actions() uses ours
            problem._actions = actions
            problem.actions = actions

            # Rebuild apis with the new actions (respects telemetry_flags)
            all_apis = problem.get_available_actions()
            # Executor agent only needs execute + submit; hide inherited base APIs
            apis = {k: v for k, v in all_apis.items() if k in ("execute", "submit")}

            # Build dynamic executor telemetry guide
            enabled_types = getattr(actions, "enabled_telemetry_types", None)
            problem.telemetry_guide = build_executor_telemetry_guide(enabled_types)
            problem_desc = problem.get_task_description()

            possible_rca = dataset_config.get("possible_root_causes")
            agent.init_context(
                problem_desc, instructs, apis,
                possible_rca=possible_rca,
            )

            # Expose system prompt so start_problem() prints it via sprint.system_prompt()
            orchestrator._system_message = agent.history[0]["content"]

            # Link executor trajectory to session for logging
            orchestrator.session.extra["executor_trajectory"] = actions._executor_trajectory

            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            full_output = asyncio.run(
                orchestrator.start_problem(max_steps=args.max_steps)
            )
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
            if actions is not None:
                actions.cleanup()

    orchestrator.finish_wandb()

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"Results saved under: {results_dir}")
    print(f"{'=' * 60}")
