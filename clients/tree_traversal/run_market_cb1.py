"""Tree-traversal RCA runner — Market Cloudbed-1 dataset.

Runs StagedRCAPipeline (Localize → Deep Dive → Expand) with automatic
tree recording, SessionPrint logging, and optional Manim video generation.

Usage:
    python clients/tree_traversal/run_market_cb1.py
    python clients/tree_traversal/run_market_cb1.py --all
    python clients/tree_traversal/run_market_cb1.py --problem openrca_market_cb1-task_5-8
    python clients/tree_traversal/run_market_cb1.py --problem openrca_market_cb1-task_6-0
    python clients/tree_traversal/run_market_cb1.py --problem openrca_market_cb1-task_6-0 --no-controller-expand
    python clients/tree_traversal/run_market_cb1.py --no-video
    python clients/tree_traversal/run_market_cb1.py --parallel 2
    python clients/tree_traversal/run_market_cb1.py --use-prefiltered prefiltered_telemetry
    python clients/tree_traversal/run_market_cb1.py --problem openrca_market_cb1-task_6-0 --use-prefiltered prefiltered_telemetry --no-video --live-view
    python clients/tree_traversal/run_market_cb1.py --live-view --no-video --live-view --parallel 3 --eval-id kpi-expand-v1
"""

import argparse
import csv
import json
import logging
import os
import re
import shutil
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_tree_market_cb1")

DATASET = "openrca_market_cb1"
MAX_STEPS = 60

# 10 single-fault problems with diverse reasons/components/levels
TARGET_INDICES = [0, 1, 2, 4, 5, 8, 13, 15, 20, 32]
# TARGET_INDICES = [0, 1, 2, 4, 5, 8, 13, 15, 18, 19]
# TARGET_INDICES = [18, 19, 20, 21, 24, 25, 26, 29, 32, 33]

SCORE_FIELDS = [
    "timestamp", "eval_id", "model", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
    "correct_component", "correct_reason", "correct_time", "time_diff_min",
    "pred_component", "pred_reason", "pred_time",
    "gt_component", "gt_reason", "gt_time",
    "gt_level",
]


def extract_dataset_key(problem_id: str) -> str:
    return problem_id.rsplit("-", 2)[0]


def prefiltered_dataset_dirnames(dataset_key: str) -> list[str]:
    """Return acceptable prefiltered directory names for this runner."""
    aliases = [dataset_key]
    if dataset_key == "openrca_market_cb1":
        aliases.append("openrca_market_cloudbed1")
    return aliases


def _prefiltered_task_sort_key(task_name: str) -> tuple[int, int, str]:
    """Sort task_6-0 before task_6-10, and unknown names last."""
    text = str(task_name or "").strip()
    m = re.match(r"^task_(\d+)-(\d+)$", text)
    if not m:
        return (10**9, 10**9, text)
    return (int(m.group(1)), int(m.group(2)), text)


def list_prefiltered_problem_ids(
    prefiltered_root: Path,
    dataset_key: str,
    *,
    dataset_dirnames: list[str] | None = None,
) -> list[str]:
    """Return problem ids backed by existing prefiltered task directories."""
    root = prefiltered_root.resolve()
    dirnames = dataset_dirnames or [dataset_key]
    task_names: set[str] = set()
    for dirname in dirnames:
        ds_dir = root / dirname
        if not ds_dir.is_dir():
            continue
        for child in ds_dir.iterdir():
            if child.is_dir() and child.name.startswith("task_"):
                task_names.add(child.name)
    return [f"{dataset_key}-{task}" for task in sorted(task_names, key=_prefiltered_task_sort_key)]


_scores_lock = threading.Lock()


def append_score(scores_path: Path, row: dict):
    with _scores_lock:
        is_new = not scores_path.exists()
        with open(scores_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)


