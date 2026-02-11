# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""OpenRCA Static Replayer Application."""

import logging
import time
from pathlib import Path
from typing import Dict, Any

from aiopslab.paths import BASE_PARENT_DIR
from aiopslab.service.apps.static_replayer.base import BaseStaticApp
from aiopslab.observer.static.openrca import (
    OpenRCATraceAPI,
    OpenRCALogAPI,
    OpenRCAMetricAPI
)

# Import TimeMapper components
import sys
sys.path.insert(0, str(BASE_PARENT_DIR / 'aiopslab-applications' / 'static-replayers' / 'openrca'))
from time_mapper import TimeMapper, parse_time_option
from datetime import datetime

logger = logging.getLogger(__name__)


class OpenRCAStaticApp(BaseStaticApp):
    """
    Static replayer application for OpenRCA datasets.

    Manages Docker container lifecycle for replaying OpenRCA telemetry data.
    Provides observer APIs for querying replayed trace, log, and metric data.
    """

    def __init__(self, config_file: str):
        """
        Initialize OpenRCA static replayer app.

        Args:
            config_file: Path to configuration YAML file
        """
        super().__init__(config_file)
        self.telemetry_apis: Dict[str, Any] = {}

    def get_dataset_path(self) -> Path:
        """
        Return path to OpenRCA dataset directory.

        The namespace may include suffixes like 'Bank_test' or 'Bank_task1',
        but the actual dataset is always the base name (e.g., 'Bank').

        Returns:
            Path to dataset directory in openrca_dataset/
        """
        namespace = self.config['dataset']['namespace']

        # Extract base dataset name (remove suffixes like _test, _task1, etc.)
        # Examples: Bank_test -> Bank, Telecom_task1 -> Telecom
        base_name = namespace.split('_')[0]

        # Handle Market subdatasets (Market/cloudbed-1)
        if '/' in namespace:
            dataset_path = BASE_PARENT_DIR / 'openrca_dataset' / namespace
        else:
            dataset_path = BASE_PARENT_DIR / 'openrca_dataset' / base_name

        if not dataset_path.exists():
            raise FileNotFoundError(
                f"OpenRCA dataset not found: {dataset_path}\n"
                f"Please ensure the dataset is available at this location."
            )

        logger.info(f"Using dataset: {dataset_path}")
        return dataset_path

    def get_docker_image(self) -> str:
        """
        Return Docker image name for OpenRCA replayer.

        Returns:
            Docker image tag
        """
        return "aiopslab-static-replayer-openrca:latest"

    def get_telemetry_apis(self) -> Dict[str, any]:
        """
        Get observer APIs for querying telemetry data.

        Returns:
            Dictionary mapping telemetry type to observer API instance
            Example: {'trace': OpenRCATraceAPI, 'log': OpenRCALogAPI, ...}
        """
        if self.telemetry_apis:
            return self.telemetry_apis

        output_path = self.get_output_path()
        enabled_types = self.config['telemetry']['enabled']

        logger.info(f"Initializing telemetry APIs for: {enabled_types}")

        # Create observer instances for enabled telemetry types
        if 'trace' in enabled_types:
            self.telemetry_apis['trace'] = OpenRCATraceAPI(output_path)
            logger.debug("✓ Trace API initialized")

        if 'log' in enabled_types:
            self.telemetry_apis['log'] = OpenRCALogAPI(output_path)
            logger.debug("✓ Log API initialized")

        if 'metric' in enabled_types:
            self.telemetry_apis['metric'] = OpenRCAMetricAPI(output_path)
            logger.debug("✓ Metric API initialized")

        logger.info(f"Initialized {len(self.telemetry_apis)} telemetry APIs")
        return self.telemetry_apis

    def _wait_for_replayer_ready(self, timeout: int = 300) -> bool:
        """
        Wait for replayer to finish Phase 1 (bulk history loading).

        The replayer writes a marker file when Phase 1 completes.
        This method polls for that marker file.

        Args:
            timeout: Maximum seconds to wait (default: 300 = 5 minutes)

        Returns:
            True if replayer is ready, False if timeout

        Raises:
            TimeoutError: If replayer doesn't become ready within timeout
        """
        logger.info("=" * 60)
        logger.info("WAITING FOR REPLAYER PHASE 1 TO COMPLETE")
        logger.info("=" * 60)

        output_path = self.get_output_path()
        enabled_telemetry = self.config['telemetry']['enabled']

        logger.info(f"Output directory: {output_path}")
        logger.info(f"Monitoring CSV files: {[f'{t}.csv' for t in enabled_telemetry]}")
        logger.info(f"Timeout: {timeout}s")
        logger.info("")

        start_time = time.time()
        check_interval = 2  # Check every 2 seconds
        last_log_time = start_time
        log_progress_interval = 10  # Log progress every 10 seconds

        while time.time() - start_time < timeout:
            elapsed = time.time() - start_time

            # Check if CSV files exist and have data (skip marker file check)
            enabled_telemetry = self.config['telemetry']['enabled']
            expected_files = [output_path / f"{t}.csv" for t in enabled_telemetry]

            all_files_ready = True
            missing_or_empty = []

            for csv_file in expected_files:
                if not csv_file.exists():
                    all_files_ready = False
                    missing_or_empty.append(f"{csv_file.name} (missing)")
                elif csv_file.stat().st_size == 0:
                    all_files_ready = False
                    missing_or_empty.append(f"{csv_file.name} (empty)")

            if all_files_ready:
                logger.info(f"✓ All CSV files ready:")
                for csv_file in expected_files:
                    size = csv_file.stat().st_size
                    logger.info(f"  - {csv_file.name}: {size:,} bytes")
                logger.info(f"✓ Replayer Phase 1 complete after {elapsed:.1f}s")
                logger.info("=" * 60)
                return True

            # Log progress periodically
            if time.time() - last_log_time >= log_progress_interval:
                logger.info(f"Still waiting... ({elapsed:.0f}s elapsed)")

                # Check what files exist in output directory
                csv_files = list(output_path.glob("*.csv"))
                if csv_files:
                    logger.info(f"  CSV files found: {len(csv_files)}")
                    for csv_file in csv_files:
                        size = csv_file.stat().st_size
                        logger.info(f"    - {csv_file.name}: {size:,} bytes")
                else:
                    logger.info(f"  No CSV files found yet")

                last_log_time = time.time()

            # Check if container is still running
            if self.container:
                try:
                    self.container.reload()
                    status = self.container.status

                    if status == 'exited':
                        # Container exited - check logs for errors
                        logger.error(f"Container exited unexpectedly (status: {status})")
                        logs = self.container.logs(tail=100).decode('utf-8')
                        logger.error(f"Container logs:\n{logs}")
                        raise RuntimeError("Replayer container exited before Phase 1 completed")

                except Exception as e:
                    if "RuntimeError" in str(type(e)):
                        raise
                    logger.error(f"Error checking container status: {e}")
                    raise

            time.sleep(check_interval)

        # Timeout reached
        elapsed = time.time() - start_time
        logger.error(f"Timeout waiting for Phase 1 completion")
        logger.error(f"Waited {elapsed:.1f}s (timeout was {timeout}s)")

        # Show final state of CSV files
        logger.error("Final state of CSV files:")
        enabled_telemetry = self.config['telemetry']['enabled']
        for telemetry_type in enabled_telemetry:
            csv_file = output_path / f"{telemetry_type}.csv"
            if csv_file.exists():
                logger.error(f"  {csv_file.name}: {csv_file.stat().st_size:,} bytes")
            else:
                logger.error(f"  {csv_file.name}: NOT FOUND")

        raise TimeoutError(
            f"CSV files not ready within {timeout}s (waited {elapsed:.1f}s). "
            f"Expected files: {[f'{t}.csv' for t in enabled_telemetry]}"
        )

    def start_and_wait(self, timeout: int = 300):
        """
        Start replayer and wait for Phase 1 to complete.

        This is a convenience method that combines start_replayer()
        and _wait_for_replayer_ready().

        Args:
            timeout: Maximum seconds to wait for Phase 1 completion

        Raises:
            TimeoutError: If replayer doesn't become ready within timeout
        """
        logger.info("=" * 60)
        logger.info("STARTING REPLAYER")
        logger.info("=" * 60)
        logger.info(f"Dataset: {self.config['dataset']['namespace']}")
        logger.info(f"Image: {self.get_docker_image()}")
        logger.info(f"Output: {self.get_output_path()}")
        logger.info("")

        # Start the Docker container
        logger.info("Starting Docker container...")
        self.start_replayer()
        logger.info(f"✓ Container started: {self.container.id[:12]}")
        logger.info("")

        # Wait for Phase 1 to complete
        self._wait_for_replayer_ready(timeout=timeout)

        logger.info("")
        logger.info("=" * 60)
        logger.info("✓ REPLAYER IS READY FOR QUERIES")
        logger.info("=" * 60)

    def get_time_mapper(self) -> TimeMapper:
        """
        Get TimeMapper instance for timestamp remapping.

        Creates a TimeMapper based on the config (same logic as replayer).

        Returns:
            TimeMapper instance
        """
        # Get fault time from config
        fault_datetime_str = self.config['dataset']['fault_datetime']
        historical_fault_time = datetime.fromisoformat(fault_datetime_str)

        # Get simulation start time using same logic as replayer
        simulation_start_time = parse_time_option(self.config, historical_fault_time)

        # Get offset
        offset_minutes = self.config.get('simulation', {}).get('offset_minutes', 0)

        # Create TimeMapper
        time_mapper = TimeMapper(
            historical_fault_time=historical_fault_time,
            simulation_start_time=simulation_start_time,
            offset_minutes=offset_minutes
        )

        logger.debug(f"TimeMapper created for dataset: {self.config['dataset']['namespace']}")

        return time_mapper
