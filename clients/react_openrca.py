"""ReAct client for OpenRCA static log replay problems.

Usage:
    # Run a single problem
    python clients/react_openrca.py --problem-id openrca_cb1-detection-0

    # Run all problems for a specific dataset
    python clients/react_openrca.py --dataset openrca_cb1

    # Run all problems for a specific task type
    python clients/react_openrca.py --dataset openrca_cb1 --task detection

    # List available problems
    python clients/react_openrca.py --list

    # List problems for a dataset
    python clients/react_openrca.py --list --dataset openrca_telecom

Available datasets:
    openrca_cb1       Market/cloudbed-1 (70 faults x 3 tasks = 210 problems)
    openrca_cb2       Market/cloudbed-2 (78 faults x 3 tasks = 234 problems)
    openrca_telecom   Telecom (52 faults x 3 tasks = 156 problems)
    openrca_bank      Bank (44 faults x 3 tasks = 132 problems)

Task types: detection, localization, analysis
"""

import argparse
import asyncio
import json

from aiopslab.orchestrator import Orchestrator
from aiopslab.orchestrator.static_problems import StaticProblemRegistry
from clients.react import Agent
from clients.utils.llm import GPTClient


def main():
    parser = argparse.ArgumentParser(description="ReAct agent for OpenRCA static problems")
    parser.add_argument("--problem-id", type=str, help="Specific problem ID to run")
    parser.add_argument("--dataset", type=str, help="Dataset prefix (openrca_cb1, openrca_telecom, etc.)")
    parser.add_argument("--task", type=str, choices=["detection", "localization", "analysis"],
                        help="Filter by task type")
    parser.add_argument("--max-steps", type=int, default=30, help="Max agent steps (default: 30)")
    parser.add_argument("--list", action="store_true", help="List available problems and exit")
    parser.add_argument("--llm", type=str, default="azure_key",
                        choices=["key", "azure_key"],
                        help="LLM auth type: 'key' for direct OpenAI, 'azure_key' for Azure OpenAI (default)")
    args = parser.parse_args()

    # Create the static problem registry
    registry = StaticProblemRegistry()

    # List mode
    if args.list:
        problem_ids = registry.get_problem_ids(task_type=args.task, dataset=args.dataset)
        print(f"Available problems: {len(problem_ids)}")
        for pid in sorted(problem_ids):
            print(f"  {pid}")
        return

    # Determine which problems to run
    if args.problem_id:
        problem_ids = [args.problem_id]
    else:
        problem_ids = registry.get_problem_ids(task_type=args.task, dataset=args.dataset)

    if not problem_ids:
        print("No problems found. Use --list to see available problems.")
        return

    print(f"Running {len(problem_ids)} problem(s)...")

    for pid in problem_ids:
        print(f"\n{'='*60}")
        print(f"Problem: {pid}")
        print(f"{'='*60}")

        agent = Agent()
        agent.llm = GPTClient(auth_type=args.llm)
        orchestrator = Orchestrator(registry=registry)
        orchestrator.register_agent(agent, name="react")

        try:
            problem_desc, instructs, apis = orchestrator.init_problem(pid)
            agent.init_context(problem_desc, instructs, apis)

            full_output = asyncio.run(orchestrator.start_problem(max_steps=args.max_steps))
            results = full_output.get("results", {})

            filename = f"react_{pid}.json"
            with open(filename, "w") as f:
                json.dump(results, f, indent=2)

            print(f"Results saved to {filename}")
            print(f"Success: {results.get('success', 'N/A')}")

        except Exception as e:
            print(f"Error while running problem {pid}: {e}")


if __name__ == "__main__":
    main()
