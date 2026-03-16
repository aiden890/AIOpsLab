"""Backward RCA runner: given ground truth, builds the rule library.

For each problem, the verifier agent:
  1. Collects evidence proving the known root cause (Phase 1)
  2. Extracts an abstract reusable rule (Phase 2)
  3. Generalizes critical executor code into a parameterized snippet (Phase 3)

Results are saved to the rule library for use by forward-RCA agents via
the query_rule_library() and run_snippet() actions.

Usage:
    # Verify a single problem
    python clients/run_verifier.py --problem openrca_bank-task_6-0

    # Verify all Bank problems (builds rule library incrementally)
    python clients/run_verifier.py --dataset openrca_bank

    # Custom library location and eval ID
    python clients/run_verifier.py --dataset openrca_bank \\
        --rule-library-dir results/my_library --eval-id exp_01
"""

import argparse
import csv
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.utils.rule_store import RuleStore
from clients.openrca_rca.verifier_agent import VerifierAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_verifier")

SCORE_FIELDS = [
    "timestamp", "eval_id", "case_id",
    "dataset", "task_type", "phases_completed",
    "rule_id", "snippet_id",
]


def _check_embedding_availability():
    """Warn if sentence-transformers is not installed."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        logger.warning(
            "sentence-transformers is NOT installed. "
            "Embedding search will fall back to keyword search. "
            "Install with: pip install sentence-transformers"
        )


def _dataset_from_problem_id(problem_id: str) -> str:
    """'openrca_bank-task_6-0' → 'openrca_bank'"""
    return problem_id.rsplit("-", 2)[0]


def _append_score(scores_path: Path, eval_id: str, case_id: str, record: dict):
    """Append one row to the verification scores CSV."""
    is_new = not scores_path.exists()
    with open(scores_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_id": eval_id,
            "case_id": case_id,
            "dataset": record.get("dataset", ""),
            "task_type": record.get("task_type", ""),
            "phases_completed": record.get("phases_completed", 0),
            "rule_id": record.get("rule_id", ""),
            "snippet_id": record.get("snippet_id", ""),
        })


def run_verification(
    problem_id: str,
    orchestrator: StaticOrchestrator,
    rule_store: RuleStore,
    api_config_path: str | None = None,
    max_steps: int = 15,
) -> dict:
    """Run backward RCA verification for a single problem.

    Args:
        problem_id:      Problem ID string (e.g., "openrca_bank-task_6-0").
        orchestrator:    Initialized StaticOrchestrator instance.
        rule_store:      RuleStore instance to write results to.
        api_config_path: Optional path to LLM API config YAML.
        max_steps:       Maximum evidence-collection steps (phase 1).

    Returns:
        dict with keys: dataset, task_type, phases_completed, rule_id, snippet_id.
    """
    dataset = _dataset_from_problem_id(problem_id)

    # register_agent is required before init_problem (sets orchestrator.agent_name)
    agent = VerifierAgent(api_config_path=api_config_path)
    orchestrator.register_agent(agent, name="verifier")

    # Initialize problem (deploys Docker container, sets up telemetry APIs)
    problem_desc, instructs, apis = orchestrator.init_problem(problem_id)
    problem = orchestrator.session.problem

    # Get ground truth from dataset labels
    ground_truth = getattr(problem, "scoring_points", "")
    if not ground_truth:
        logger.warning(f"No scoring_points found for {problem_id} — using empty ground truth")

    task_type = getattr(problem, "task_type", "unknown")

    logger.info(f"Ground truth: {ground_truth[:150]}")

    # Set up telemetry guide for executor
    from clients.openrca_rca.prompts.telemetry_guide import build_executor_telemetry_guide
    enabled_types = getattr(problem._actions, "enabled_telemetry_types", None)
    problem.telemetry_guide = build_executor_telemetry_guide(enabled_types)

    agent.run(
        problem=problem,
        ground_truth=ground_truth,
        dataset_key=dataset,
        max_steps=max_steps,
    )

    # Save results to rule library
    save_output = agent.save_results(
        rule_store=rule_store,
        case_id=problem_id,
        dataset=dataset,
        task_type=task_type,
        ground_truth=ground_truth,
    )

    phases = agent.get_phases_completed()
    logger.info(
        f"Completed {phases}/3 phases | "
        f"rule={save_output.get('rule_id')} | "
        f"snippet={save_output.get('snippet_id')}"
    )

    return {
        "dataset": dataset,
        "task_type": task_type,
        "phases_completed": phases,
        "rule_id": save_output.get("rule_id"),
        "snippet_id": save_output.get("snippet_id"),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backward RCA verifier — builds rule library from ground truth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python clients/run_verifier.py --problem openrca_bank-task_6-0
  python clients/run_verifier.py --dataset openrca_bank
  python clients/run_verifier.py --dataset openrca_bank --task-type task_6
  python clients/run_verifier.py --dataset openrca_bank --rule-library-dir results/my_library
""",
    )
    parser.add_argument("--problem", type=str, help="Single problem ID to verify")
    parser.add_argument("--dataset", type=str, help="Filter by dataset (e.g., openrca_bank)")
    parser.add_argument("--task-type", type=str, help="Filter by task type (e.g., task_6)")
    parser.add_argument("--max-steps", type=int, default=15,
                        help="Max evidence-collection steps per problem (default: 15)")
    parser.add_argument("--rule-library-dir", type=str, default="results/rule_library",
                        help="Rule library output directory (default: results/rule_library)")
    parser.add_argument("--api-config", type=str, default=None,
                        help="Path to api_config.yaml")
    parser.add_argument("--eval-id", type=str, default=None,
                        help="Eval run identifier (default: auto UUID)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    _check_embedding_availability()

    rule_library_dir = Path(args.rule_library_dir)
    rule_library_dir.mkdir(parents=True, exist_ok=True)

    eval_id = args.eval_id or uuid.uuid4().hex[:8]
    scores_path = rule_library_dir / f"{eval_id}_verify_scores.csv"

    orchestrator = StaticOrchestrator(
        results_dir=str(rule_library_dir / "orchestrator_results"),
        eval_id=eval_id,
    )
    rule_store = RuleStore(str(rule_library_dir))

    if args.problem:
        problem_ids = [args.problem]
    else:
        problem_ids = orchestrator.probs.get_problem_ids(
            task_type=args.task_type,
            dataset=args.dataset,
        )

    logger.info(f"Verifying {len(problem_ids)} problem(s) | eval_id={eval_id}")
    logger.info(f"Rule library: {rule_library_dir}")
    logger.info(f"Scores: {scores_path}\n")

    completed = 0
    phases_total = 0

    for pid in problem_ids:
        print(f"\n{'=' * 60}")
        print(f"Verifying: {pid}")
        print(f"{'=' * 60}\n")

        try:
            result = run_verification(
                problem_id=pid,
                orchestrator=orchestrator,
                rule_store=rule_store,
                api_config_path=args.api_config,
                max_steps=args.max_steps,
            )

            _append_score(scores_path, eval_id, pid, result)
            completed += 1
            phases_total += result.get("phases_completed", 0)

            print(f"\nPhases completed: {result['phases_completed']}/3")
            print(f"Rule ID:    {result.get('rule_id', 'none')}")
            print(f"Snippet ID: {result.get('snippet_id', 'none')}")

        except Exception as e:
            logger.error(f"Error verifying {pid}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"Done! {completed}/{len(problem_ids)} problems verified.")
    if completed > 0:
        print(f"Average phases completed: {phases_total / completed:.1f}/3")
    print(f"Rule library: {rule_library_dir}")
    print(f"Scores: {scores_path}")
    print(f"{'=' * 60}")
