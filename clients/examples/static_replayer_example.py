"""
Static Replayer Client Usage Examples

Demonstrates various ways to use the static replayer client.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from clients.static_replayer import StaticReplayerClient


def example_1_list_configs():
    """Example 1: List all available configurations"""
    print("\n" + "="*60)
    print("Example 1: List Available Configurations")
    print("="*60 + "\n")

    client = StaticReplayerClient()
    configs = client.list_available_configs()

    print(f"Found {len(configs)} configurations:\n")
    for config in configs:
        print(f"- {config['name']}: {config['dataset_name']}")


def example_2_deploy_single():
    """Example 2: Deploy a single dataset"""
    print("\n" + "="*60)
    print("Example 2: Deploy Single Dataset")
    print("="*60 + "\n")

    client = StaticReplayerClient()

    # Deploy OpenRCA Bank
    result = client.deploy("openrca_bank")

    if result['status'] == 'deployed':
        print("\n✓ Deployment successful!")
        print(f"  Dataset: {result['dataset_name']}")
        print(f"  Namespace: {result['namespace']}")
        print(f"  Deploy time: {result['deploy_time_seconds']:.1f}s")

        # Access services
        print("\n📍 Access Points:")
        print("  - Elasticsearch: http://localhost:9200")
        print("  - Prometheus: http://localhost:9090")
        print("  - Jaeger: http://localhost:16686")

        # Cleanup
        input("\nPress Enter to cleanup...")
        client.cleanup("openrca_bank")


def example_3_deploy_multiple():
    """Example 3: Deploy multiple datasets sequentially"""
    print("\n" + "="*60)
    print("Example 3: Deploy Multiple Datasets")
    print("="*60 + "\n")

    client = StaticReplayerClient()

    configs = [
        "openrca_bank",
        "openrca_telecom"
    ]

    results = client.deploy_multiple(configs)

    print("\n📊 Results Summary:")
    for result in results:
        status_icon = "✓" if result['status'] == 'deployed' else "✗"
        print(f"  {status_icon} {result['config_name']}: {result['status']}")


def example_4_with_custom_results_dir():
    """Example 4: Deploy with custom results directory"""
    print("\n" + "="*60)
    print("Example 4: Custom Results Directory")
    print("="*60 + "\n")

    client = StaticReplayerClient()
    client.results_dir = Path("./my_results")

    result = client.deploy("openrca_bank")

    print(f"\nResults saved to: {client.results_dir}")


def example_5_programmatic_usage():
    """Example 5: Programmatic usage in your code"""
    print("\n" + "="*60)
    print("Example 5: Programmatic Usage")
    print("="*60 + "\n")

    from aiopslab.service.apps.static_replayer import StaticReplayer

    # Direct usage without client
    replayer = StaticReplayer("openrca_bank")

    # Get configuration info
    print(f"Dataset: {replayer.dataset_config['dataset_name']}")
    print(f"Namespace: {replayer.namespace}")

    # Deploy
    print("\nDeploying...")
    replayer.deploy()

    print("\n✓ Deployed! Data is now streaming to:")
    print("  - Logs: Elasticsearch")
    print("  - Metrics: Prometheus")
    print("  - Traces: Jaeger")

    # Your analysis code here
    # ...

    # Cleanup
    input("\nPress Enter to cleanup...")
    replayer.cleanup()


if __name__ == "__main__":
    print("""
Static Replayer Client Examples
================================

Choose an example to run:
1. List available configurations
2. Deploy single dataset
3. Deploy multiple datasets
4. Custom results directory
5. Programmatic usage

Enter choice (1-5):
    """)

    try:
        choice = input().strip()

        if choice == "1":
            example_1_list_configs()
        elif choice == "2":
            example_2_deploy_single()
        elif choice == "3":
            example_3_deploy_multiple()
        elif choice == "4":
            example_4_with_custom_results_dir()
        elif choice == "5":
            example_5_programmatic_usage()
        else:
            print("Invalid choice")

    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
