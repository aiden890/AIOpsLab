"""Runner for KG + RAG RCA Agent.

Builds a Trace Knowledge Graph from telemetry before the agent starts,
serializes it to text, and injects it as context into the agent's system prompt.

Usage:
    # Single problem (Bank)
    python clients/run_kg_rca.py --problem openrca_bank-task_1-0

    # All Bank problems
    python clients/run_kg_rca.py --dataset openrca_bank

    # Telecom
    python clients/run_kg_rca.py --dataset openrca_telecom

    # Choose serialization format
    python clients/run_kg_rca.py --problem openrca_bank-task_1-0 --kg-format sections

    # Choose top-K for KG
    python clients/run_kg_rca.py --problem openrca_bank-task_1-0 --kg-top-k 10
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
from aiopslab.orchestrator.static_actions.kg import (
    build_fault_timeline, format_for_instruction, format_for_result, format_for_summary,
    format_for_metrics, format_for_traces,
)
from clients.openrca_rca.kg_rca_agent import KGRCAAgent
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_kg_rca")

MAX_STEPS = 60

SCORE_FIELDS = [
    "timestamp", "eval_id", "model", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
    "correct_component", "correct_reason", "correct_time", "time_diff_min",
    "pred_component", "pred_reason", "pred_time",
    "gt_component", "gt_reason", "gt_time",
    "kg_format", "kg_top_k",
]


def extract_dataset_key(problem_id: str) -> str:
    """'openrca_bank-task_1-0' → 'openrca_bank'"""
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


def _no_op_extract_components(_instruction: str, _dataset_type: str) -> list:
    """Stub: no component extraction (trace/metric KG builders removed)."""
    return []


def parse_args():
    parser = argparse.ArgumentParser(
        description="KG + RAG RCA Agent runner"
    )
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset")
    parser.add_argument("--task-type", type=str, help="Filter by task type")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument("--results-dir", type=str, default="results/static_problems")
    parser.add_argument("--api-config", type=str, default=None)
    parser.add_argument("--eval-id", type=str, default=None)
    parser.add_argument("--start-index", type=int, default=0)
    # KG-specific args
    parser.add_argument("--kg-format", type=str, default="sections",
                        choices=["sections", "json", "markdown", "natural"],
                        help="Trace KG serialization format")
    parser.add_argument("--kg-top-k", type=int, default=5,
                        help="Top-K anomalous edges to include in KG")
    # Metric RAG args
    parser.add_argument("--rag-detail", type=str, default="short",
                        choices=["short", "full"],
                        help="Metric RAG detail: 'short' = KPI names only, 'full' = all stats")
    # Parallel execution
    parser.add_argument("--parallel", type=int, default=1,
                        help="Number of problems to run in parallel (default 1 = sequential)")
    return parser.parse_args()


def run_single_problem(pid: str, args, results_dir: Path, eval_id: str,
                       api_config_path: str,
                       worker_id: int = 0) -> dict:
    """Run a single problem. Returns {"score": float, "success": bool} or None on error.

    Each worker gets a unique condition suffix so Docker containers don't collide.
    """
    condition = f"w{worker_id}" if args.parallel > 1 else None

    dataset_key = extract_dataset_key(pid)
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

        # per-task dir: .../eval_id/task_2-0/
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

        # =============================================================
        # Build Fault Timeline + wire executor pipeline injectors
        # =============================================================
        if use_executor:
            _sprint = orchestrator.sprint
            _dt = dataset_key.replace("openrca_", "")
            metric_df = actions.static_app.fetch_metrics_df(
                problem.namespace,
                start_time=query_time_range.get("start") if query_time_range else None,
                end_time=query_time_range.get("end") if query_time_range else None,
            )
            trace_df_for_rag = actions.static_app.fetch_traces_df(
                problem.namespace,
                start_time=query_time_range.get("start") if query_time_range else None,
                end_time=query_time_range.get("end") if query_time_range else None,
            )
            fault_timeline = build_fault_timeline(
                metric_df=metric_df,
                trace_df=trace_df_for_rag,
                time_range=query_time_range,
                all_components=all_components,
                dataset_type=_dt,
                bucket_minutes=5,
            )
            n_buckets_with_anomalies = sum(
                1 for b in fault_timeline.bucket_anomalies if b.anomalous_components
            )
            _tl_summary = (
                f"Fault timeline built: window={fault_timeline.fault_window.source} "
                f"({fault_timeline.fault_window.start:.0f}~{fault_timeline.fault_window.end:.0f}), "
                f"profiles={len(fault_timeline.component_profiles)}, "
                f"top_anomalous={fault_timeline.top_anomalous[:5]}, "
                f"buckets={len(fault_timeline.bucket_anomalies)} "
                f"({n_buckets_with_anomalies} with anomalies)"
            )
            logger.info(_tl_summary)
            _sprint._log(
                f"\n{'='*60}\n"
                f"[TIMELINE] {_tl_summary}\n"
                f"{'='*60}"
            )

            # ① RAG injector: identity (metric KG removed); timeline injector adds fault-window data
            _orig_rag = lambda x: x
            def _timeline_rag_injector(instruction: str, _orig=_orig_rag, _tl=fault_timeline, _dt2=_dt, _sp=_sprint) -> str:
                instruction = _orig(instruction)
                comps = _no_op_extract_components(instruction, _dt2)
                if not comps:
                    return instruction
                ctx = format_for_instruction(_tl, comps)
                if ctx:
                    _sp._log(
                        f"\n{'='*60}\n"
                        f"[TIMELINE] Fault window data injected for: {', '.join(comps)}\n"
                        f"{'='*60}"
                    )
                    return f"{instruction}\n\n{ctx}"
                return instruction

            actions.set_rag_injector(_timeline_rag_injector)

            # ③ Summary injector (before executor LLM summary)
            def _timeline_summary_injector(result: str, _tl=fault_timeline, _dt2=_dt, _sp=_sprint) -> str:
                comps = _no_op_extract_components(result, _dt2)
                if not comps:
                    return result
                ctx = format_for_summary(_tl, comps)
                if ctx:
                    _sp._log(
                        f"\n{'='*60}\n"
                        f"[TIMELINE] Summary injected for: {', '.join(comps)}\n"
                        f"{'='*60}\n"
                        f"{ctx}\n"
                        f"{'='*60}"
                    )
                    return f"{result}\n\n{ctx}"
                return result

            actions.set_summary_injector(_timeline_summary_injector)

            # ② Result enricher (result → controller)
            def _timeline_result_enricher(result: str, _tl=fault_timeline, _dt2=_dt, _sp=_sprint) -> str:
                comps = _no_op_extract_components(result, _dt2)
                if not comps:
                    return result
                ctx = format_for_result(_tl, comps)
                if ctx:
                    _sp._log(
                        f"\n{'='*60}\n"
                        f"[TIMELINE] Fault-window peer context for: {', '.join(comps)}\n"
                        f"{'='*60}"
                    )
                    return f"{result}\n\n{ctx}"
                return result

            actions.set_result_enricher(_timeline_result_enricher)

            # ④ Metrics enricher (get_metrics result에 이상치 요약 추가)
            # NOTE: temporarily disabled — vision critic handles anomaly detection
            # def _metrics_enricher(result: str, _tl=fault_timeline, _sp=_sprint) -> str:
            #     ctx = format_for_metrics(_tl)
            #     if ctx:
            #         _sp._log(
            #             f"\n{'='*60}\n"
            #             f"[METRICS] Anomaly summary injected into get_metrics result\n"
            #             f"{'='*60}\n"
            #             f"{ctx}\n"
            #             f"{'='*60}"
            #         )
            #         return f"{result}\n\n{ctx}"
            #     else:
            #         n_anom_buckets = sum(1 for b in _tl.bucket_anomalies if b.anomalous_components)
            #         _sp._log(
            #             f"\n{'='*60}\n"
            #             f"[METRICS] Enricher called but no anomaly summary generated "
            #             f"(profiles={len(_tl.component_profiles)}, top_anomalous={len(_tl.top_anomalous)}, "
            #             f"buckets={len(_tl.bucket_anomalies)}, anomalous_buckets={n_anom_buckets})\n"
            #             f"{'='*60}"
            #         )
            #     return result
            #
            # actions.set_metrics_enricher(_metrics_enricher)

            # ⑤ Traces enricher — disabled (controller no longer calls get_traces directly)
            # actions.set_traces_enricher(_traces_enricher)

        agent.init_context(
            problem_desc, instructs, apis,
            possible_rca=possible_rca,
        )

        orchestrator._system_message = agent.history[0]["content"]
        orchestrator.session.extra["executor_trajectory"] = actions._executor_trajectory
        orchestrator.sprint.problem_init(problem_desc, instructs, apis)

        full_output = asyncio.run(
            orchestrator.start_problem(max_steps=args.max_steps)
        )
        results = full_output.get("results", {})

        score = results.get("score", 0)

        # --- Per-field evaluation from record.csv ground truth ---
        record = results.get("record", [])
        gt_fault = record[0] if record else {}
        gt_component = gt_fault.get("component", "")
        gt_reason = gt_fault.get("reason", "")
        gt_time = gt_fault.get("datetime", "")

        # Extract prediction from eval_detail (parsed from agent submission)
        detail = results.get("eval_detail", {})
        pred_component = detail.get("pred_component", "")
        pred_reason = detail.get("pred_reason", "")
        pred_time = detail.get("pred_time", "")

        correct_component = pred_component == gt_component if gt_component else ""
        correct_reason = pred_reason == gt_reason if gt_reason else ""

        # Time comparison
        time_diff_min = ""
        correct_time = ""
        if gt_time and pred_time:
            try:
                from datetime import datetime as _dt
                t1 = _dt.strptime(gt_time.strip(), "%Y-%m-%d %H:%M:%S")
                t2 = _dt.strptime(pred_time.strip(), "%Y-%m-%d %H:%M:%S")
                # signed: positive = pred is after GT, negative = pred is before GT
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
            "kg_format": args.kg_format,
            "kg_top_k": args.kg_top_k,
        })

        print(f"\n[{pid}] Score: {score}")
        print(f"[{pid}] Passing: {results.get('passing_criteria', 'N/A')}")
        print(f"[{pid}] Failing: {results.get('failing_criteria', 'N/A')}")

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


if __name__ == "__main__":
    args = parse_args()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]

    api_config_path = args.api_config or str(
        Path(__file__).parent / "openrca_rca" / "api_config.yaml"
    )

    # Get problem list (use a temporary orchestrator just for listing)
    _tmp_orch = StaticOrchestrator(results_dir=str(results_dir), eval_id=eval_id)
    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = _tmp_orch.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    if args.start_index > 0:
        logger.info(f"Skipping first {args.start_index} problems")
        problem_ids = problem_ids[args.start_index:]

    n_parallel = max(1, args.parallel)
    logger.info(
        f"Running {len(problem_ids)} problems | eval_id={eval_id} | "
        f"parallel={n_parallel} | kg_format={args.kg_format} | kg_top_k={args.kg_top_k}"
    )

    total_score = 0
    completed = 0

    if n_parallel <= 1:
        # Sequential (original behavior)
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
        # Parallel execution
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import queue

        # Worker ID pool — each concurrent task gets a unique ID,
        # returned to the pool when done so the next task reuses it.
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
    print(f"KG format: {args.kg_format}, top_k: {args.kg_top_k}")
    print(f"Results saved under: {results_dir}")
    print(f"{'=' * 60}")
