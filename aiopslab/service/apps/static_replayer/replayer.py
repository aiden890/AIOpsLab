# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Static Replayer

Main class for replaying static datasets as real-time telemetry.
Orchestrates adapter, query parser, time remapper, bulk loaders, and replayers.
"""

from aiopslab.service.apps.base import Application
from aiopslab.paths import STATIC_REPLAYER_METADATA
from pathlib import Path
import subprocess
import time
import json


class StaticReplayer(Application):
    """Replay static datasets as real-time telemetry"""

    def __init__(self, dataset_config_name: str):
        """
        Initialize Static Replayer

        Args:
            dataset_config_name: Name of config file without .json extension
                                (e.g., "openrca_bank", "alibaba_cluster")
        """
        # Load metadata
        metadata_file = Path(__file__).parent.parent.parent / "metadata" / "static-replayer.json"
        super().__init__(str(metadata_file))

        # Load dataset configuration
        config_path = Path(__file__).parent / "config" / f"{dataset_config_name}.json"
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r") as f:
            self.dataset_config = json.load(f)

        # Create adapter
        self.adapter = self._create_adapter()

        # Parse query (if enabled)
        self.query_info = None
        if self.dataset_config.get('query', {}).get('enable', False):
            self._parse_query()

        # Create time remapper (if query info available)
        if self.query_info:
            from .time_mapping.time_remapper import TimeRemapper
            self.time_remapper = TimeRemapper(self.dataset_config, self.query_info)
            print(self.time_remapper.get_summary())
        else:
            self.time_remapper = None

        # Set namespace
        self.namespace = self.dataset_config['namespace']
        self.load_app_json()

        # Docker Compose path
        self.compose_file = Path(__file__).parent / "docker" / "docker-compose.yml"

    def _create_adapter(self):
        """Create dataset-specific adapter"""
        dataset_type = self.dataset_config['dataset_type']

        if dataset_type == 'openrca':
            from .adapters.openrca import OpenRCAAdapter
            return OpenRCAAdapter(self.dataset_config)
        elif dataset_type == 'alibaba':
            from .adapters.alibaba import AlibabaAdapter
            return AlibabaAdapter(self.dataset_config)
        elif dataset_type == 'acme':
            from .adapters.acme import AcmeAdapter
            return AcmeAdapter(self.dataset_config)
        else:
            raise ValueError(f"Unknown dataset type: {dataset_type}")

    def _parse_query(self):
        """Parse query using dataset-specific query parser"""
        try:
            query_parser = self.adapter.get_query_parser()
            task_id = self.dataset_config['query'].get('task_identifier')

            if not task_id:
                # Use first available task
                available_tasks = query_parser.list_tasks()
                if available_tasks:
                    task_id = available_tasks[0]
                    print(f"ℹ️  Using first available task: {task_id}")

            if task_id:
                self.query_info = query_parser.parse_task(task_id)
                print(f"✓ Parsed Query: {self.query_info}")

        except Exception as e:
            print(f"⚠️  Query parsing failed: {e}")
            print("   Continuing without query information...")

    def deploy(self):
        """
        Deploy Static Replayer

        Steps:
        1. Start infrastructure (Elasticsearch, Prometheus, Jaeger)
        2. Bulk load history data
        3. Start realtime replay
        """
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"Deploying: {self.dataset_config['dataset_name']}")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # 1. Start infrastructure
        self._start_infrastructure()

        # 2. Bulk load history (if enabled)
        if self.time_remapper and self.dataset_config['time_mapping'].get('enable_bulk_history', True):
            self._bulk_load_history()

        # 3. Start realtime replay (in-process or background)
        use_docker = self.dataset_config.get('use_docker_replayer', False)

        if use_docker:
            self._start_docker_replayer()
        else:
            self._start_inprocess_replayer()

        print("✓ Static Replayer deployed successfully!")

    def _start_infrastructure(self):
        """Start Elasticsearch, Prometheus, Jaeger via Docker Compose"""
        print("\n[1/3] Starting infrastructure...")

        if not self.compose_file.exists():
            print("  ⚠️  Docker Compose file not found, skipping infrastructure startup")
            print("     Please ensure Elasticsearch, Prometheus, and Jaeger are running")
            return

        env_vars = {
            'NAMESPACE': self.namespace,
        }
        env_str = ' '.join([f"{k}={v}" for k, v in env_vars.items()])

        cmd = f"{env_str} docker-compose -f {self.compose_file} up -d elasticsearch prometheus pushgateway jaeger"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"  ⚠️  Infrastructure startup had issues: {result.stderr}")
            print("     Continuing anyway (services may already be running)...")

        print("  Waiting for services to be ready...")
        time.sleep(20)
        self._check_services()

    def _check_services(self):
        """Check if services are accessible"""
        import requests

        services = {
            'Elasticsearch': 'http://localhost:9200',
            'Prometheus': 'http://localhost:9090/-/ready',
            'Jaeger': 'http://localhost:16686',
        }

        for name, url in services.items():
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    print(f"  ✓ {name} is ready")
            except Exception as e:
                print(f"  ✗ {name} is not ready: {e}")

    def _bulk_load_history(self):
        """Bulk load history data"""
        print("\n[2/3] Bulk loading history...")

        from .bulk_loader.elasticsearch_bulk import ElasticsearchBulkLoader
        from .bulk_loader.prometheus_bulk import PrometheusBulkLoader
        from .bulk_loader.jaeger_bulk import JaegerBulkLoader

        # Elasticsearch (Logs)
        if self.dataset_config['telemetry'].get('enable_log', False):
            print("  Loading logs...")
            try:
                es_loader = ElasticsearchBulkLoader('http://localhost:9200', self.namespace)
                log_count = es_loader.bulk_load(
                    self.adapter.load_logs(),
                    self.time_remapper
                )
                print(f"  ✓ Loaded {log_count} log entries")
            except Exception as e:
                print(f"  ✗ Log loading failed: {e}")

        # Prometheus (Metrics)
        if self.dataset_config['telemetry'].get('enable_metric', False):
            print("  Loading metrics...")
            try:
                prom_loader = PrometheusBulkLoader('http://localhost:9091', self.namespace)
                metric_count = prom_loader.bulk_load(
                    self.adapter.load_metrics(),
                    self.time_remapper
                )
                print(f"  ✓ Loaded {metric_count} metric samples")
            except Exception as e:
                print(f"  ✗ Metric loading failed: {e}")

        # Jaeger (Traces)
        if self.dataset_config['telemetry'].get('enable_trace', False):
            print("  Loading traces...")
            try:
                jaeger_loader = JaegerBulkLoader('http://localhost:14268/api/traces', self.namespace)
                trace_count = jaeger_loader.bulk_load(
                    self.adapter.load_traces(),
                    self.time_remapper
                )
                print(f"  ✓ Loaded {trace_count} trace spans")
            except Exception as e:
                print(f"  ✗ Trace loading failed: {e}")

        print("  ✓ History bulk loading completed!")

    def _start_inprocess_replayer(self):
        """Start replayers in-process (blocking)"""
        print("\n[3/3] Starting realtime replay (in-process)...")

        from .replayers.metric_replayer import MetricReplayer
        from .replayers.log_replayer import LogReplayer
        from .replayers.trace_replayer import TraceReplayer

        speed_factor = self.dataset_config['replay_config'].get('speed_factor', 1.0)

        # Start replayers (sequential for now, could be parallel with threading)
        if self.dataset_config['telemetry'].get('enable_metric', False):
            replayer = MetricReplayer(
                self.adapter,
                'http://localhost:9091',
                self.namespace,
                speed_factor,
                self.time_remapper
            )
            replayer.replay()

        if self.dataset_config['telemetry'].get('enable_log', False):
            replayer = LogReplayer(
                self.adapter,
                'http://localhost:9200',
                self.namespace,
                speed_factor,
                self.time_remapper
            )
            replayer.replay()

        if self.dataset_config['telemetry'].get('enable_trace', False):
            replayer = TraceReplayer(
                self.adapter,
                'http://localhost:14268/api/traces',
                self.namespace,
                speed_factor,
                self.time_remapper
            )
            replayer.replay()

    def _start_docker_replayer(self):
        """Start replayers in Docker container (background)"""
        print("\n[3/3] Starting realtime replay (Docker)...")
        print("  ⚠️  Docker replayer not implemented yet, falling back to in-process")
        self._start_inprocess_replayer()

    def delete(self):
        """Stop Docker Compose services"""
        if self.compose_file.exists():
            cmd = f"docker-compose -f {self.compose_file} down"
            subprocess.run(cmd, shell=True)

    def cleanup(self):
        """Cleanup all resources including volumes"""
        if self.compose_file.exists():
            cmd = f"docker-compose -f {self.compose_file} down -v"
            subprocess.run(cmd, shell=True)
        print("✓ Static Replayer cleaned up")
