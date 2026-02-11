#!/usr/bin/env python3
"""Client for running static RCA experiments with agents."""

import argparse
import asyncio
import json
import logging

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from aiopslab.utils.logging_config import setup_logging
from clients.react import Agent

# Setup logging
logger = logging.getLogger(__name__)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run static RCA experiment with an agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with ReAct agent on OpenRCA Bank task 1
  python clients/static_rca_client.py --agent react --problem-id openrca_bank_task1_0

  # Run with GPT agent, custom results dir
  python clients/static_rca_client.py --agent gpt --problem-id openrca_bank_task6 \
      --results-dir my_results --max-steps 20
        """
    )

    parser.add_argument(
        "--agent",
        type=str,
        required=True,
        help="Agent to use (e.g., 'react', 'gpt')"
    )

    parser.add_argument(
        "--problem-id",
        type=str,
        required=True,
        help="Problem ID (e.g., 'openrca_bank_task1_0')"
    )

    parser.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Maximum number of agent steps (default: 30)"
    )

    parser.add_argument(
        "--results-dir",
        type=str,
        default="results/static_rca",
        help="Base directory for results (default: results/static_rca)"
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    return parser.parse_args()


async def run_static_rca(agent_name: str, problem_id: str, max_steps: int, results_dir: str, debug: bool = False):
    """
    Run a static RCA experiment with an agent.

    Follows the same pattern as the regular client (react.py).

    Args:
        agent_name: Name of agent to use
        problem_id: Problem identifier
        max_steps: Maximum number of steps
        results_dir: Results directory
        debug: Enable debug logging

    Returns:
        Results dictionary
    """
    # Setup logging
    log_level = "DEBUG" if debug else "INFO"
    setup_logging(level=log_level)

    logger.info("=" * 80)
    logger.info("STATIC RCA CLIENT")
    logger.info("=" * 80)
    logger.info(f"Agent: {agent_name}")
    logger.info(f"Problem: {problem_id}")
    logger.info(f"Max steps: {max_steps}")
    logger.info("")

    # Get agent from registry
    agent = Agent()


    # Create orchestrator
    orchestrator = StaticOrchestrator(results_base_dir=results_dir)
    logger.info("✓ Orchestrator created")

    # Register agent (following react.py pattern)
    orchestrator.register_agent(agent, name=agent_name)
    logger.info(f"✓ Agent registered")
    logger.info("")

    try:
        logger.info(f"{'*' * 30}")
        logger.info(f"Starting problem {problem_id} with agent {agent_name}")
        logger.info(f"{'*' * 30}")
        logger.info("")

        # Initialize problem (following react.py pattern)
        problem_desc, instructs, apis = orchestrator.init_static_problem(problem_id)
        logger.info("✓ Problem initialized")
        logger.info("")

        # Initialize agent context (following react.py pattern)
        agent.init_context(problem_desc, instructs, apis)
        logger.info("✓ Agent context initialized")
        logger.info("")

        # Start problem solving
        full_output = await orchestrator.start_problem(max_steps=max_steps)
        results = full_output.get("results", {})

        logger.info("")
        logger.info(f"{'*' * 30}")
        logger.info(f"Successfully completed problem {problem_id}")
        logger.info(f"{'*' * 30}")
        logger.info("")

        # Display results
        logger.info("=" * 80)
        logger.info("RESULTS")
        logger.info("=" * 80)
        logger.info(f"Success: {results.get('success', False)}")
        logger.info(f"Feedback: {results.get('feedback', 'N/A')}")
        logger.info("")

        if 'evaluation' in results:
            logger.info("Evaluation:")
            for key, value in results['evaluation'].items():
                logger.info(f"  {key}: {value}")
            logger.info("")

        # Save results to file
        filename = f"static_{agent_name}_{problem_id}.json"
        with open(filename, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Results saved to: {filename}")

        return results

    except Exception as e:
        logger.error(f"Failed to process problem {problem_id}. Error: {e}", exc_info=True)
        raise


def main():
    """Main entry point."""
    args = parse_args()

    try:
        results = asyncio.run(run_static_rca(
            agent_name=args.agent,
            problem_id=args.problem_id,
            max_steps=args.max_steps,
            results_dir=args.results_dir,
            debug=args.debug
        ))

        # Exit with success/failure code
        exit(0 if results.get('success', False) else 1)

    except Exception as e:
        logger.error(f"Client failed: {e}")
        exit(1)


if __name__ == "__main__":
    main()