def snapshot_vision_artifacts(task_save_dir: Path) -> Path:
    """Snapshot generated vision images under task_save_dir/vision with a manifest."""
    vision_dir = task_save_dir / "vision"
    vision_dir.mkdir(parents=True, exist_ok=True)

    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    entries: list[dict] = []
    seen_targets: set[str] = set()

    for src in sorted(task_save_dir.rglob("*")):
        if not src.is_file():
            continue
        if vision_dir in src.parents:
            continue
        if src.suffix.lower() not in image_exts:
            continue

        rel = src.relative_to(task_save_dir)
        target_name = "__".join(rel.parts)
        if not target_name:
            continue
        if target_name in seen_targets:
            base = Path(target_name).stem
            ext = Path(target_name).suffix
            i = 2
            while f"{base}__{i}{ext}" in seen_targets:
                i += 1
            target_name = f"{base}__{i}{ext}"
        seen_targets.add(target_name)

        dst = vision_dir / target_name
        shutil.copy2(src, dst)
        entries.append(
            {
                "source": str(rel),
                "snapshot": str(dst.relative_to(task_save_dir)),
                "size_bytes": src.stat().st_size,
            }
        )

    manifest_path = vision_dir / "vision_manifest.json"
    payload = {
        "task_dir": str(task_save_dir),
        "image_count": len(entries),
        "images": entries,
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def build_problem_ids(indices: list[int] | None = None) -> list[str]:
    import pandas as pd
    base = Path(__file__).parent.parent.parent
    query_csv = base / "aiopslab-applications/static_dataset/openrca/Market/cloudbed-1/query.csv"
    df = pd.read_csv(query_csv)
    if indices is None:
        indices = list(range(len(df)))
    pids = []
    for idx in indices:
        task_type = df.iloc[idx]["task_index"]
        pids.append(f"{DATASET}-{task_type}-{idx}")
    return pids


def render_manim_video(tree_json_path: str, output_dir: str | None = None):
    """Render Manim animation from tree.json. Uses absolute paths so it works from any cwd.

    Output locations (when output_dir is set, e.g. <task_save_dir>/media):
      - After success: <output_dir>/tree_animation.mp4 (copy of the rendered mp4)
      - Manim native:  <output_dir>/videos/visualize_rca_tree/480p15/RCATreeAnimation.mp4
    If no video appears, Manim may have failed during the scene; check stderr in logs
    or run manually: TREE_JSON=<path> manim -ql clients/tree_traversal/visualize_rca_tree.py RCATreeAnimation --media_dir <output_dir>
    """
    tree_abs = str(Path(tree_json_path).resolve())
    out_abs = str(Path(output_dir).resolve()) if output_dir else None

    script_path = str(Path(__file__).parent.resolve() / "visualize_rca_tree.py")
    cmd = ["manim", "-ql", script_path, "RCATreeAnimation"]
    if out_abs:
        cmd.extend(["--media_dir", out_abs])

    env = {**os.environ, "TREE_JSON": tree_abs}
    logger.info(f"Rendering Manim video: TREE_JSON={tree_abs}")
    if out_abs:
        logger.info(f"  Video will be saved to: {out_abs}/tree_animation.mp4 (and under {out_abs}/videos/...)")
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120, cwd=Path(__file__).parent.parent.parent)
        if result.returncode == 0:
            logger.info("Manim video rendered successfully")
            # Manim writes to media_dir/videos/<script_name>/<quality>/*.mp4; find and copy to predictable path
            if out_abs:
                out_path = Path(out_abs)
                found = False
                for p in out_path.rglob("*.mp4"):
                    logger.info(f"  → found: {p}")
                    try:
                        dest = out_path / "tree_animation.mp4"
                        shutil.copy2(p, dest)
                        logger.info(f"  → copied to {dest}")
                        found = True
                    except Exception as e:
                        logger.warning(f"Copy mp4 to tree_animation.mp4: {e}")
                    break
                if not found:
                    logger.warning(
                        f"Manim reported success but no .mp4 found under {out_abs}. "
                        f"Check {out_abs}/videos/visualize_rca_tree/480p15/ for RCATreeAnimation.mp4 or run "
                        "manim manually to see scene errors."
                    )
            else:
                for line in result.stdout.splitlines():
                    if ".mp4" in line:
                        logger.info(f"  → {line.strip()}")
        else:
            logger.warning(f"Manim render failed (rc={result.returncode})")
            if result.stderr:
                logger.warning(result.stderr[:2000])
                if out_abs:
                    stderr_file = Path(out_abs).parent / "manim_stderr.txt"
                    try:
                        stderr_file.write_text(result.stderr, encoding="utf-8")
                        logger.warning(f"Full stderr written to: {stderr_file}")
                    except Exception as e:
                        logger.warning(f"Could not write manim_stderr.txt: {e}")
            if result.stdout and "Error" in result.stdout:
                logger.warning("Manim stdout (excerpt): %s", result.stdout[-1500:])
    except FileNotFoundError:
        logger.warning("manim not found. Install with: pip install manim")
    except subprocess.TimeoutExpired:
        logger.warning("Manim rendering timed out (120s)")


