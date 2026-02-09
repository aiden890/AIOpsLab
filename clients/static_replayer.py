"""Static Telemetry Replayer Client for AIOpsLab.

A unified client interface for:
1. Deploying standalone static dataset replayers
2. Running static problems with Orchestrator integration

Supports OpenRCA, Alibaba, and other static datasets with configurable options.
"""

import argparse
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
import sys

# Add aiopslab to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from aiopslab.service.apps.static_replayer import StaticReplayer


class StaticReplayerClient:
    """Client for managing static telemetry replayers"""

    def __init__(self):
        self.active_replayers = {}
        self.results_dir = Path(__file__).parent.parent / "results" / "static_replayer"
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def list_available_configs(self):
        """List all available dataset configurations"""
        config_dir = Path(__file__).parent.parent / "aiopslab" / "service" / "apps" / "static_replayer" / "config"

        if not config_dir.exists():
            print("⚠️  Config directory not found")
            return []

        configs = []
        for config_file in config_dir.glob("*.json"):
            config_name = config_file.stem
            try:
                with open(config_file, "r") as f:
                    config_data = json.load(f)
                configs.append({
                    'name': config_name,
                    'dataset_name': config_data.get('dataset_name', 'Unknown'),
                    'dataset_type': config_data.get('dataset_type', 'Unknown'),
                    'namespace': config_data.get('namespace', 'Unknown'),
                    'telemetry': config_data.get('telemetry', {})
                })
            except Exception as e:
                print(f"⚠️  Failed to load config {config_name}: {e}")

        return configs

    def deploy(self, config_name: str, wait: bool = True):
        """
        Deploy a static replayer

        Args:
            config_name: Name of config file (e.g., "openrca_bank")
            wait: Wait for replay to complete

        Returns:
            dict: Deployment result with status and info
        """
        print(f"\n{'='*60}")
        print(f"Deploying Static Replayer: {config_name}")
        print(f"{'='*60}\n")

        start_time = time.time()

        try:
            # Create replayer
            replayer = StaticReplayer(config_name)
            self.active_replayers[config_name] = replayer

            # Deploy
            replayer.deploy()

            deploy_time = time.time() - start_time

            result = {
                'config_name': config_name,
                'dataset_name': replayer.dataset_config.get('dataset_name'),
                'namespace': replayer.namespace,
                'status': 'deployed',
                'deploy_time_seconds': deploy_time,
                'deployment_timestamp': datetime.now().isoformat(),
                'telemetry': replayer.dataset_config.get('telemetry', {}),
                'time_mapping': replayer.time_remapper.mapping if replayer.time_remapper else None,
                'query_info': replayer.query_info.to_dict() if replayer.query_info else None
            }

            # Save result
            result_file = self.results_dir / f"{config_name}_{int(time.time())}.json"
            with open(result_file, "w") as f:
                json.dump(result, f, indent=2)

            print(f"\n✓ Deployment completed in {deploy_time:.1f} seconds")
            print(f"  Result saved to: {result_file}")

            return result

        except Exception as e:
            print(f"\n✗ Deployment failed: {e}")
            return {
                'config_name': config_name,
                'status': 'failed',
                'error': str(e),
                'deploy_time_seconds': time.time() - start_time
            }

    def cleanup(self, config_name: str | None = None):
        """
        Cleanup deployed replayers

        Args:
            config_name: Specific config to cleanup, or None for all
        """
        if config_name:
            if config_name in self.active_replayers:
                print(f"Cleaning up {config_name}...")
                self.active_replayers[config_name].cleanup()
                del self.active_replayers[config_name]
                print(f"✓ Cleaned up {config_name}")
            else:
                print(f"⚠️  No active replayer found for {config_name}")
        else:
            print("Cleaning up all active replayers...")
            for name, replayer in self.active_replayers.items():
                print(f"  Cleaning up {name}...")
                replayer.cleanup()
            self.active_replayers.clear()
            print("✓ All replayers cleaned up")

    def deploy_multiple(self, config_names: list, sequential: bool = True):
        """
        Deploy multiple replayers

        Args:
            config_names: List of config names
            sequential: Run sequentially (True) or in parallel (False)

        Returns:
            list: List of deployment results
        """
        results = []

        if sequential:
            for config_name in config_names:
                result = self.deploy(config_name)
                results.append(result)

                # Cleanup after each to free resources
                if result['status'] == 'deployed':
                    print(f"\n  Waiting 10 seconds before next deployment...")
                    time.sleep(10)
                    self.cleanup(config_name)
        else:
            # Parallel deployment not implemented yet
            print("⚠️  Parallel deployment not yet supported, using sequential")
            return self.deploy_multiple(config_names, sequential=True)

        # Save summary
        summary_file = self.results_dir / f"batch_{int(time.time())}.json"
        with open(summary_file, "w") as f:
            json.dump({
                'configs': config_names,
                'results': results,
                'timestamp': datetime.now().isoformat()
            }, f, indent=2)

        print(f"\n{'='*60}")
        print(f"Batch Deployment Summary")
        print(f"{'='*60}")
        print(f"Total configs: {len(config_names)}")
        print(f"Successful: {sum(1 for r in results if r['status'] == 'deployed')}")
        print(f"Failed: {sum(1 for r in results if r['status'] == 'failed')}")
        print(f"Summary saved to: {summary_file}")

        return results

    def list_problems(self):
        """List all available static problems"""
        static_problems = {
            "openrca-bank-detection-0": {
                "name": "OpenRCA Bank Detection",
                "dataset": "OpenRCA Bank",
                "task": "Detection",
                "class": "OpenRCABankDetection"
            },
            "openrca-telecom-detection-0": {
                "name": "OpenRCA Telecom Detection",
                "dataset": "OpenRCA Telecom",
                "task": "Detection",
                "class": "OpenRCATelecomDetection"
            },
            "openrca-market-cb1-detection-0": {
                "name": "OpenRCA Market Cloudbed-1 Detection",
                "dataset": "OpenRCA Market Cloudbed-1",
                "task": "Detection",
                "class": "OpenRCAMarketCloudbed1Detection"
            },
            "openrca-market-cb2-detection-0": {
                "name": "OpenRCA Market Cloudbed-2 Detection",
                "dataset": "OpenRCA Market Cloudbed-2",
                "task": "Detection",
                "class": "OpenRCAMarketCloudbed2Detection"
            }
        }
        return static_problems

    def run_problem(self, problem_id: str, agent_name: str = "react", max_steps: int = 30):
        """
        Run a static problem with Orchestrator

        Args:
            problem_id: Problem ID (e.g., "openrca-bank-detection-0")
            agent_name: Agent to use (default: "react")
            max_steps: Maximum number of steps

        Returns:
            dict: Problem results
        """
        print(f"\n{'='*60}")
        print(f"Running Static Problem: {problem_id}")
        print(f"{'='*60}\n")

        try:
            from aiopslab.orchestrator import Orchestrator

            # Import agent
            if agent_name == "react":
                from clients.react import Agent
            else:
                raise ValueError(f"Unknown agent: {agent_name}")

            # Get problem class
            problems = self.list_problems()
            if problem_id not in problems:
                raise ValueError(f"Unknown problem: {problem_id}")

            problem_info = problems[problem_id]
            problem_class_name = problem_info['class']

            # Import problem class
            from aiopslab.orchestrator.static_problems.openrca_detection import (
                OpenRCABankDetection,
                OpenRCATelecomDetection,
                OpenRCAMarketCloudbed1Detection,
                OpenRCAMarketCloudbed2Detection,
            )

            problem_classes = {
                "OpenRCABankDetection": OpenRCABankDetection,
                "OpenRCATelecomDetection": OpenRCATelecomDetection,
                "OpenRCAMarketCloudbed1Detection": OpenRCAMarketCloudbed1Detection,
                "OpenRCAMarketCloudbed2Detection": OpenRCAMarketCloudbed2Detection,
            }

            problem_class = problem_classes[problem_class_name]

            # Initialize
            agent = Agent()
            orchestrator = Orchestrator()
            orchestrator.register_agent(agent, name=agent_name)

            # Create problem instance
            problem = problem_class()

            # Initialize session if not exists
            if orchestrator.session is None:
                from aiopslab.session import Session
                from aiopslab.paths import RESULTS_DIR
                orchestrator.session = Session(results_dir=RESULTS_DIR)

            # Set problem (this creates the problem dir)
            orchestrator.session.set_problem(problem, pid=problem_id)

            # Get problem description and task instructions
            # Build app summary
            dataset_name = problem.replayer.dataset_config.get('dataset_name', 'Static Dataset')
            namespace = problem.namespace
            query_info = problem.query_info

            problem_desc = f"""Service Name: {dataset_name}
Namespace: {namespace}
Description: Static dataset replay for fault detection.

Dataset Information:
- Task: {query_info.task_id if query_info else 'N/A'}
- Time Range: {query_info.time_range['start_str']} ~ {query_info.time_range['end_str'] if query_info else 'N/A'}
- Known Faults: {len(problem.expected_faults)} fault(s) recorded in this time period"""

            # Build task instructions using task_desc from DetectionTask
            task_desc_template = problem.task_desc
            instructions = task_desc_template.format(app_summary=problem_desc)

            # Get APIs
            apis = problem.get_available_actions()

            agent.init_context(problem_desc, instructions, apis)

            # Run problem
            print(f"Starting problem with {agent_name} agent (max {max_steps} steps)...")
            start_time = time.time()

            full_output = asyncio.run(orchestrator.start_problem(max_steps=max_steps))
            results = full_output.get("results", {})

            duration = time.time() - start_time

            # Save results
            result_file = self.results_dir / f"{problem_id}_{agent_name}_{int(time.time())}.json"
            with open(result_file, "w") as f:
                json.dump({
                    "problem_id": problem_id,
                    "problem_name": problem_info['name'],
                    "agent": agent_name,
                    "max_steps": max_steps,
                    "duration_seconds": duration,
                    "results": results,
                    "timestamp": datetime.now().isoformat()
                }, f, indent=2)

            print(f"\n✓ Problem completed in {duration:.1f} seconds")
            print(f"  Result saved to: {result_file}")

            # Print summary
            if "success" in results:
                status = "✓ SUCCESS" if results["success"] else "✗ FAILED"
                print(f"  Status: {status}")

            return results

        except Exception as e:
            print(f"\n✗ Problem execution failed: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    def run_all_problems(self, agent_name: str = "react", max_steps: int = 30):
        """
        Run all static problems

        Args:
            agent_name: Agent to use
            max_steps: Maximum steps per problem

        Returns:
            dict: Summary of all results
        """
        problems = self.list_problems()
        results = {}

        print(f"\n{'='*60}")
        print(f"Running All Static Problems ({len(problems)} total)")
        print(f"{'='*60}\n")

        for problem_id in problems:
            result = self.run_problem(problem_id, agent_name, max_steps)
            results[problem_id] = result

            # Small delay between problems
            time.sleep(5)

        # Save summary
        summary_file = self.results_dir / f"all_problems_{agent_name}_{int(time.time())}.json"
        with open(summary_file, "w") as f:
            json.dump({
                "agent": agent_name,
                "total_problems": len(problems),
                "results": results,
                "timestamp": datetime.now().isoformat()
            }, f, indent=2)

        print(f"\n{'='*60}")
        print(f"All Problems Summary")
        print(f"{'='*60}")
        print(f"Total: {len(problems)}")
        print(f"Successful: {sum(1 for r in results.values() if r.get('success', False))}")
        print(f"Failed: {sum(1 for r in results.values() if not r.get('success', True))}")
        print(f"Summary saved to: {summary_file}")

        return results


def main():
    parser = argparse.ArgumentParser(
        description="Static Telemetry Replayer Client - Unified interface for standalone and problem-based usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Standalone Mode (Direct Replayer):
  # List available datasets
  python clients/static_replayer.py --list

  # Deploy single dataset
  python clients/static_replayer.py --deploy openrca_bank

  # Deploy multiple datasets
  python clients/static_replayer.py --deploy openrca_bank openrca_telecom

  # Deploy all OpenRCA datasets
  python clients/static_replayer.py --deploy-all-openrca

Problem Mode (Orchestrator Integration):
  # List available problems
  python clients/static_replayer.py --list-problems

  # Run single problem with agent
  python clients/static_replayer.py --run-problem openrca-bank-detection-0

  # Run all problems
  python clients/static_replayer.py --run-all-problems

  # Run with specific agent
  python clients/static_replayer.py --run-problem openrca-bank-detection-0 --agent react

Cleanup:
  python clients/static_replayer.py --cleanup
        """
    )

    # Standalone mode arguments
    standalone_group = parser.add_argument_group('Standalone Mode')
    standalone_group.add_argument(
        "--list",
        action="store_true",
        help="List all available dataset configurations"
    )

    standalone_group.add_argument(
        "--deploy",
        nargs="+",
        metavar="CONFIG",
        help="Deploy one or more datasets (e.g., openrca_bank)"
    )

    standalone_group.add_argument(
        "--deploy-all-openrca",
        action="store_true",
        help="Deploy all OpenRCA datasets"
    )

    # Problem mode arguments
    problem_group = parser.add_argument_group('Problem Mode (Orchestrator)')
    problem_group.add_argument(
        "--list-problems",
        action="store_true",
        help="List all available static problems"
    )

    problem_group.add_argument(
        "--run-problem",
        metavar="PROBLEM_ID",
        help="Run a specific problem (e.g., openrca-bank-detection-0)"
    )

    problem_group.add_argument(
        "--run-all-problems",
        action="store_true",
        help="Run all static problems"
    )

    problem_group.add_argument(
        "--agent",
        default="react",
        help="Agent to use for problem solving (default: react)"
    )

    problem_group.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Maximum steps for agent (default: 30)"
    )

    # Common arguments
    parser.add_argument(
        "--cleanup",
        nargs="?",
        const="all",
        metavar="CONFIG",
        help="Cleanup deployed replayer(s)"
    )

    parser.add_argument(
        "--results-dir",
        type=str,
        help="Custom results directory"
    )

    args = parser.parse_args()

    # Create client
    client = StaticReplayerClient()

    if args.results_dir:
        client.results_dir = Path(args.results_dir)
        client.results_dir.mkdir(parents=True, exist_ok=True)

    # Handle commands
    # Standalone mode
    if args.list:
        print("\n" + "="*60)
        print("Available Dataset Configurations")
        print("="*60 + "\n")

        configs = client.list_available_configs()
        if not configs:
            print("No configurations found")
        else:
            for i, config in enumerate(configs, 1):
                print(f"{i}. {config['name']}")
                print(f"   Dataset: {config['dataset_name']}")
                print(f"   Type: {config['dataset_type']}")
                print(f"   Namespace: {config['namespace']}")
                print(f"   Telemetry: ", end="")
                telem = config['telemetry']
                enabled = [k.replace('enable_', '') for k, v in telem.items() if v]
                print(", ".join(enabled) if enabled else "None")
                print()

    elif args.deploy:
        client.deploy_multiple(args.deploy)

    elif args.deploy_all_openrca:
        openrca_configs = [
            "openrca_bank",
            "openrca_telecom",
            "openrca_market_cloudbed1",
            "openrca_market_cloudbed2"
        ]
        print(f"\nDeploying all OpenRCA datasets: {', '.join(openrca_configs)}\n")
        client.deploy_multiple(openrca_configs)

    # Problem mode
    elif args.list_problems:
        print("\n" + "="*60)
        print("Available Static Problems")
        print("="*60 + "\n")

        problems = client.list_problems()
        for i, (pid, info) in enumerate(problems.items(), 1):
            print(f"{i}. {pid}")
            print(f"   Name: {info['name']}")
            print(f"   Dataset: {info['dataset']}")
            print(f"   Task: {info['task']}")
            print()

    elif args.run_problem:
        client.run_problem(args.run_problem, args.agent, args.max_steps)

    elif args.run_all_problems:
        client.run_all_problems(args.agent, args.max_steps)

    # Cleanup
    elif args.cleanup:
        if args.cleanup == "all":
            client.cleanup()
        else:
            client.cleanup(args.cleanup)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
