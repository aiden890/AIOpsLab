"""Tree-traversal RCA runner — Bank dataset.

Usage:
    python clients/tree_traversal/run_bank.py
    python clients/tree_traversal/run_bank.py --all
    python clients/tree_traversal/run_bank.py --problem openrca_bank-task_1-0
    python clients/tree_traversal/run_bank.py --no-video
    python clients/tree_traversal/run_bank.py --live-view
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
from clients.openrca_rca.api_router import load_config

from clients.tree_traversal.dataset_profile import build_profile
from clients.tree_traversal.staged_rca_pipeline import StagedRCAPipeline
from clients.tree_traversal.live_tree_viewer import LiveTreeViewer
from clients.tree_traversal.run_market_cb1 import (
    append_score, render_manim_video, SCORE_FIELDS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_tree_bank")

DATASET = "openrca_bank"
MAX_STEPS = 60

# Diverse problems: apache, Tomcat, MG, IG, Mysql, Redis ×
# high CPU, high memory, network latency/loss, disk I/O, disk space, JVM CPU, JVM OOM
TARGET_INDICES = [0, 10, 30, 60, 100, 150, 200, 250, 300, 380]


def extract_dataset_key(problem_id: str) -> str:
    return problem_id.rsplit("-", 2)[0]


def build_problem_ids(indices: list[int] | None = None) -> list[str]:
    import pandas as pd
    base = Path(__file__).parent.parent.parent
    query_csv = base / "aiopslab-applications/static_dataset/openrca/Bank/query.csv"
    df = pd.read_csv(query_csv)
    if indices is None:
        indices = list(range(len(df)))
    pids = []
    for idx in indices:
        task_type = df.iloc[idx]["task_index"]
        pids.append(f"{DATASET}-{task_type}-{idx}")
    return pids


def run_single_problem(
    pid: str, args, results_dir: Path, eval_id: str,
    api_config_path: str, llm_configs: dict,
) -> dict | None:
    dataset_key = extract_dataset_key(pid)

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)
    orchestrator.register_agent(None, name="tree-traversal")

    actions: StaticRCAActionsWithExecutor | None = None
    live_viewer = None

    try:
        problem_desc, instructs, apis = orchestrator.init_problem(pid)
        problem = orchestrator.session.problem

        eval_dir = orchestrator.session.get_save_dir()
        scores_path = eval_dir / "scores.csv"
        task_part = pid.split('-', maxsplit=1)[1] if '-' in pid else pid
        task_save_dir = eval_dir / task_part
        task_save_dir.mkdir(parents=True, exist_ok=True)
        orchestrator.session._save_dir_override = task_save_dir

        dataset_config = problem.app.dataset_config
        use_executor = dataset_config.get("executor", {}).get("enable", True)
        basic_prompt = get_basic_prompt(dataset_key)

        actions = StaticRCAActionsWithExecutor(
            container_name=problem.app.get_container_name(),
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

        config_path = "aiopslab/service/apps/static_dataset/config/openrca_bank.json"
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

        pipeline = StagedRCAPipeline(
            actions=actions,
            llm_configs=llm_configs,
            profile=profile,
            namespace=problem.namespace,
            save_dir=str(task_save_dir),
            time_range=query_time_range,
            live_viewer=live_viewer,
        )

        start_t = time.time()
        prediction = pipeline.run(max_iterations=args.max_iterations)
        duration = time.time() - start_t

        submission = {
            "1": {
                "root cause component": prediction.get("component", ""),
                "root cause reason": prediction.get("reason", ""),
                "root cause occurrence datetime": prediction.get("datetime", ""),
            }
        }

        orchestrator.session.solution = submission
        orchestrator.session.start()
        orchestrator.session.end()

        results = problem.eval(submission, orchestrator.session.history, duration)
        score = results.get("score", 0)

        record = results.get("record", [])
        gt_fault = record[0] if record else {}
        detail = results.get("eval_detail", {})

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
            "correct_component": detail.get("pred_component", "") == gt_fault.get("component", ""),
            "correct_reason": detail.get("pred_reason", "") == gt_fault.get("reason", ""),
            "correct_time": "",
            "time_diff_min": "",
            "pred_component": detail.get("pred_component", prediction.get("component", "")),
            "pred_reason": detail.get("pred_reason", prediction.get("reason", "")),
            "pred_time": detail.get("pred_time", prediction.get("datetime", "")),
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
    parser = argparse.ArgumentParser(description="Tree-traversal RCA — Bank")
    parser.add_argument("--problem", type=str)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--api-config", type=str, default=None)
    parser.add_argument("--eval-id", type=str, default=None)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--live-view", action="store_true", help="Save live_timeline.png and live_tree.png during run")
    parser.add_argument("--parallel", type=int, default=1)
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

    if args.problem:
        problem_ids = [args.problem]
    elif args.all:
        problem_ids = build_problem_ids(indices=None)
    else:
        problem_ids = build_problem_ids(indices=TARGET_INDICES)

    if args.start_index > 0:
        problem_ids = problem_ids[args.start_index:]

    logger.info(f"Running {len(problem_ids)} Bank problems (tree-traversal) | eval_id={eval_id}")
    print(f"eval_id: {eval_id}")
    for pid in problem_ids:
        print(f"  - {pid}")
    print()

    total_score = 0
    completed = 0

    for pid in problem_ids:
        print(f"\n{'=' * 60}\nProblem: {pid}\n{'=' * 60}\n")
        result = run_single_problem(pid, args, results_dir, eval_id, api_config_path, llm_configs)
        if result is not None:
            total_score += result["score"]
            completed += 1

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"Results saved under: {results_dir}\n{'=' * 60}")
