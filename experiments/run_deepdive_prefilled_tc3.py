"""Run Deep Dive-focused experiment with prefilled exploration snapshots.

This script:
1) Treats EXPLORATION as pre-completed using fixed graph snapshots.
2) Selects the first 3 telecom tasks from a 30-task list.
3) Runs Deep Dive agent and saves:
   - result_01_*.json
   - result_02_*.json
   - result_03_*.json

Top-level outputs are saved under experiments/exp_{k:03d}/.
Full orchestrator artifacts are saved under experiments/exp_{k:03d}/orchestrator_results/.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import importlib.util
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.orchestrator.static_actions.rca_executor import StaticRCAActionsWithExecutor
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide
from clients.run_experiment import (
    create_agent,
    extract_dataset_key,
    get_agent_name,
    load_candidates_for_task,
    classify_criteria,
    load_problems_file,
)
from clients.openrca_rca.action_profiles import (
    list_known_profiles,
    resolve_profile_name,
    select_agent_actions,
)

logger = logging.getLogger("deepdive_prefilled_tc3")


GRAPH_DEFINITIONS = {
    "CALL_GRAPH": (
        "trace_span.csv from parent span cmdb_id -> child span cmdb_id (RPC) "
        "or cmdb_id -> dsName (JDBC). Used for failure propagation reasoning."
    ),
    "DEPLOYMENT_GRAPH": (
        "Pod-to-node placement graph from metric_container.csv cmdb_id format. "
        "Used to narrow simultaneous pod failures to a shared node."
    ),
    "SHARED_RESOURCE_GRAPH": (
        "Caller groups sharing the same backend resource, derived from CALL_GRAPH "
        "by callee perspective. Used for shared dependency root-cause reasoning."
    ),
}


EXPLORATION_SNAPSHOTS = {
    "openrca_telecom": {
        "graph_definitions": GRAPH_DEFINITIONS,
        "CALL_GRAPH": {
            "os_021": ["docker_003", "docker_004"],
            "os_022": ["docker_001", "docker_002"],
            "docker_001": ["docker_007", "docker_008", "db_007", "db_009"],
            "docker_002": ["docker_007", "docker_008", "db_007", "db_009"],
            "docker_003": ["docker_005", "docker_006", "db_007", "db_009"],
            "docker_004": ["docker_005", "docker_006", "db_007", "db_009"],
            "docker_005": ["db_003"],
            "docker_006": ["db_003"],
            "docker_007": ["db_003"],
            "docker_008": ["db_003"],
        },
        "DEPLOYMENT_GRAPH": {},
        "SHARED_RESOURCE_GRAPH": {
            "db_003": ["docker_005", "docker_006", "docker_007", "docker_008"],
            "db_007": ["docker_001", "docker_002", "docker_003", "docker_004"],
            "db_009": ["docker_001", "docker_002", "docker_003", "docker_004"],
        },
    },
    "openrca_bank": {
        "graph_definitions": GRAPH_DEFINITIONS,
        "CALL_GRAPH": {
            "IG01": ["Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04"],
            "IG02": ["Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04"],
            "Tomcat01": ["MG01", "MG02"],
            "Tomcat02": ["MG01", "MG02"],
            "Tomcat03": ["MG01", "MG02"],
            "Tomcat04": ["MG01", "MG02"],
            "MG01": ["dockerA1", "dockerA2", "dockerB1", "dockerB2"],
            "MG02": ["dockerA1", "dockerA2", "dockerB1", "dockerB2"],
            "dockerA1": ["MG01", "MG02"],
            "dockerA2": ["MG01", "MG02"],
            "dockerB1": ["MG01", "MG02"],
            "dockerB2": ["MG01", "MG02"],
        },
        "DEPLOYMENT_GRAPH": {},
        "SHARED_RESOURCE_GRAPH": {
            "MG01": [
                "Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04",
                "dockerA1", "dockerA2", "dockerB1", "dockerB2",
            ],
            "MG02": [
                "Tomcat01", "Tomcat02", "Tomcat03", "Tomcat04",
                "dockerA1", "dockerA2", "dockerB1", "dockerB2",
            ],
        },
    },
    "openrca_market_cb1": {
        "graph_definitions": GRAPH_DEFINITIONS,
        "CALL_GRAPH": {
            "frontend": [
                "adservice", "cartservice", "checkoutservice", "currencyservice",
                "productcatalogservice", "recommendationservice", "shippingservice",
            ],
            "frontend2": [
                "adservice2", "cartservice2", "checkoutservice2", "currencyservice2",
                "productcatalogservice2", "recommendationservice2", "shippingservice2",
            ],
            "checkoutservice": [
                "cartservice", "currencyservice", "emailservice", "paymentservice",
                "productcatalogservice", "shippingservice",
            ],
            "checkoutservice2": [
                "cartservice2", "currencyservice2", "emailservice2", "paymentservice2",
                "productcatalogservice2", "shippingservice2",
            ],
            "recommendationservice": ["productcatalogservice"],
            "recommendationservice2": ["productcatalogservice"],
        },
        "DEPLOYMENT_GRAPH": {
            "node-5": [
                "adservice-2", "cartservice2-0", "checkoutservice-2",
                "frontend-1", "frontend-2", "shippingservice-2",
            ],
            "node-6": [
                "adservice-0", "adservice-1", "adservice2-0",
                "cartservice-0", "cartservice-1", "cartservice-2",
                "checkoutservice-0", "checkoutservice-1", "checkoutservice2-0",
                "currencyservice-0", "currencyservice-1", "currencyservice-2", "currencyservice2-0",
                "emailservice-0", "emailservice-1", "emailservice-2", "emailservice2-0",
                "frontend-0", "frontend2-0",
                "paymentservice-0", "paymentservice-1", "paymentservice-2", "paymentservice2-0",
                "productcatalogservice-0", "productcatalogservice-1", "productcatalogservice-2", "productcatalogservice2-0",
                "recommendationservice-0", "recommendationservice-1", "recommendationservice-2", "recommendationservice2-0",
                "redis-cart-0", "redis-cart2-0",
                "shippingservice-0", "shippingservice-1", "shippingservice2-0",
            ],
        },
        "SHARED_RESOURCE_GRAPH": {
            "productcatalogservice": ["checkoutservice", "frontend", "recommendationservice", "recommendationservice2"],
            "cartservice": ["checkoutservice", "frontend"],
            "currencyservice": ["checkoutservice", "frontend"],
            "shippingservice": ["checkoutservice", "frontend"],
            "productcatalogservice2": ["checkoutservice2", "frontend2"],
            "cartservice2": ["checkoutservice2", "frontend2"],
            "currencyservice2": ["checkoutservice2", "frontend2"],
            "shippingservice2": ["checkoutservice2", "frontend2"],
        },
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deep Dive-only telecom TC3 runner")
    parser.add_argument("--problems-file", type=str, default="experiments/problems_30.txt")
    parser.add_argument(
        "--agent",
        type=str,
        default="deepdive1",
        choices=[
            "deepdive1",
            "deepdive-hypothesis",
            "deepdive-baseline",
            "deepdive-evidence",
            "deepdive-multisignal",
            "deepdive-robust",
        ],
    )
    parser.add_argument("--api-config", type=str, default="clients/openrca_rca/api_config_low.yaml")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--k", type=int, default=None, help="Experiment index. Auto-increment if omitted.")
    parser.add_argument("--base-dir", type=str, default="experiments", help="Base directory for exp_k.")
    parser.add_argument(
        "--prompt-file",
        type=str,
        default="experiments/prompt_presets/deepdive_tc3_prompt_exp01.py",
        help="Python prompt file loaded from experiments directory.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Create exp directory/metadata only.")
    parser.add_argument("--no-wandb", action="store_true", help="Disable W&B logging (default is enabled).")
    parser.add_argument(
        "--action-profile",
        type=str,
        default=None,
        help="Agent action profile name (built-in or from --action-profile-file). Default: legacy_execute_only.",
    )
    parser.add_argument(
        "--action-profile-file",
        type=str,
        default=None,
        help="Optional JSON file defining/overriding action profiles.",
    )
    # Backward-compatible alias for older scripts.
    parser.add_argument(
        "--executor-api",
        type=str,
        choices=["legacy", "anomaly_report"],
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--list-action-profiles",
        action="store_true",
        help="List available action profiles and exit.",
    )
    args = parser.parse_args()
    if args.list_action_profiles:
        for name in list_known_profiles(profile_file=args.action_profile_file):
            print(name)
        raise SystemExit(0)
    args.action_profile = resolve_profile_name(
        action_profile=args.action_profile,
        executor_api_legacy=args.executor_api,
    )
    known = set(list_known_profiles(profile_file=args.action_profile_file))
    if args.action_profile not in known:
        parser.error(
            f"Unknown action profile '{args.action_profile}'. "
            f"Known profiles: {', '.join(sorted(known))}"
        )
    return args


def make_experiment_dir(base_dir: Path, k: int | None) -> tuple[int, Path]:
    base_dir.mkdir(parents=True, exist_ok=True)
    if k is None:
        existing = []
        for d in base_dir.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r"exp_(\d{3})$", d.name)
            if m:
                existing.append(int(m.group(1)))
        k = (max(existing) + 1) if existing else 1
    exp_dir = base_dir / f"exp_{k:03d}"
    exp_dir.mkdir(parents=True, exist_ok=True)
    return k, exp_dir


def select_telecom_first3_tc(problem_ids: list[str]) -> list[str]:
    telecom_tc = []
    for pid in problem_ids:
        if not pid.startswith("openrca_telecom-"):
            continue
        task_match = re.search(r"-task_(\d+)-", pid)
        if not task_match:
            continue
        task_num = int(task_match.group(1))
        if task_num in (4, 7):  # both include T + C in ground-truth dimensions
            telecom_tc.append(pid)
    selected = telecom_tc[:3]
    if len(selected) < 3:
        raise ValueError("Need at least 3 telecom tasks with T+C ground-truth (task_4 or task_7).")
    return selected


def tc_breakdown(results: dict) -> dict:
    gt = results.get("ground_truth", "")
    passing = results.get("passing_criteria", [])
    counts = classify_criteria(passing)
    total_t = len(re.findall(r"root cause occurrence time", gt))
    total_c = len(re.findall(r"root cause component", gt))
    return {
        "T": {"pass": counts["t"], "total": total_t},
        "C": {"pass": counts["c"], "total": total_c},
    }


def sanitize_problem_description(text: str) -> str:
    """Remove misleading static-dataset boilerplate from problem description."""
    drop_prefixes = (
        "Service Details:",
        "Service Name:",
        "Namespace:",
        "Description:",
        "Supported Operations:",
    )
    kept: list[str] = []
    blank_run = 0
    for line in text.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(p) for p in drop_prefixes):
            continue
        if not stripped:
            blank_run += 1
            if blank_run <= 1:
                kept.append("")
            continue
        blank_run = 0
        kept.append(line.rstrip())
    return "\n".join(kept).strip() + "\n"


def _parse_scoring_points_obj(scoring_points_raw):
    if isinstance(scoring_points_raw, dict):
        return scoring_points_raw
    if not isinstance(scoring_points_raw, str):
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            obj = parser(scoring_points_raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def extract_gt_single_candidate(problem) -> dict:
    """Extract one GT candidate (component/time) from problem scoring points."""
    raw = getattr(problem, "scoring_points", None)
    obj = _parse_scoring_points_obj(raw)

    comp = ""
    when = ""
    if isinstance(obj, dict):
        first_item = None
        if "1" in obj and isinstance(obj["1"], dict):
            first_item = obj["1"]
        else:
            for v in obj.values():
                if isinstance(v, dict):
                    first_item = v
                    break
        if isinstance(first_item, dict):
            comp = (
                first_item.get("root cause component")
                or first_item.get("component")
                or ""
            )
            when = (
                first_item.get("root cause occurrence datetime")
                or first_item.get("root cause occurrence time")
                or first_item.get("time")
                or ""
            )

    raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if not comp:
        patterns = [
            r"root cause component[\"']?\s*[:=]\s*[\"']([^\"'\n]+)",
            r"The only predicted root cause component is ([^\n]+)",
            r"The \d+-th predicted root cause component is ([^\n]+)",
        ]
        for pat in patterns:
            m_comp = re.search(pat, raw_text, re.IGNORECASE)
            if m_comp:
                comp = m_comp.group(1).strip()
                break
    if not when:
        m_time = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", raw_text)
        if m_time:
            when = m_time.group(1).strip()

    # Fallback to query_info faults if scoring_points parsing is incomplete.
    if not comp or not when:
        qi = getattr(getattr(problem, "app", None), "query_info", None)
        faults = getattr(qi, "faults", None) if qi else None
        if isinstance(faults, list) and faults:
            f0 = faults[0]
            if not comp:
                comp = str(f0.get("component", "")).strip()
            if not when:
                when = str(f0.get("datetime", "")).strip()

    if not comp or not when:
        raise RuntimeError(
            f"Failed to extract GT single candidate from scoring_points: component='{comp}', time='{when}'"
        )

    return {"component": comp, "peak_time": when}


def extract_gt_single_candidate_from_candidates(dataset_key: str, problem_id: str) -> dict | None:
    """Prefer GT candidate from experiments/candidates/*_top3_candidates.json."""
    row_index = problem_id.rsplit("-", 1)[-1]
    cands = load_candidates_for_task(dataset_key, row_index)
    if not cands:
        return None

    gt = next((c for c in cands if c.get("is_gt") is True), None)
    if gt is None:
        gt = next((c for c in cands if int(c.get("rank", 9999)) == 1), None)
    if gt is None and cands:
        gt = cands[0]
    if not gt:
        return None

    component = str(gt.get("component", "")).strip()
    peak_time = str(gt.get("peak_time", gt.get("time", ""))).strip()
    if not component or not peak_time:
        return None
    return {"component": component, "peak_time": peak_time}


def load_prompt_module_from_file(prompt_file: Path):
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
    spec = importlib.util.spec_from_file_location("deepdive_exp_prompt", str(prompt_file))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load prompt spec from: {prompt_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_prompt_overrides(agent, prompt_module) -> None:
    """Override stage prompts from an experiments-side Python file."""
    if not hasattr(agent, "configure_prompts"):
        raise TypeError("Selected agent does not support runtime prompt overrides.")

    agent.configure_prompts(
        exploration_prompt=getattr(prompt_module, "EXPLORATION_PROMPT", None),
        deepdive_prompt_template=getattr(prompt_module, "DEEPDIVE_PROMPT_TEMPLATE", None),
        expand_prompt_template=getattr(prompt_module, "EXPAND_PROMPT_TEMPLATE", None),
        system_template=getattr(prompt_module, "SYSTEM_TEMPLATE", None),
    )


def prepare_problem(
    problem_id: str,
    agent_type: str,
    api_config: str,
    orchestrator: StaticOrchestrator,
    prompt_module,
    action_profile: str = "legacy_execute_only",
    action_profile_file: str | None = None,
):
    dataset_key = extract_dataset_key(problem_id)
    if dataset_key not in EXPLORATION_SNAPSHOTS:
        raise ValueError(f"No exploration snapshot configured for dataset: {dataset_key}")

    agent = create_agent(agent_type, api_config)
    apply_prompt_overrides(agent, prompt_module)
    agent_name = get_agent_name(agent_type)
    orchestrator.register_agent(agent, name=agent_name)

    problem_desc, instructs, _ = orchestrator.init_problem(problem_id)
    problem = orchestrator.session.problem
    save_dir = orchestrator.session.get_save_dir()

    dataset_config = problem.app.dataset_config
    basic_prompt = get_basic_prompt(dataset_key)

    actions = StaticRCAActionsWithExecutor(
        base_path=str(problem.app.get_host_telemetry_path()),
        possible_root_causes=dataset_config.get("possible_root_causes"),
        telemetry_flags=dataset_config.get("telemetry"),
        use_executor=dataset_config.get("executor", {}).get("enable", True),
        use_hypothesis=dataset_config.get("hypothesis", {}).get("enable", False),
        work_dir=str(save_dir),
    )

    if dataset_config.get("executor", {}).get("enable", True):
        task_part = problem_id.split("-", maxsplit=1)[1] if "-" in problem_id else problem_id
        notebook_path = save_dir / f"{task_part}_executor.ipynb"
        query_time_range = problem.app.query_info.time_range if problem.app.query_info else None
        actions.setup_executor(
            background=basic_prompt.schema,
            api_config_path=api_config,
            namespace=problem.namespace,
            logger=logger,
            notebook_save_path=str(notebook_path),
            query_time_range=query_time_range,
        )

    problem._actions = actions
    problem.actions = actions

    all_apis = problem.get_available_actions()
    apis, _profile_cfg = select_agent_actions(
        all_apis=all_apis,
        action_profile=action_profile,
        profile_file=action_profile_file,
    )

    enabled_types = getattr(actions, "enabled_telemetry_types", None)
    executor_actions = [k for k in apis.keys() if k.startswith("execute")]
    problem.telemetry_guide = build_executor_telemetry_guide(
        enabled_types,
        executor_actions=executor_actions,
    )
    problem_desc = sanitize_problem_description(problem.get_task_description())

    gt_candidate = extract_gt_single_candidate_from_candidates(dataset_key, problem_id)
    if gt_candidate is None:
        gt_candidate = extract_gt_single_candidate(problem)
    candidates = [gt_candidate]

    snapshot = EXPLORATION_SNAPSHOTS[dataset_key]
    agent.init_context(
        problem_desc,
        instructs,
        apis,
        possible_rca=dataset_config.get("possible_root_causes"),
        dataset_notes="",
        candidates=candidates,
        prefilled_exploration=snapshot,
        dataset_key=dataset_key,
    )

    orchestrator._system_message = agent.get_system_prompt()
    orchestrator._problem_init_info = (problem_desc, instructs, apis)
    orchestrator.session.extra["executor_trajectory"] = actions._executor_trajectory
    if hasattr(agent, "_agent_trajectory"):
        orchestrator.session.extra["agent_trajectory"] = agent._agent_trajectory

    orchestrator.sprint.problem_init(problem_desc, instructs, apis)
    return agent, actions, apis


def main() -> None:
    args = parse_args()
    use_wandb = not args.no_wandb
    # Make behavior deterministic regardless of parent shell env.
    # Without explicit init, USE_WANDB=true causes wandb.save() preinit errors.
    os.environ["USE_WANDB"] = "true" if use_wandb else "false"

    problem_ids = load_problems_file(args.problems_file)
    selected = select_telecom_first3_tc(problem_ids)

    k, exp_dir = make_experiment_dir(Path(args.base_dir), args.k)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    eval_id = f"deepdive_tc3_exp_{k:03d}_{run_stamp}"
    orchestrator_root = exp_dir / "orchestrator_results"
    orchestrator_root.mkdir(parents=True, exist_ok=True)
    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_absolute():
        prompt_file = (Path.cwd() / prompt_file).resolve()
    prompt_module = load_prompt_module_from_file(prompt_file)

    meta = {
        "experiment_index": k,
        "created_at": run_stamp,
        "agent": args.agent,
        "api_config": args.api_config,
        "max_steps": args.max_steps,
        "problems_file": args.problems_file,
        "selected_problems": selected,
        "notes": "Exploration is prefilled from fixed graph snapshots; Deep Dive starts immediately.",
        "prompt_file": str(prompt_file),
        "use_wandb": use_wandb,
        "action_profile": args.action_profile,
        "action_profile_file": args.action_profile_file,
        "available_apis": [],
    }
    (exp_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (exp_dir / "exploration_snapshots.json").write_text(
        json.dumps(EXPLORATION_SNAPSHOTS, indent=2),
        encoding="utf-8",
    )
    if args.dry_run:
        print(f"[DRY-RUN] Experiment directory: {exp_dir}")
        print(f"[DRY-RUN] Selected problems: {selected}")
        print(f"[DRY-RUN] Prompt file: {prompt_file}")
        print(f"[DRY-RUN] Action profile: {args.action_profile}")
        return

    for idx, pid in enumerate(selected, start=1):
        orchestrator = StaticOrchestrator(results_dir=str(orchestrator_root), eval_id=eval_id)
        agent = None
        actions = None
        try:
            if use_wandb:
                orchestrator.init_wandb(
                    run_name=f"{args.agent}/{eval_id}/{idx:02d}-{pid}",
                    config={
                        "agent": args.agent,
                        "eval_id": eval_id,
                        "problem_id": pid,
                        "experiment_dir": str(exp_dir),
                    },
                )

            agent, actions, apis = prepare_problem(
                pid,
                args.agent,
                args.api_config,
                orchestrator,
                prompt_module=prompt_module,
                action_profile=args.action_profile,
                action_profile_file=args.action_profile_file,
            )
            if not meta.get("available_apis"):
                meta["available_apis"] = list(apis.keys())
                (exp_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

            output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = output.get("results", {})
            tc = tc_breakdown(results)

            payload = {
                "problem_id": pid,
                "score": results.get("score"),
                "success": results.get("success"),
                "steps": results.get("steps"),
                "T": tc["T"],
                "C": tc["C"],
                "passing_criteria": results.get("passing_criteria", []),
                "failing_criteria": results.get("failing_criteria", []),
                "ground_truth": results.get("ground_truth", ""),
                "session_json": str(orchestrator.session.get_filepath(file_type="json")),
                "session_log": str(orchestrator.session.get_filepath(file_type="log")),
            }
            result_path = exp_dir / f"result_{idx:02d}_{pid}.json"
            result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"[OK] {pid} -> {result_path}")
        finally:
            if agent is not None and hasattr(agent, "cleanup"):
                agent.cleanup()
            if actions is not None:
                actions.cleanup()
            if use_wandb:
                orchestrator.finish_wandb()

    print(f"\nExperiment directory: {exp_dir}")
    print("Saved files:")
    for idx, pid in enumerate(selected, start=1):
        print(f"- {exp_dir / f'result_{idx:02d}_{pid}.json'}")


if __name__ == "__main__":
    main()
