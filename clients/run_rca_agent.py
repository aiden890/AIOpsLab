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
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from clients.openrca_rca.agent import OpenRCARCAAgent
from clients.openrca_rca.prompts.telemetry_guide import TELEMETRY_GUIDE

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("openrca_rca")


def parse_args():
    parser = argparse.ArgumentParser(description="OpenRCA RCA Agent runner")
    parser.add_argument("--problem", type=str, help="Single problem ID to run")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g., openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g., task_1, task_7)")
    parser.add_argument("--max-steps", type=int, default=25, help="Max orchestrator steps")
    parser.add_argument("--results-dir", type=str, default="results/rca_agent")
    parser.add_argument("--api-config", type=str, default=None, help="Path to api_config.yaml")
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

    orchestrator = StaticOrchestrator(results_dir=str(results_dir))

    # Select problems to run
    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    logger.info(f"Running {len(problem_ids)} problems")
    logger.info(f"Results dir: {results_dir}\n")

    total_score = 0
    completed = 0

    for pid in problem_ids:
        print(f"\n{'=' * 60}")
        print(f"Problem: {pid}")
        print(f"{'=' * 60}\n")

        agent = OpenRCARCAAgent(api_config_path=args.api_config)
        orchestrator.register_agent(agent, name="openrca-rca")

        try:
            # 1. Initialize problem (deploy, setup)
            #    Inject RCA-specific telemetry guide before getting task description
            problem_desc, instructs, apis = orchestrator.init_problem(pid)

            # Override telemetry guide with RCA agent's version and regenerate
            problem = orchestrator.session.problem
            problem.telemetry_guide = TELEMETRY_GUIDE
            problem_desc = problem.get_task_description()

            agent.init_context(problem_desc, instructs, apis)

            # 2. Inject Executor callback into the actions object
            dataset_key = extract_dataset_key(pid)
            agent.set_actions(problem._actions, problem.namespace, dataset_key,
                              max_steps=args.max_steps)

            # 3. Link executor trajectory to session (populated during loop)
            orchestrator.session.extra["executor_trajectory"] = agent.executor_trajectory

            # 4. Print initial problem setup
            orchestrator.sprint.problem_init(problem_desc, instructs, apis)

            # 5. Run orchestrator loop (ask_agent → ask_env → repeat)
            full_output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = full_output.get("results", {})

            score = results.get("score", 0)
            total_score += score
            completed += 1

            print(f"\nScore: {score}")
            print(f"Passing: {results.get('passing_criteria', 'N/A')}")
            print(f"Failing: {results.get('failing_criteria', 'N/A')}")

        except Exception as e:
            logger.error(f"Error running {pid}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            agent.cleanup()

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems completed.")
    if completed > 0:
        print(f"Average score: {total_score / completed:.3f}")
    print(f"{'=' * 60}")
