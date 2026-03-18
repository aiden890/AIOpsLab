"""Runner for KG RCA Agent — Telecom dataset.

Usage:
    python clients/run_kg_rca_telecom.py
    python clients/run_kg_rca_telecom.py --all
    python clients/run_kg_rca_telecom.py --problem openrca_telecom-task_2-0
"""

import argparse
import asyncio
import csv
import logging
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.orchestrator.static_actions.rca_executor import StaticRCAActionsWithExecutor
from clients.openrca_rca.kg_rca_agent import KGRCAAgent
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_kg_rca_telecom")

DATASET = "openrca_telecom"
MAX_STEPS = 60

# Selected problem indices for Telecom (diverse reasons/components)
# Telecom has 138 problems; reasons: CPU fault, network delay, network loss,
# db connection limit, db close
TARGET_INDICES = [0, 5, 10, 20, 30, 50, 70, 90, 110, 130]

SCORE_FIELDS = [
    "timestamp", "eval_id", "model", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
    "correct_component", "correct_reason", "correct_time", "time_diff_min",
    "pred_component", "pred_reason", "pred_time",
    "gt_component", "gt_reason", "gt_time",
    "gt_level",
    "kg_format", "kg_top_k",
]


def extract_dataset_key(problem_id: str) -> str:
    return problem_id.rsplit("-", 2)[0]


_scores_lock = threading.Lock()


def append_score(scores_path: Path, row: dict):
    with _scores_lock:
        is_new = not scores_path.exists()
        with open(scores_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)


def build_problem_ids(indices: list[int] | None = None) -> list[str]:
    import pandas as pd
    base = Path(__file__).parent.parent
    query_csv = base / "aiopslab-applications/static_dataset/openrca/Telecom/query.csv"
    df = pd.read_csv(query_csv)
    if indices is None:
        indices = list(range(len(df)))
    pids = []
    for idx in indices:
        task_type = df.iloc[idx]["task_index"]
        pids.append(f"{DATASET}-{task_type}-{idx}")
    return pids


