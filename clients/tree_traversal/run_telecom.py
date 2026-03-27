"""Tree-traversal RCA runner — Telecom dataset.

Usage:
    python clients/tree_traversal/run_telecom.py
    python clients/tree_traversal/run_telecom.py --all
    python clients/tree_traversal/run_telecom.py --problem openrca_telecom-task_2-0
    python clients/tree_traversal/run_telecom.py --no-video
    python clients/tree_traversal/run_telecom.py --live-view
    python clients/tree_traversal/run_telecom.py --parallel 8 --eval-id telecom-batch-v1
    # Run RCA from prefiltered data under prefiltered_telemetry/openrca_telecom/
    python clients/tree_traversal/run_telecom.py --use-prefiltered prefiltered_telemetry
    python clients/tree_traversal/run_telecom.py --problem openrca_telecom-task_2-0 --use-prefiltered prefiltered_telemetry --no-video --live-view
    python clients/tree_traversal/run_telecom.py --problem all --use-prefiltered prefiltered_telemetry --no-video --live-view --eval-id telecom-prefiltered-v1 --parallel 8
"""

import argparse
import csv
import logging
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.orchestrator.static_actions.rca_executor import StaticRCAActionsWithExecutor
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide
from clients.openrca_rca.api_router import load_config

from clients.tree_traversal.dataset_profile import build_profile
from clients.tree_traversal.staged_rca_pipeline import StagedRCAPipeline
from clients.tree_traversal.live_tree_viewer import LiveTreeViewer
from clients.tree_traversal.run_market_cb1 import (
    append_score, render_manim_video, SCORE_FIELDS, list_prefiltered_problem_ids,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_tree_telecom")

DATASET = "openrca_telecom"
MAX_STEPS = 60

# Diverse problems across reasons: CPU fault, network delay, network loss,
# db connection limit, db close
TARGET_INDICES = [0, 2, 4, 5, 8, 9, 13, 19, 20, 27]


def extract_dataset_key(problem_id: str) -> str:
    return problem_id.rsplit("-", 2)[0]


def build_problem_ids(indices: list[int] | None = None) -> list[str]:
    import pandas as pd
    base = Path(__file__).parent.parent.parent
    query_csv = base / "aiopslab-applications/static_dataset/openrca/Telecom/query.csv"
    df = pd.read_csv(query_csv)
    if indices is None:
        indices = list(range(len(df)))
    pids = []
    for idx in indices:
        task_type = df.iloc[idx]["task_index"]
        pids.append(f"{DATASET}-{task_type}-{idx}")
    return pids


def run_single_problem(
    pid: str,
    args,
    results_dir: Path,
    eval_id: str,
    api_config_path: str,
    llm_configs: dict,
    worker_id: int = 0,
    prefiltered_dir: Path | None = None,
) -> dict | None:
    """Run one Telecom problem end-to-end (one static-dataset instance).

    worker_id is used only when running in parallel, to pass a distinct
    condition string to init_problem so that each worker can have its own
    static-dataset namespace (mirrors Market runner behavior).
    When prefiltered_dir is set, telemetry is read from
    prefiltered_dir/<dataset_key>/<task_id>/ (or legacy prefiltered_dir/<task_id>/)
    with no Docker deploy.
    """
    dataset_key = extract_dataset_key(pid)
    if prefiltered_dir is not None:
        condition = None  # use static-telecom namespace to match prefilter output
    else:
        condition = f"w{worker_id}" if args.parallel > 1 else None

    # Local copy of llm_configs so that token usage does not leak across problems.
    local_llm_configs = dict(llm_configs)
    local_llm_configs["_in_tokens"] = 0
    local_llm_configs["_out_tokens"] = 0

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)
    orchestrator.register_agent(None, name="tree-traversal")
    sprint = orchestrator.sprint

    actions: StaticRCAActionsWithExecutor | None = None
    live_viewer = None

    try:
        problem_desc, instructs, apis = orchestrator.init_problem(
            pid, condition=condition, skip_deploy=(prefiltered_dir is not None)
        )
        problem = orchestrator.session.problem

        eval_dir = orchestrator.session.get_save_dir()
        scores_path = eval_dir / "scores.csv"
        task_part = pid.split('-', maxsplit=1)[1] if '-' in pid else pid
        task_save_dir = eval_dir / task_part
        task_save_dir.mkdir(parents=True, exist_ok=True)
        orchestrator.session._save_dir_override = task_save_dir

        # Initialize SessionPrint log (session.log) similar to Market runner
        log_filepath = task_save_dir / "session.log"
        sprint.init_log_file(str(log_filepath))
        sprint.problem_init(problem_desc, instructs, apis)

        dataset_config = problem.app.dataset_config
        use_executor = dataset_config.get("executor", {}).get("enable", True)
        basic_prompt = get_basic_prompt(dataset_key)

        if prefiltered_dir is not None:
            pid_suffix = pid.split("-", maxsplit=1)[1] if "-" in pid else pid
            legacy_task_id = getattr(problem, "task_type", None) or getattr(problem.app.query_info, "task_id", None)
            dataset_prefiltered_dir = prefiltered_dir.resolve() / dataset_key
            candidate_paths = [
                dataset_prefiltered_dir / pid_suffix,
                prefiltered_dir.resolve() / pid_suffix,
            ]
            if legacy_task_id:
                candidate_paths.append(dataset_prefiltered_dir / legacy_task_id)
                candidate_paths.append(prefiltered_dir.resolve() / legacy_task_id)
            base_dir = next((p for p in candidate_paths if p.exists()), candidate_paths[0])
            base_path = str(base_dir)
            container_name = None
        else:
            base_path = None
            container_name = problem.app.get_container_name()

        actions = StaticRCAActionsWithExecutor(
            container_name=container_name,
            base_path=base_path,
            possible_root_causes=dataset_config.get("possible_root_causes"),
            telemetry_flags=dataset_config.get("telemetry"),
            use_executor=use_executor,
            use_hypothesis=False,
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

        config_path = "aiopslab/service/apps/static_dataset/config/openrca_telecom.json"
        if not Path(config_path).exists():
            config_path = None
        profile = build_profile(dataset_key, config_path=config_path)

        if args.live_view:
            try:
                import matplotlib.pyplot as plt
                plt.ion()
                window_start_min = window_end_min = None
                if query_time_range and "start" in query_time_range and "end" in query_time_range:
                    start_dt = datetime.fromtimestamp(float(query_time_range["start"]), tz=timezone.utc)
                    end_dt = datetime.fromtimestamp(float(query_time_range["end"]), tz=timezone.utc)
                    window_start_min = start_dt.hour * 60.0 + start_dt.minute + start_dt.second / 60.0
                    window_end_min = end_dt.hour * 60.0 + end_dt.minute + end_dt.second / 60.0
                live_viewer = [
                    LiveTreeViewer(
                        title=f"RCA — {task_part}",
                        mode="timeline",
                        save_path=str(task_save_dir / "live_timeline.png"),
                        window_start_min=window_start_min,
                        window_end_min=window_end_min,
                    ),
                    LiveTreeViewer(
                        title=f"RCA — {task_part}",
                        mode="tree",
                        save_path=str(task_save_dir / "live_tree.png"),
                    ),
                ]
            except ImportError:
                logger.warning("--live-view 사용하려면 matplotlib 필요: pip install matplotlib")

        expand_cfg = dataset_config.get("expand", {})
        expand_max_hops = expand_cfg.get("max_hops", 2)

        pipeline = StagedRCAPipeline(
            actions=actions,
            llm_configs=local_llm_configs,
            profile=profile,
            namespace=problem.namespace,
            save_dir=str(task_save_dir),
            time_range=query_time_range,
            sprint=sprint,
            problem=problem,                # enable controller-driven deep dive/expand
            expand_max_hops=expand_max_hops,
            live_viewer=live_viewer,
        )

        # SessionPrint log (session.log) should capture the full pipeline run
        orchestrator.session.start()
        start_t = time.time()
        prediction = pipeline.run(max_iterations=args.max_iterations)
        duration = time.time() - start_t
        orchestrator.session.end()

        submission = {
            "1": {
                "root cause component": prediction.get("component", ""),
                "root cause reason": prediction.get("reason", ""),
                "root cause occurrence datetime": prediction.get("datetime", ""),
            }
        }

        orchestrator.session.solution = submission

        results = problem.eval(submission, orchestrator.session.history, duration)
        score = results.get("score", 0)

        record = results.get("record", [])
        gt_fault = record[0] if record else {}
        detail = results.get("eval_detail", {})
        pred_time = detail.get("pred_time", prediction.get("datetime", ""))
        gt_time = gt_fault.get("datetime", "")

        time_diff_min = ""
        correct_time = ""
        if gt_time and pred_time:
            try:
                t1 = datetime.strptime(gt_time.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                t2 = datetime.strptime(pred_time.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                time_diff_min = round((t2 - t1).total_seconds() / 60.0, 1)
                correct_time = abs(time_diff_min) <= 1.0
            except ValueError:
                pass

        # Prefer API-accumulated tokens when evaluator's (session-based) count is 0
        in_tok = results.get("in_tokens")
        if in_tok is None or in_tok == "" or in_tok == 0:
            in_tok = local_llm_configs.get("_in_tokens")
        out_tok = results.get("out_tokens")
        if out_tok is None or out_tok == "" or out_tok == 0:
            out_tok = local_llm_configs.get("_out_tokens")

        append_score(scores_path, {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_id": eval_id,
            "problem_id": pid,
            "task_type": results.get("task_type", ""),
            "difficulty": results.get("difficulty", ""),
            "score": score,
            "success": results.get("success", ""),
            "steps": pipeline.tree._step,
            "TTA": round(duration, 2),
            "in_tokens": in_tok if in_tok is not None else "",
            "out_tokens": out_tok if out_tok is not None else "",
            "correct_component": detail.get("pred_component", "") == gt_fault.get("component", ""),
            "correct_reason": detail.get("pred_reason", "") == gt_fault.get("reason", ""),
            "correct_time": correct_time,
            "time_diff_min": time_diff_min,
            "pred_component": detail.get("pred_component", prediction.get("component", "")),
            "pred_reason": detail.get("pred_reason", prediction.get("reason", "")),
            "pred_time": pred_time,
            "gt_component": gt_fault.get("component", ""),
            "gt_reason": gt_fault.get("reason", ""),
            "gt_time": gt_fault.get("datetime", ""),
            "gt_level": gt_fault.get("level", ""),
        })

        print(f"\n[{pid}] Score: {score}")
        print(f"[{pid}] GT: {gt_fault.get('level','')}/{gt_fault.get('component','')} — {gt_fault.get('reason','')}")

        tree_path = task_save_dir / "tree.json"
        if not args.no_video and tree_path.exists():
            render_manim_video(str(tree_path), output_dir=str(task_save_dir / "media"))

        return {"score": score, "success": results.get("success", False)}

    except Exception as e:
        logger.error(f"Error running {pid}: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        orchestrator.session._save_dir_override = None
        if live_viewer is not None:
            viewers = live_viewer if isinstance(live_viewer, list) else [live_viewer]
            for v in viewers:
                v.close()
        if actions is not None:
            actions.cleanup()


def parse_args():
    parser = argparse.ArgumentParser(description="Tree-traversal RCA — Telecom")
    parser.add_argument("--problem", type=str)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Max Stage2-3 (deep-dive + expand) iterations per problem",
    )
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--api-config", type=str, default=None)
    parser.add_argument("--eval-id", type=str, default=None)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--live-view", action="store_true", help="Save live_timeline.png and live_tree.png during run")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument(
        "--use-prefiltered",
        type=Path,
        default=None,
        metavar="DIR",
        help="Use prefiltered telemetry from DIR (e.g. from scripts/prefilter_static_telemetry.py). Skips Docker deploy.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]
    api_config_path = args.api_config or str(
        Path(__file__).parent.parent / "openrca_rca" / "api_config.yaml"
    )
    llm_configs = load_config(api_config_path)

    prefiltered_dir = args.use_prefiltered.resolve() if args.use_prefiltered else None
    if prefiltered_dir is not None:
        logger.info(f"Using prefiltered telemetry from {prefiltered_dir} (no Docker)")
        if not prefiltered_dir.is_dir():
            logger.error(f"Prefiltered dir not found or not a directory: {prefiltered_dir}")
            sys.exit(1)

    if args.problem:
        if args.problem.strip().lower() == "all":
            if prefiltered_dir is not None:
                problem_ids = list_prefiltered_problem_ids(prefiltered_dir, DATASET)
            else:
                problem_ids = build_problem_ids(indices=None)
        else:
            problem_ids = [args.problem]
    elif args.all:
        if prefiltered_dir is not None:
            problem_ids = list_prefiltered_problem_ids(prefiltered_dir, DATASET)
        else:
            problem_ids = build_problem_ids(indices=None)
    else:
        problem_ids = build_problem_ids(indices=TARGET_INDICES)

    if args.start_index > 0:
        problem_ids = problem_ids[args.start_index:]

    logger.info(f"Running {len(problem_ids)} Telecom problems (tree-traversal) | eval_id={eval_id}")
    print(f"eval_id: {eval_id}")
    for pid in problem_ids:
        print(f"  - {pid}")
    print()

    total_score = 0
    completed = 0

    n_parallel = max(1, args.parallel)

    if n_parallel <= 1:
        for pid in problem_ids:
            print(f"\n{'=' * 60}\nProblem: {pid}\n{'=' * 60}\n")
            result = run_single_problem(
                pid, args, results_dir, eval_id, api_config_path, llm_configs, worker_id=0,
                prefiltered_dir=prefiltered_dir,
            )
            if result is not None:
                total_score += result["score"]
                completed += 1
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import queue

        print(f"\nRunning in parallel with {n_parallel} workers\n")

        worker_pool = queue.Queue()
        for wid in range(n_parallel):
            worker_pool.put(wid)

        def _run_with_worker(pid: str):
            wid = worker_pool.get()
            try:
                return run_single_problem(
                    pid, args, results_dir, eval_id, api_config_path, llm_configs, worker_id=wid,
                    prefiltered_dir=prefiltered_dir,
                )
            finally:
                worker_pool.put(wid)

        futures = {}
        with ThreadPoolExecutor(max_workers=n_parallel) as pool:
            for pid in problem_ids:
                future = pool.submit(_run_with_worker, pid)
                futures[future] = pid

            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    result = fut.result()
                    if result is not None:
                        total_score += result["score"]
                        completed += 1
                except Exception as e:
                    logger.error(f"Worker exception for {pid}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"Results saved under: {results_dir}\n{'=' * 60}")
