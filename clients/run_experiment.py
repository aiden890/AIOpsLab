"""Experiment runner for RCA agents.

Runs a list of problems from a file, supports both original and critic agents,
and saves unified scores with per-dataset and per-dimension (t/c/r) statistics.

Usage:
    # Critic agent
    python clients/run_experiment.py \
        --problems-file clients/problems_30.txt \
        --agent critic \
        --api-config clients/openrca_rca/api_config_low.yaml \
        --eval-id gpt5-low-critic

    # Original agent
    python clients/run_experiment.py \
        --problems-file clients/problems_30.txt \
        --agent original \
        --api-config clients/openrca_rca/api_config.yaml \
        --eval-id gpt5-original

Results structure:
    results/experiments/{eval_id}/
        scores.csv              # unified scores for all problems
        summary.txt             # statistics summary (dataset, t/c/r, overall)
        openrca_telecom/...     # per-dataset logs, json, notebooks
        openrca_bank/...
        openrca_market_cb1/...
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import wandb

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.orchestrator.static_actions.rca_executor import StaticRCAActionsWithExecutor
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide, build_telemetry_guide

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_experiment")

MAX_STEPS = 40

SCORE_FIELDS = [
    "timestamp", "eval_id", "problem_id", "dataset",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
    "passing_criteria", "failing_criteria", "ground_truth",
]


def extract_dataset_key(problem_id: str) -> str:
    """'openrca_bank-task_1-0' -> 'openrca_bank'"""
    return problem_id.rsplit("-", 2)[0]


def load_problems_file(filepath: str) -> list[str]:
    with open(filepath) as f:
        return [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def create_agent(agent_type: str, api_config_path: str):
    if agent_type == "critic":
        from clients.openrca_rca.react_rca_critic_agent import ReactRCACriticAgent
        return ReactRCACriticAgent(api_config_path=api_config_path)
    elif agent_type == "react":
        from clients.openrca_rca.react_rca_agent import ReactRCAAgent
        return ReactRCAAgent(api_config_path=api_config_path)
    elif agent_type == "deepdive1":
        from clients.agents.deepdive1.agent import DeepDiveAgent1
        return DeepDiveAgent1(api_config_path=api_config_path)
    elif agent_type == "deepdive-hypothesis":
        from clients.agents.deepdive_hypothesis.agent import DeepDiveAgentHypothesis
        return DeepDiveAgentHypothesis(api_config_path=api_config_path)
    elif agent_type == "deepdive-baseline":
        from clients.agents.deepdive_baseline.agent import DeepDiveAgentBaseline
        return DeepDiveAgentBaseline(api_config_path=api_config_path)
    elif agent_type == "deepdive-evidence":
        from clients.agents.deepdive_evidence.agent import DeepDiveAgentEvidence
        return DeepDiveAgentEvidence(api_config_path=api_config_path)
    elif agent_type == "deepdive-multisignal":
        from clients.agents.deepdive_multisignal.agent import DeepDiveAgentMultiSignal
        return DeepDiveAgentMultiSignal(api_config_path=api_config_path)
    elif agent_type == "deepdive-robust":
        from clients.agents.deepdive_robust.agent import DeepDiveAgentRobust
        return DeepDiveAgentRobust(api_config_path=api_config_path)
    else:  # original
        from clients.openrca_rca.agent import OpenRCARCAAgent
        return OpenRCARCAAgent(api_config_path=api_config_path)


def get_agent_name(agent_type: str) -> str:
    if agent_type == "critic":
        return "react-rca-critic"
    elif agent_type == "react":
        return "react-rca"
    elif agent_type == "deepdive1":
        return "deepdive-agent-1"
    elif agent_type == "deepdive-hypothesis":
        return "deepdive-agent-hypothesis"
    elif agent_type == "deepdive-baseline":
        return "deepdive-agent-baseline"
    elif agent_type == "deepdive-evidence":
        return "deepdive-agent-evidence"
    elif agent_type == "deepdive-multisignal":
        return "deepdive-agent-multisignal"
    elif agent_type == "deepdive-robust":
        return "deepdive-agent-robust"
    else:  # original
        return "openrca-rca"


def load_candidates_for_task(dataset_key: str, row_index: str) -> list[dict] | None:
    """Load pre-analyzed top-3 candidates from experiments/ or experiments/candidates/."""
    experiments_dir = Path(__file__).parent.parent / "experiments"
    # Map dataset_key to file
    file_map = {
        "openrca_bank": "bank_top3_candidates.json",
        "openrca_telecom": "telecom_top3_candidates.json",
        "openrca_market_cb1": "market_top3_candidates.json",
        "openrca_market_cb2": "market_top3_candidates.json",
    }
    filename = file_map.get(dataset_key)
    if not filename:
        return None
    # Check candidates/ subdirectory first, then experiments/ root
    filepath = experiments_dir / "candidates" / filename
    if not filepath.exists():
        filepath = experiments_dir / filename
    if not filepath.exists():
        logger.warning(f"Candidates file not found: {filepath}")
        return None
    with open(filepath) as f:
        data = json.load(f)
    tasks = data.get("tasks", {})
    if row_index in tasks:
        candidates = tasks[row_index].get("candidates", [])
        return _normalize_candidate_times_to_utc(dataset_key, candidates)
    return None


def _normalize_candidate_times_to_utc(dataset_key: str, candidates: list[dict]) -> list[dict]:
    """Normalize OpenRCA candidate times from UTC+8 text to UTC text.

    OpenRCA source query/record times are UTC+8. DeepDive candidates are generated
    from those times, so we normalize full datetime strings before passing them to
    the agent that issues UTC-based telemetry queries.
    """
    if not dataset_key.startswith("openrca_"):
        return candidates

    utc8 = timezone(timedelta(hours=8))
    out: list[dict] = []
    for cand in candidates:
        row = dict(cand)
        for key in ("peak_time", "time"):
            raw = row.get(key)
            if not isinstance(raw, str):
                continue
            try:
                dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=utc8)
                row[key] = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                # Keep non-datetime formats (e.g., "14:30") as-is.
                pass
        out.append(row)
    return out


def append_score(scores_path: Path, eval_id: str, pid: str, results: dict):
    dataset = extract_dataset_key(pid)
    is_new = not scores_path.exists()
    with open(scores_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_id": eval_id,
            "problem_id": pid,
            "dataset": dataset,
            "task_type": results.get("task_type", ""),
            "difficulty": results.get("difficulty", ""),
            "score": results.get("score", ""),
            "success": results.get("success", ""),
            "steps": results.get("steps", ""),
            "TTA": round(results.get("TTA", 0), 2),
            "in_tokens": results.get("in_tokens", ""),
            "out_tokens": results.get("out_tokens", ""),
            "passing_criteria": json.dumps(results.get("passing_criteria", [])),
            "failing_criteria": json.dumps(results.get("failing_criteria", [])),
            "ground_truth": results.get("ground_truth", ""),
        })


def classify_criteria(criteria_list: list[str]) -> dict:
    """Classify passing/failing criteria into t(ime), c(omponent), r(eason)."""
    counts = {"t": 0, "c": 0, "r": 0}
    for item in criteria_list:
        if re.search(r"\d{4}-\d{2}-\d{2}", item):
            counts["t"] += 1
        elif item in ("container read I/O load", "container write I/O load",
                       "container network receive load", "container network transmit load",
                       "container cpu load", "container memory load"):
            counts["r"] += 1
        else:
            # If it looks like a reason (longer text with spaces), classify as reason
            # Otherwise classify as component
            if any(kw in item.lower() for kw in ("load", "cpu", "memory", "disk",
                                                   "network", "connection", "timeout",
                                                   "error", "failure", "lock",
                                                   "io", "i/o", "read", "write")):
                counts["r"] += 1
            else:
                counts["c"] += 1
    return counts


def write_summary(summary_path: Path, all_results: list[dict], eval_id: str,
                  agent_type: str, model_name: str):
    """Write experiment summary with dataset, t/c/r, and overall statistics."""
    # Organize by dataset
    by_dataset = defaultdict(list)
    for r in all_results:
        by_dataset[r["dataset"]].append(r)

    # Count t/c/r from ground_truth and passing_criteria
    total_t, total_c, total_r = 0, 0, 0
    pass_t, pass_c, pass_r = 0, 0, 0

    lines = []
    lines.append(f"Experiment Summary: {eval_id}")
    lines.append(f"Agent: {agent_type} | Model: {model_name}")
    lines.append(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Total problems: {len(all_results)}")
    lines.append("=" * 60)

    for ds in sorted(by_dataset.keys()):
        results = by_dataset[ds]
        scores = [r["score"] for r in results]
        successes = sum(1 for r in results if r["success"])
        avg_score = sum(scores) / len(scores) if scores else 0
        avg_tta = sum(r["TTA"] for r in results) / len(results) if results else 0

        lines.append(f"\n[{ds}] ({len(results)} problems)")
        lines.append(f"  Avg score: {avg_score:.3f}")
        lines.append(f"  Success rate: {successes}/{len(results)} ({successes/len(results)*100:.1f}%)")
        lines.append(f"  Avg TTA: {avg_tta:.1f}s")

        # Per-problem details
        for r in results:
            status = "PASS" if r["success"] else "FAIL"
            lines.append(f"    {r['pid']:45s} score={r['score']:.2f} [{status}]")

    # t/c/r accuracy
    lines.append("\n" + "=" * 60)
    lines.append("Per-dimension accuracy (t=time, c=component, r=reason)")
    lines.append("-" * 60)

    for r in all_results:
        gt = r.get("ground_truth", "")
        passing = r.get("passing_criteria", [])
        failing = r.get("failing_criteria", [])

        # Count expected dimensions from ground_truth
        gt_t = len(re.findall(r"root cause occurrence time", gt))
        gt_c = len(re.findall(r"root cause component", gt))
        gt_r = len(re.findall(r"root cause reason", gt))

        total_t += gt_t
        total_c += gt_c
        total_r += gt_r

        # Count passing dimensions
        p_counts = classify_criteria(passing)
        pass_t += p_counts["t"]
        pass_c += p_counts["c"]
        pass_r += p_counts["r"]

    def pct(a, b):
        return f"{a}/{b} ({a/b*100:.1f}%)" if b > 0 else "N/A"

    lines.append(f"  Time (t):      {pct(pass_t, total_t)}")
    lines.append(f"  Component (c): {pct(pass_c, total_c)}")
    lines.append(f"  Reason (r):    {pct(pass_r, total_r)}")
    total_dims = total_t + total_c + total_r
    pass_dims = pass_t + pass_c + pass_r
    lines.append(f"  Overall:       {pct(pass_dims, total_dims)}")

    # Overall accuracy
    lines.append("\n" + "=" * 60)
    all_scores = [r["score"] for r in all_results]
    all_successes = sum(1 for r in all_results if r["success"])
    lines.append(f"Overall avg score: {sum(all_scores)/len(all_scores):.3f}")
    lines.append(f"Overall success rate: {pct(all_successes, len(all_results))}")
    lines.append(f"Overall avg TTA: {sum(r['TTA'] for r in all_results)/len(all_results):.1f}s")
    lines.append("=" * 60)

    summary_text = "\n".join(lines)
    summary_path.write_text(summary_text)
    print(f"\n{summary_text}")
    return summary_text


def parse_args():
    parser = argparse.ArgumentParser(description="RCA Experiment Runner")
    parser.add_argument("--problems-file", type=str, required=True,
                        help="Path to a text file listing problem IDs")
    parser.add_argument("--agent", type=str, default="critic",
                        choices=[
                            "critic",
                            "react",
                            "original",
                            "deepdive1",
                            "deepdive-hypothesis",
                            "deepdive-baseline",
                            "deepdive-evidence",
                            "deepdive-multisignal",
                            "deepdive-robust",
                        ],
                        help=(
                            "Agent type: critic/react/original/deepdive1/"
                            "deepdive-hypothesis/deepdive-baseline/"
                            "deepdive-evidence/deepdive-multisignal/deepdive-robust "
                            "(default: critic)"
                        ))
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS,
                        help=f"Max orchestrator steps (default: {MAX_STEPS})")
    parser.add_argument("--results-dir", type=str, default="results/experiments",
                        help="Base results directory (default: results/experiments)")
    parser.add_argument("--api-config", type=str, default=None,
                        help="Path to api_config.yaml")
    parser.add_argument("--eval-id", type=str, required=True,
                        help="Experiment identifier (used as results subdirectory)")
    parser.add_argument("--start-index", type=int, default=0,
                        help="Skip the first N problems (default: 0)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Results directory: results/experiments/{eval_id}/
    experiment_dir = Path(args.results_dir) / args.eval_id
    experiment_dir.mkdir(parents=True, exist_ok=True)
    scores_path = experiment_dir / "scores.csv"
    summary_path = experiment_dir / "summary.txt"

    api_config_path = args.api_config or str(
        Path(__file__).parent / "openrca_rca" / "api_config.yaml"
    )

    # Load problem IDs
    problem_ids = load_problems_file(args.problems_file)
    if args.start_index > 0:
        logger.info(f"Skipping first {args.start_index} problems")
        problem_ids = problem_ids[args.start_index:]

    # Orchestrator uses experiment_dir as base; session.get_save_dir() adds dataset/agent/model/eval_id
    orchestrator = StaticOrchestrator(results_dir=str(experiment_dir), eval_id=args.eval_id)

    # Get model name for W&B
    _tmp_agent = create_agent(args.agent, api_config_path)
    model_name = _tmp_agent.get_model_name()
    agent_name = get_agent_name(args.agent)

    orchestrator.init_wandb(
        run_name=f"{agent_name}/{model_name}/{args.eval_id}",
        config={
            "agent": agent_name,
            "model": model_name,
            "eval_id": args.eval_id,
            "total_problems": len(problem_ids),
            "problems_file": args.problems_file,
        },
    )

    logger.info(f"Experiment: {args.eval_id}")
    logger.info(f"Agent: {args.agent} | Model: {model_name}")
    logger.info(f"Running {len(problem_ids)} problems")
    logger.info(f"Results: {experiment_dir}\n")

    all_results = []
    total_score = 0
    completed = 0

    for i, pid in enumerate(problem_ids):
        print(f"\n{'=' * 60}")
        print(f"[{i+1}/{len(problem_ids)}] Problem: {pid}")
        print(f"{'=' * 60}\n")

        dataset_key = extract_dataset_key(pid)
        agent = create_agent(args.agent, api_config_path)
        orchestrator.register_agent(agent, name=agent_name)

        actions = None

        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            problem = orchestrator.session.problem

            save_dir = orchestrator.session.get_save_dir()

            dataset_config = problem.app.dataset_config
            basic_prompt = get_basic_prompt(dataset_key)

            if args.agent == "original":
                # OpenRCA original agent: internal executor via set_actions()
                problem.telemetry_guide = build_telemetry_guide("all", dataset_key=dataset_key)
                problem_desc = problem.get_task_description()

                apis = {k: v for k, v in apis.items() if k in ("execute", "submit")}
                orchestrator._problem_init_info = (problem_desc, instructs, apis)

                agent.init_context(problem_desc, instructs, apis)
                agent.set_actions(problem._actions, problem.namespace, dataset_key,
                                  max_steps=args.max_steps)

                orchestrator._system_message = agent.controller_prompt[0]["content"]

                # Link trajectories
                orchestrator.session.extra["executor_trajectory"] = agent.executor_trajectory

            else:
                # React / Critic agent: external executor via StaticRCAActionsWithExecutor
                use_executor = dataset_config.get("executor", {}).get("enable", True)
                use_hypothesis = dataset_config.get("hypothesis", {}).get("enable", False)

                actions = StaticRCAActionsWithExecutor(
                    container_name=problem.app.get_container_name(),
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

                problem._actions = actions
                problem.actions = actions

                all_apis = problem.get_available_actions()
                apis = {k: v for k, v in all_apis.items() if k in ("execute", "submit")}

                enabled_types = getattr(actions, "enabled_telemetry_types", None)
                problem.telemetry_guide = build_executor_telemetry_guide(enabled_types)
                problem_desc = problem.get_task_description()

                possible_rca = dataset_config.get("possible_root_causes")

                # Load pre-analyzed candidates for staged agent
                candidates = None
                if args.agent in {
                    "deepdive1",
                    "deepdive-hypothesis",
                    "deepdive-baseline",
                    "deepdive-evidence",
                    "deepdive-multisignal",
                    "deepdive-robust",
                }:
                    row_index = pid.rsplit("-", 1)[-1]  # e.g. "openrca_bank-task_7-3" → "3"
                    candidates = load_candidates_for_task(dataset_key, row_index)

                agent.init_context(
                    problem_desc, instructs, apis,
                    possible_rca=possible_rca,
                    **({"candidates": candidates} if candidates else {}),
                )

                if hasattr(agent, "get_system_prompt"):
                    orchestrator._system_message = agent.get_system_prompt()
                elif hasattr(agent, "history"):
                    orchestrator._system_message = agent.history[0]["content"]
                else:
                    orchestrator._system_message = ""

                # Link trajectories
                orchestrator.session.extra["executor_trajectory"] = actions._executor_trajectory
                if hasattr(agent, "_critic_trajectory"):
                    orchestrator.session.extra["critic_trajectory"] = agent._critic_trajectory
                if hasattr(agent, "_agent_trajectory"):
                    orchestrator.session.extra["agent_trajectory"] = agent._agent_trajectory

            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            full_output = asyncio.run(
                orchestrator.start_problem(max_steps=args.max_steps)
            )
            results = full_output.get("results", {})

            score = results.get("score", 0)
            total_score += score
            completed += 1

            # Save to unified scores.csv
            append_score(scores_path, args.eval_id, pid, results)

            # Collect for summary
            all_results.append({
                "pid": pid,
                "dataset": dataset_key,
                "score": score,
                "success": results.get("success", False),
                "TTA": results.get("TTA", 0),
                "task_type": results.get("task_type", ""),
                "passing_criteria": results.get("passing_criteria", []),
                "failing_criteria": results.get("failing_criteria", []),
                "ground_truth": results.get("ground_truth", ""),
            })

            # W&B: log real-time progress and t/c/r accuracy
            successes = sum(1 for r in all_results if r["success"])
            avg_score = total_score / completed if completed else 0

            # Compute running t/c/r accuracy
            cum_t_total = cum_c_total = cum_r_total = 0
            cum_t_pass = cum_c_pass = cum_r_pass = 0
            for r in all_results:
                gt = r.get("ground_truth", "")
                cum_t_total += len(re.findall(r"root cause occurrence time", gt))
                cum_c_total += len(re.findall(r"root cause component", gt))
                cum_r_total += len(re.findall(r"root cause reason", gt))
                p = classify_criteria(r.get("passing_criteria", []))
                cum_t_pass += p["t"]
                cum_c_pass += p["c"]
                cum_r_pass += p["r"]

            t_rate = cum_t_pass / cum_t_total * 100 if cum_t_total else 0
            c_rate = cum_c_pass / cum_c_total * 100 if cum_c_total else 0
            r_rate = cum_r_pass / cum_r_total * 100 if cum_r_total else 0

            if orchestrator.use_wandb:
                wandb.log({
                    "T": t_rate,
                    "C": c_rate,
                    "R": r_rate,
                })

                # Overview summary (shown in Runs table)
                wandb.summary["progress"] = f"{completed}/{len(problem_ids)}"
                wandb.summary["T"] = round(t_rate, 1)
                wandb.summary["C"] = round(c_rate, 1)
                wandb.summary["R"] = round(r_rate, 1)

            t_str = f"{cum_t_pass}/{cum_t_total}" if cum_t_total else "N/A"
            c_str = f"{cum_c_pass}/{cum_c_total}" if cum_c_total else "N/A"
            r_str = f"{cum_r_pass}/{cum_r_total}" if cum_r_total else "N/A"

            print(f"\nScore: {score}")
            print(f"Passing: {results.get('passing_criteria', 'N/A')}")
            print(f"Failing: {results.get('failing_criteria', 'N/A')}")
            print(f"Running: avg={avg_score:.3f}, success={successes}/{completed}, t={t_str}, c={c_str}, r={r_str}")

        except Exception as e:
            logger.error(f"Error running {pid}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if args.agent == "original":
                agent.cleanup()
            elif actions is not None:
                actions.cleanup()

    # W&B: set final summary values (shown as columns in Runs Table)
    if all_results:
        final_successes = sum(1 for r in all_results if r["success"])
        final_avg = total_score / completed if completed else 0
        # Recompute final t/c/r
        ft_total = fc_total = fr_total = 0
        ft_pass = fc_pass = fr_pass = 0
        for r in all_results:
            gt = r.get("ground_truth", "")
            ft_total += len(re.findall(r"root cause occurrence time", gt))
            fc_total += len(re.findall(r"root cause component", gt))
            fr_total += len(re.findall(r"root cause reason", gt))
            p = classify_criteria(r.get("passing_criteria", []))
            ft_pass += p["t"]
            fc_pass += p["c"]
            fr_pass += p["r"]

        if orchestrator.use_wandb:
            wandb.summary["progress"] = f"{completed}/{completed}"
            wandb.summary["T"] = round(ft_pass / ft_total * 100, 1) if ft_total else 0
            wandb.summary["C"] = round(fc_pass / fc_total * 100, 1) if fc_total else 0
            wandb.summary["R"] = round(fr_pass / fr_total * 100, 1) if fr_total else 0

    orchestrator.finish_wandb()

    # Write summary with statistics
    if all_results:
        write_summary(summary_path, all_results, args.eval_id, args.agent, model_name)
    else:
        print("\nNo results to summarize.")

    print(f"\nScores: {scores_path}")
    print(f"Summary: {summary_path}")