def run_single_problem(
    pid: str,
    args,
    results_dir: Path,
    eval_id: str,
    api_config_path: str,
    llm_configs: dict,
    worker_id: int = 0,
) -> dict | None:
    task_save_dir: Path | None = None
    if args.use_prefiltered is not None:
        condition = None
    else:
        condition = f"w{worker_id}" if args.parallel > 1 else None
    dataset_key = extract_dataset_key(pid)

    # Make a shallow copy of llm_configs so that per-problem token usage
    # (configs['_in_tokens'], configs['_out_tokens']) does not leak across problems.
    local_llm_configs = dict(llm_configs)
    local_llm_configs["_in_tokens"] = 0
    local_llm_configs["_out_tokens"] = 0

    orchestrator = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)
    # Pipeline acts as the agent; set name/model for session tracking
    orchestrator.agent = None
    orchestrator.agent_name = "tree-traversal"
    orchestrator.model_name = local_llm_configs.get("MODEL", "unknown")

    sprint = orchestrator.sprint
    actions: StaticRCAActionsWithExecutor | None = None
    live_viewer = None

    try:
        problem_desc, instructs, apis = orchestrator.init_problem(
            pid,
            condition=condition,
            skip_deploy=(args.use_prefiltered is not None),
        )
        problem = orchestrator.session.problem

        eval_dir = orchestrator.session.get_save_dir()
        scores_path = eval_dir / "scores.csv"
        task_part = pid.split('-', maxsplit=1)[1] if '-' in pid else pid
        task_save_dir = eval_dir / task_part
        task_save_dir.mkdir(parents=True, exist_ok=True)
        orchestrator.session._save_dir_override = task_save_dir

        # ── Initialize SessionPrint log ───────────────────────────────
        log_filepath = task_save_dir / "session.log"
        sprint.init_log_file(str(log_filepath))
        sprint.service_detail(f"Session log path: {log_filepath}")
        sprint.problem_init(problem_desc, instructs, apis)

        dataset_config = problem.app.dataset_config
        use_executor = dataset_config.get("executor", {}).get("enable", True)
        basic_prompt = get_basic_prompt(dataset_key)

        if args.use_prefiltered is not None:
            pid_suffix = pid.split("-", maxsplit=1)[1] if "-" in pid else pid
            legacy_task_id = getattr(problem, "task_type", None) or getattr(problem.app.query_info, "task_id", None)
            candidate_paths = []
            for dirname in prefiltered_dataset_dirnames(dataset_key):
                dataset_prefiltered_dir = args.use_prefiltered.resolve() / dirname
                candidate_paths.append(dataset_prefiltered_dir / pid_suffix)
            if legacy_task_id:
                for dirname in prefiltered_dataset_dirnames(dataset_key):
                    dataset_prefiltered_dir = args.use_prefiltered.resolve() / dirname
                    candidate_paths.append(dataset_prefiltered_dir / legacy_task_id)
            candidate_paths.append(args.use_prefiltered.resolve() / pid_suffix)
            if legacy_task_id:
                candidate_paths.append(args.use_prefiltered.resolve() / legacy_task_id)
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
            use_hypothesis=False,  # pipeline handles hypothesis logic
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

        # ── Build DatasetProfile ──────────────────────────────────────
        config_path = Path(
            "aiopslab/service/apps/static_dataset/config/openrca_market_cloudbed1.json"
        )
        profile = build_profile(
            dataset_key,
            config_path=str(config_path) if config_path.exists() else None,
        )

        # ── 실시간 그래프 뷰어 (on-process) ─────────────────────────────
        live_viewer = None
        if args.live_view:
            try:
                import matplotlib.pyplot as plt
                plt.ion()
                window_start_min = window_end_min = None
                if query_time_range and "start" in query_time_range and "end" in query_time_range:
                    # 쿼리 윈도우의 시각은 epoch 초이지만,
                    # 타임라인에서는 날짜를 무시하고 하루 중 시각(HH:MM)만 사용.
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

        # ── Run StagedRCAPipeline ─────────────────────────────────────
        pipeline = StagedRCAPipeline(
            actions=actions,
            llm_configs=local_llm_configs,
            profile=profile,
            namespace=problem.namespace,
            save_dir=str(task_save_dir),
            time_range=query_time_range,
            sprint=sprint,
            problem=problem,
            render_localization_timeline=args.timeline,
            expand_max_hops=1,
            use_controller_deep_dive=not args.no_controller_deep_dive,
            use_controller_expand=not args.no_controller_expand,
            live_viewer=live_viewer,
        )

        orchestrator.session.start()
        start_t = time.time()
        prediction = pipeline.run(max_iterations=args.max_iterations)
        duration = time.time() - start_t
        orchestrator.session.end()

        sprint.agent(
            f"Pipeline prediction: component={prediction['component']}, "
            f"reason={prediction['reason']}, time={prediction['datetime']}"
        )

        # ── Submit & Evaluate ─────────────────────────────────────────
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
        sprint.result(results)

        # ── Record Scores ─────────────────────────────────────────────
        record = results.get("record", [])
        gt_fault = record[0] if record else {}
        gt_component = gt_fault.get("component", "")
        gt_reason = gt_fault.get("reason", "")
        gt_time = gt_fault.get("datetime", "")
        gt_level = gt_fault.get("level", "")

        detail = results.get("eval_detail", {})
        pred_component = detail.get("pred_component", prediction.get("component", ""))
        pred_reason = detail.get("pred_reason", prediction.get("reason", ""))
        pred_time = detail.get("pred_time", prediction.get("datetime", ""))

        correct_component = pred_component == gt_component if gt_component else ""
        correct_reason = pred_reason == gt_reason if gt_reason else ""

        time_diff_min = ""
        correct_time = ""
        if gt_time and pred_time:
            try:
                from datetime import datetime as _dt
                t1 = _dt.strptime(gt_time.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                t2 = _dt.strptime(pred_time.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
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
            "model": local_llm_configs.get("MODEL", ""),
            "problem_id": pid,
            "task_type": results.get("task_type", ""),
            "difficulty": results.get("difficulty", ""),
            "score": score,
            "success": results.get("success", ""),
            "steps": pipeline.tree._step,
            "TTA": round(duration, 2),
            "in_tokens": in_tok if in_tok is not None else "",
            "out_tokens": out_tok if out_tok is not None else "",
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
        })

        print(f"\n[{pid}] Score: {score}")
        print(f"[{pid}] Prediction: {pred_component} / {pred_reason} / {pred_time}")
        print(f"[{pid}] GT: {gt_level}/{gt_component} — {gt_reason}")

        # ── Render Manim Video ────────────────────────────────────────
        tree_path = task_save_dir / "tree.json"
        if not args.no_video and tree_path.exists():
            render_manim_video(str(tree_path), output_dir=str(task_save_dir / "media"))

        # ── Snapshot Vision Artifacts ─────────────────────────────────
        manifest_path = snapshot_vision_artifacts(task_save_dir)
        sprint.service_detail(f"Vision snapshot manifest: {manifest_path}")

        return {"score": score, "success": results.get("success", False)}

    except Exception as e:
        logger.error(f"Error running {pid}: {e}")
        import traceback
        traceback.print_exc()
        if task_save_dir is not None and task_save_dir.exists():
            try:
                manifest_path = snapshot_vision_artifacts(task_save_dir)
                sprint.service_detail(
                    f"[Exception path] Vision snapshot manifest: {manifest_path}"
                )
            except Exception as snapshot_err:
                logger.warning(f"Failed to snapshot vision artifacts for {pid}: {snapshot_err}")
        return None
    finally:
        sprint.close_log_file()
        if live_viewer is not None:
            viewers = live_viewer if isinstance(live_viewer, list) else [live_viewer]
            for v in viewers:
                v.close()
        if orchestrator.session is not None:
            orchestrator.session._save_dir_override = None
        if actions is not None:
            actions.cleanup()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Tree-traversal RCA — Market Cloudbed-1"
    )
    parser.add_argument("--problem", type=str, help="Single problem ID")
    parser.add_argument("--all", action="store_true",
                        help="Run all problems (not just TARGET_INDICES)")
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
    parser.add_argument("--no-video", action="store_true",
                        help="Skip Manim video rendering")
    parser.add_argument("--timeline", action="store_true",
                        help="Render localization timeline (Manim, x-axis=time) after stage1")
    parser.add_argument("--no-controller-deep-dive", action="store_true",
                        help="Disable controller-driven deep dive (use fixed execute×reason pipeline)")
    parser.add_argument("--no-controller-expand", action="store_true",
                        help="Disable controller-driven expand and use graph-only expand")
    parser.add_argument("--live-view", action="store_true",
                        help="실시간(on-process) matplotlib 창으로 그래프 갱신 (x=시간 타임라인)")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument(
        "--use-prefiltered",
        type=Path,
        default=None,
        help="Read telemetry from prefiltered dir instead of Docker/runtime process_telemetry",
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
    if prefiltered_dir is not None and not prefiltered_dir.is_dir():
        raise SystemExit(f"Prefiltered dir not found or not a directory: {prefiltered_dir}")

    if args.problem:
        if args.problem.strip().lower() == "all":
            if prefiltered_dir is not None:
                problem_ids = list_prefiltered_problem_ids(
                    prefiltered_dir,
                    DATASET,
                    dataset_dirnames=prefiltered_dataset_dirnames(DATASET),
                )
            else:
                problem_ids = build_problem_ids(indices=None)
        else:
            problem_ids = [args.problem]
    elif args.all:
        if prefiltered_dir is not None:
            problem_ids = list_prefiltered_problem_ids(
                prefiltered_dir,
                DATASET,
                dataset_dirnames=prefiltered_dataset_dirnames(DATASET),
            )
        else:
            problem_ids = build_problem_ids(indices=None)
    else:
        problem_ids = build_problem_ids(indices=TARGET_INDICES)

    if args.start_index > 0:
        problem_ids = problem_ids[args.start_index:]

    n_parallel = max(1, args.parallel)
    logger.info(
        f"Running {len(problem_ids)} Market CB1 problems (tree-traversal) | "
        f"eval_id={eval_id} | parallel={n_parallel} | "
        f"max_iterations={args.max_iterations}"
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
                pid, args, results_dir, eval_id, api_config_path, llm_configs,
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
                    pid, args, results_dir, eval_id, api_config_path, llm_configs,
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
