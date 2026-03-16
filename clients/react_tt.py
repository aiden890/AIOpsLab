"""Run script for RE2-TT Root Cause Analysis.

Runs the ReAct agent on RE2-TT fault injection cases.
Each run is identified by --eval-id, which is used as:
  - subdirectory name under results_dir  (results/re2tt/<eval-id>/)
  - agent name registered with the orchestrator
  - scores.csv filename                  (results/re2tt/<eval-id>/scores.csv)

This makes parallel runs fully independent — pass different --eval-id values:

    python clients/react_tt.py --eval-id run-cpu --fault cpu &
    python clients/react_tt.py --eval-id run-mem --fault mem &

Usage:
    # Run all RE2-TT cases
    python clients/react_tt.py --eval-id my-run

    # Run a subset by fault type
    python clients/react_tt.py --eval-id run-cpu --fault cpu

    # Run a specific problem
    python clients/react_tt.py --eval-id dbg --problem re2tt-auth-cpu-1

    # Smoke test: first 2 cases only
    python clients/react_tt.py --eval-id smoke --test
"""

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.orchestrator.static_actions.rca_executor import StaticRCAActionsWithExecutor
from clients.react_tt_client import (
    Agent, TT_SCHEMA, ALLOWED_ACTIONS, append_score,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("react_tt")


def parse_args():
    parser = argparse.ArgumentParser(description="ReAct agent run script for RE2-TT RCA")
    parser.add_argument("--problem",      type=str, help="Single problem ID to run")
    parser.add_argument("--fault",        type=str, help="Filter by fault type (cpu/mem/delay/loss/disk/socket)")
    parser.add_argument("--service",      type=str, help="Filter by service name substring")
    parser.add_argument("--max-steps",    type=int, default=1000)
    parser.add_argument("--results-dir",  type=str, default="results/re2tt",
                        help="Base results directory (default: results/re2tt)")
    parser.add_argument("--eval-id",      type=str, default=None,
                        help="Unique run identifier. Used as subdirectory and agent name. "
                             "Auto-generated if not provided.")
    parser.add_argument("--api-config",   type=str, default=None,
                        help="Path to api_config.yaml for the Executor sub-LLM")
    parser.add_argument("--start-index",  type=int, default=0,
                        help="Skip the first N problems (default: 0)")
    parser.add_argument("--test",         action="store_true",
                        help="Smoke test: run first 2 matching problems only")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    eval_id = args.eval_id or uuid.uuid4().hex[:8]

    # Each run gets its own subdirectory: results/re2tt/<eval-id>/
    run_dir = Path(args.results_dir) / eval_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Scores for this run are aggregated in a single CSV at the run root
    run_scores_path = run_dir / "scores.csv"

    api_config_path = args.api_config or str(
        Path(__file__).parent / "openrca_rca" / "api_config.yaml"
    )

    # Agent name includes eval_id so parallel runs are distinguishable in logs
    agent_name = f"react-tt-{eval_id}"

    orchestrator = StaticOrchestrator(results_dir=str(run_dir), eval_id=eval_id)

    # Select problems
    if args.problem:
        problem_ids = [args.problem]
    else:
        all_ids = orchestrator.probs.get_problem_ids(dataset="re2tt")
        if args.fault:
            all_ids = [pid for pid in all_ids if f"-{args.fault}-" in pid]
        if args.service:
            all_ids = [pid for pid in all_ids if args.service in pid]
        problem_ids = sorted(all_ids)

    if args.start_index > 0:
        print(f"Skipping first {args.start_index} problems (--start-index={args.start_index})")
        problem_ids = problem_ids[args.start_index:]

    if args.test:
        problem_ids = problem_ids[:2]

    print(f"{'='*60}")
    print(f"Eval ID:     {eval_id}")
    print(f"Agent name:  {agent_name}")
    print(f"Run dir:     {run_dir}")
    print(f"Scores:      {run_scores_path}")
    print(f"Problems:    {len(problem_ids)}")
    print(f"{'='*60}\n")

    for pid in problem_ids:
        print(f"\n{'='*60}")
        print(f"Problem: {pid}")
        print(f"{'='*60}\n")

        agent = Agent()
        orchestrator.register_agent(agent, name=agent_name)

        actions: StaticRCAActionsWithExecutor | None = None
        fault_info = {}

        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            problem = orchestrator.session.problem

            # Per-problem save directory (inside the run dir)
            save_dir = orchestrator.session.get_save_dir()

            # Extract fault metadata for the scores CSV
            app = problem.app
            fault_info = {
                "service": getattr(app, "fault_service", ""),
                "fault":   getattr(app, "fault_type",   ""),
            }

            # Notebook path: <save_dir>/<problem-part>_executor.ipynb
            task_part = pid.split("-", maxsplit=1)[1] if "-" in pid else pid
            notebook_path = save_dir / f"{task_part}_executor.ipynb"

            actions = StaticRCAActionsWithExecutor(
                container_name=app.get_container_name(),
                possible_root_causes=app.dataset_config.get("possible_root_causes"),
                telemetry_flags=app.dataset_config.get("telemetry"),
                use_executor=True,
                use_hypothesis=False,
            )
            actions.setup_executor(
                background=TT_SCHEMA,
                api_config_path=api_config_path,
                namespace=problem.namespace,
                logger=logger,
                notebook_save_path=str(notebook_path),
            )

            # Swap actions on the problem and rebuild API list
            problem._actions = actions
            problem.actions = actions
            apis = {k: v for k, v in problem.get_available_actions().items()
                    if k in ALLOWED_ACTIONS}

            agent.init_context(problem_desc, instructs, apis)
            orchestrator._system_message = agent.system_message
            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            full_output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = full_output.get("results", {})

            # Write to both per-problem and run-level scores CSV
            append_score(save_dir / "scores.csv", eval_id, agent.get_model_name(),
                         pid, results, fault_info=fault_info)
            append_score(run_scores_path, eval_id, agent.get_model_name(),
                         pid, results, fault_info=fault_info)

            print(f"\nScore: {results.get('score', 'N/A')}  "
                  f"Success: {results.get('success', 'N/A')}")

        except Exception as e:
            logger.error(f"Error running {pid}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if actions is not None:
                actions.cleanup()

    print(f"\n{'='*60}")
    print(f"Done! {len(problem_ids)} problems completed.")
    print(f"Run dir:  {run_dir}")
    print(f"Scores:   {run_scores_path}")
    print(f"{'='*60}")
