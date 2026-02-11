"""
Static Dataset Application

Manages the lifecycle of a static dataset deployment.
Handles config loading, query-based time mapping, and telemetry streaming.

Architecture:
  1. deploy() starts a Docker container with raw dataset bind-mounted
  2. Runs process_telemetry.py --mode init: loads initial time window (sync)
  3. Runs process_telemetry.py --mode stream: adds data over time (background)
  4. Processed telemetry stored at /agent/telemetry/ inside container
  5. Agent actions read from the container via `docker exec` (DockerStaticApp)
"""

from aiopslab.service.apps.base import Application
from aiopslab.service.dock import Docker
from aiopslab.paths import STATIC_DATASET_METADATA, TARGET_MICROSERVICES, BASE_PARENT_DIR
from pathlib import Path
import json
import os
import tempfile
import pandas as pd


class StaticDataset(Application):
    """Application class for static dataset deployments."""

    def __init__(self, dataset_config_name: str, query_index: int = None):
        """
        Args:
            dataset_config_name: Name of config file without .json extension
                                (e.g., "openrca_bank", "alibaba_cluster")
            query_index: Optional row index in query.csv. When provided,
                         sets up time mapping for that specific query.
        """
        super().__init__(str(STATIC_DATASET_METADATA))

        # Load dataset configuration
        config_path = Path(__file__).parent / "config" / f"{dataset_config_name}.json"
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r") as f:
            self.dataset_config = json.load(f)

        # Load base metadata first, then override namespace from dataset config
        self.load_app_json()
        self.namespace = self.dataset_config["namespace"]

        # Docker client
        self.docker = Docker()
        self.docker_deploy_path = TARGET_MICROSERVICES / "static_dataset"

        # Dataset path (where raw CSV files live)
        # Resolve relative paths against project root
        raw_path = Path(self.dataset_config["dataset_path"])
        if not raw_path.is_absolute():
            raw_path = BASE_PARENT_DIR / raw_path
        self.dataset_path = raw_path

        # Query and time mapping
        self.query_info = None
        self.time_remapper = None
        self._processing_config_path = None

        if query_index is not None:
            self._setup_for_query(query_index)
        elif self.dataset_config.get("query", {}).get("enable", False):
            self._parse_query()
            if self.query_info:
                from .time_mapping.time_remapper import TimeRemapper
                self.time_remapper = TimeRemapper(self.dataset_config, self.query_info)

    def _setup_for_query(self, query_index: int):
        """Set up time mapping for a specific query row."""
        query_file = self.dataset_path / self.dataset_config.get(
            "query", {}
        ).get("query_file", "query.csv")

        if not query_file.exists():
            print(f"Warning: Query file not found: {query_file}")
            return

        query_df = pd.read_csv(query_file)
        if query_index >= len(query_df):
            print(f"Warning: query_index {query_index} out of range")
            return

        row = query_df.iloc[query_index]
        instruction = row["instruction"]

        # Parse time range from instruction
        dataset_type = self.dataset_config["dataset_type"]
        if dataset_type == "openrca":
            from .time_mapping.openrca_query_parser import OpenRCAQueryParser
            query_config = self.dataset_config.get("query", {})
            parser = OpenRCAQueryParser(self.dataset_path, query_config)
            time_range = parser._extract_time_range_from_instruction(instruction)
            faults = parser._extract_faults(time_range)
            from .time_mapping.base_query_parser import QueryResult
            self.query_info = QueryResult(
                task_id=row["task_index"],
                time_range=time_range,
                faults=faults,
                metadata={
                    "instruction": instruction,
                    "scoring_points": row.get("scoring_points", ""),
                    "query_index": query_index,
                },
            )

        if self.query_info:
            from .time_mapping.time_remapper import TimeRemapper
            self.time_remapper = TimeRemapper(self.dataset_config, self.query_info)

    def _parse_query(self):
        """Parse query using dataset-specific query parser (legacy path)."""
        try:
            dataset_type = self.dataset_config["dataset_type"]

            if dataset_type == "openrca":
                from .time_mapping.openrca_query_parser import OpenRCAQueryParser
                query_config = self.dataset_config.get("query", {})
                query_parser = OpenRCAQueryParser(self.dataset_path, query_config)
            else:
                print(f"No query parser for dataset type: {dataset_type}")
                return

            task_id = self.dataset_config["query"].get("task_identifier")
            if not task_id:
                available_tasks = query_parser.list_tasks()
                if available_tasks:
                    task_id = available_tasks[0]

            if task_id:
                self.query_info = query_parser.parse_task(task_id)

        except Exception as e:
            print(f"Query parsing failed: {e}")

    def get_services(self) -> list[str]:
        """Return the list of service/component names for this dataset."""
        return self.dataset_config.get("services", [])

    def get_container_name(self) -> str:
        """Return the Docker container name for this dataset."""
        return f"static-dataset-{self.namespace}"

    def deploy(self):
        """Deploy static dataset via Docker.

        1. Write processing config to a temp file on host
        2. Start Docker container (detached) with raw dataset + config bind-mounted
        3. Run process_telemetry.py --mode init (synchronous initial window)
        4. Run process_telemetry.py --mode stream (detached background streaming)
        5. Telemetry is stored inside the container at /agent/telemetry/
        """
        print(f"Deploying: {self.dataset_config['dataset_name']}")

        # Build processing config for Docker
        mapping = self.time_remapper.mapping if self.time_remapper else {}
        processing_config = {
            "namespace": self.namespace,
            "data_mapping": self.dataset_config.get("data_mapping", {}),
            "telemetry": self.dataset_config.get("telemetry", {}),
            "replay": self.dataset_config.get("replay", {}),
            "time_offset": mapping.get("time_offset", 0),
            "init_start_original": mapping.get("init_start_original"),
            "init_end_original": mapping.get("init_end_original"),
        }
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="aiopslab_")
        with os.fdopen(fd, "w") as f:
            json.dump(processing_config, f, indent=2)
        self._processing_config_path = config_path

        # Set environment for Docker compose variable substitution
        env = self._get_docker_env()

        # Clean up previous container, then build and start (detached)
        self.docker.compose_down(cwd=str(self.docker_deploy_path), env=env)
        self.docker.compose_up(cwd=str(self.docker_deploy_path), env=env, build=True)

        container = self.get_container_name()

        # Step 1: Initial load (synchronous — blocks until done)
        print(f"  Loading initial telemetry window: {container}")
        result = self.docker.exec_in_container(
            container, "python /app/process_telemetry.py --mode init"
        )
        if result:
            print(result)

        # Step 2: Start background streaming (detached — returns immediately)
        print(f"  Starting telemetry stream in background")
        self.docker.exec_in_container(
            container, "python /app/process_telemetry.py --mode stream",
            detach=True,
        )

        if self.time_remapper:
            print(self.time_remapper.get_summary())

    def _get_docker_env(self):
        """Get environment variables for Docker compose."""
        env = os.environ.copy()
        env["NAMESPACE"] = self.namespace
        env["DATASET_PATH"] = str(self.dataset_path)
        if self._processing_config_path:
            env["PROCESSING_CONFIG"] = self._processing_config_path
        return env

    def delete(self):
        """Stop Docker containers."""
        try:
            self.docker.compose_down(
                cwd=str(self.docker_deploy_path), env=self._get_docker_env()
            )
        except Exception:
            pass
        self._cleanup_config()

    def cleanup(self):
        """Stop containers and clean up."""
        try:
            self.docker.compose_down(
                cwd=str(self.docker_deploy_path), env=self._get_docker_env()
            )
            self.docker.cleanup()
        except Exception:
            pass
        self._cleanup_config()

    def _cleanup_config(self):
        """Remove temporary processing config file."""
        if self._processing_config_path and os.path.exists(self._processing_config_path):
            os.unlink(self._processing_config_path)
            self._processing_config_path = None