def run_single_problem(pid: str, args, results_dir: Path, eval_id: str,
                       api_config_path: str, worker_id: int = 0) -> dict:
    condition = f"w{worker_id}" if args.parallel > 1 else None

    dataset_key = extract_dataset_key(pid)
    dataset_type = dataset_key.replace("openrca_", "")

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)
    agent = KGRCAAgent(api_config_path=api_config_path)
    agent.sprint = orchestrator.sprint
    orchestrator.register_agent(agent, name="kg-rca")

    actions: StaticRCAActionsWithExecutor | None = None

    try:
        problem_desc, instructs, apis = orchestrator.init_problem(
            pid, condition=condition,
        )
        problem = orchestrator.session.problem

        eval_dir = orchestrator.session.get_save_dir()
        scores_path = eval_dir / "scores.csv"
        task_part = pid.split('-', maxsplit=1)[1] if '-' in pid else pid
        task_save_dir = eval_dir / task_part
        task_save_dir.mkdir(parents=True, exist_ok=True)
        orchestrator.session._save_dir_override = task_save_dir

        dataset_config = problem.app.dataset_config
        use_executor = dataset_config.get("executor", {}).get("enable", True)
        use_hypothesis = dataset_config.get("hypothesis", {}).get("enable", True)

        basic_prompt = get_basic_prompt(dataset_key)

        actions = StaticRCAActionsWithExecutor(
            container_name=problem.app.get_container_name(),
            possible_root_causes=dataset_config.get("possible_root_causes"),
            telemetry_flags=dataset_config.get("telemetry"),
            use_executor=use_executor,
            use_hypothesis=use_hypothesis,
        )
        actions.problem_id = pid
        actions.save_dir = str(task_save_dir)

        query_time_range = (
            problem.app.query_info.time_range
            if problem.app.query_info else None
        )

        if use_executor:
            notebook_path = task_save_dir / "executor.ipynb"
            actions.setup_executor(
                background=basic_prompt.schema,
                api_config_path=api_config_path,
                namespace=problem.namespace,
                logger=logger,
                notebook_save_path=str(notebook_path),
                query_time_range=query_time_range,
            )

        problem._actions = actions
        problem.actions = actions
        apis = problem.get_available_actions()

        enabled_types = getattr(actions, "enabled_telemetry_types", None)
        problem.telemetry_guide = build_executor_telemetry_guide(enabled_types)
        problem_desc = f"Namespace: {problem.app.namespace}\n\n{problem.instruction}"

        possible_rca = dataset_config.get("possible_root_causes")
        all_components = possible_rca.get("components", []) if possible_rca else None

        # Init agent & run
        agent.init_context(
            problem_desc, instructs, apis,
            possible_rca=possible_rca,
            dataset_type=dataset_type,
        )

        orchestrator._system_message = agent.history[0]["content"]
        orchestrator.session.extra["executor_trajectory"] = actions._executor_trajectory
        orchestrator.sprint.problem_init(problem_desc, instructs, apis)

        full_output = asyncio.run(
            orchestrator.start_problem(max_steps=args.max_steps)
        )
        results = full_output.get("results", {})
        score = results.get("score", 0)

        record = results.get("record", [])
        gt_fault = record[0] if record else {}
        gt_component = gt_fault.get("component", "")
        gt_reason = gt_fault.get("reason", "")
        gt_time = gt_fault.get("datetime", "")
        gt_level = gt_fault.get("level", "")

        detail = results.get("eval_detail", {})
        pred_component = detail.get("pred_component", "")
        pred_reason = detail.get("pred_reason", "")
        pred_time = detail.get("pred_time", "")

        correct_component = pred_component == gt_component if gt_component else ""
        correct_reason = pred_reason == gt_reason if gt_reason else ""

        time_diff_min = ""
        correct_time = ""
        if gt_time and pred_time:
            try:
                from datetime import datetime as _dt
                t1 = _dt.strptime(gt_time.strip(), "%Y-%m-%d %H:%M:%S")
                t2 = _dt.strptime(pred_time.strip(), "%Y-%m-%d %H:%M:%S")
                time_diff_min = round((t2 - t1).total_seconds() / 60.0, 1)
                correct_time = abs(time_diff_min) <= 1.0
            except ValueError:
                pass

        append_score(scores_path, {
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
            "correct_component": correct_component,
            "correct_reason": correct_reason,
            "correct_time": correct_time,
            "time_diff_min": time_diff_min,
            "pred_component": pred_component,
            "pred_reason": pred_reason,
            "pred_time": pred_time,
            "gt_component": gt_component,
            "gt_reason": gt_reason,
            "gt_time": gt_time,
            "gt_level": gt_level,
            "kg_format": args.kg_format,
            "kg_top_k": args.kg_top_k,
        })

        print(f"\n[{pid}] Score: {score}")
        print(f"[{pid}] GT: {gt_level}/{gt_component} — {gt_reason}")

        return {"score": score, "success": results.get("success", False)}

    except Exception as e:
        logger.error(f"Error running {pid}: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        orchestrator.session._save_dir_override = None
        if actions is not None:
            actions.cleanup()


def parse_args():
    parser = argparse.ArgumentParser(
        description="KG RCA Agent runner — Telecom"
    )
    parser.add_argument("--problem", type=str, help="Single problem ID")
    parser.add_argument("--all", action="store_true",
                        help="Run all problems (not just TARGET_INDICES)")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--api-config", type=str, default=None)
    parser.add_argument("--eval-id", type=str, default=None)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--kg-format", type=str, default="sections",
                        choices=["sections", "json", "markdown", "natural"])
    parser.add_argument("--kg-top-k", type=int, default=5)
    parser.add_argument("--parallel", type=int, default=1)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]

    api_config_path = args.api_config or str(
        Path(__file__).parent / "openrca_rca" / "api_config.yaml"
    )

    if args.problem:
        problem_ids = [args.problem]
    elif args.all:
        problem_ids = build_problem_ids(indices=None)
    else:
        problem_ids = build_problem_ids(indices=TARGET_INDICES)

    if args.start_index > 0:
        problem_ids = problem_ids[args.start_index:]

    n_parallel = max(1, args.parallel)
    logger.info(
        f"Running {len(problem_ids)} Telecom problems | eval_id={eval_id} | "
        f"parallel={n_parallel} | kg_format={args.kg_format} | kg_top_k={args.kg_top_k}"
    )
    print(f"eval_id: {eval_id}")
    for pid in problem_ids:
        print(f"  - {pid}")
    print()

    total_score = 0
    completed = 0

    if n_parallel <= 1:
        for pid in problem_ids:
            print(f"\n{'=' * 60}")
            print(f"Problem: {pid}")
            print(f"{'=' * 60}\n")
            result = run_single_problem(
                pid, args, results_dir, eval_id, api_config_path,
                worker_id=0,
            )
            if result is not None:
                total_score += result["score"]
                completed += 1
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import queue

        worker_pool = queue.Queue()
        for wid in range(n_parallel):
            worker_pool.put(wid)

        def _run_with_worker(pid):
            wid = worker_pool.get()
            try:
                return run_single_problem(
                    pid, args, results_dir, eval_id, api_config_path,
                    worker_id=wid,
                )
            finally:
                worker_pool.put(wid)

        futures = {}
        with ThreadPoolExecutor(max_workers=n_parallel) as pool:
            for pid in problem_ids:
                future = pool.submit(_run_with_worker, pid)
                futures[future] = pid

            for future in as_completed(futures):
                pid = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        total_score += result["score"]
                        completed += 1
                except Exception as e:
                    logger.error(f"Worker exception for {pid}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"Results saved under: {results_dir}")
    print(f"{'=' * 60}")
