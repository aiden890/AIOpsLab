# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Base class for static log replayer applications."""

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Optional
import docker
import yaml

logger = logging.getLogger(__name__)


class BaseStaticApp(ABC):
    """Base class for static log replayer applications."""

    def __init__(self, config_file: str):
        """
        Initialize static replayer app.

        Args:
            config_file: Path to configuration YAML file
        """
        # Convert to absolute path (Docker requires absolute paths for volumes)
        self.config_file = str(Path(config_file).resolve())
        self.config = self.load_config()
        self.docker_client = docker.from_env()
        self.container: Optional[Any] = None

    def load_config(self) -> Dict[str, Any]:
        """Load application configuration from YAML file."""
        with open(self.config_file, 'r') as f:
            return yaml.safe_load(f)

    @abstractmethod
    def get_dataset_path(self) -> Path:
        """Return path to dataset directory."""
        pass

    @abstractmethod
    def get_docker_image(self) -> str:
        """Return Docker image name."""
        pass

    def get_output_path(self) -> Path:
        """Get output path for telemetry data."""
        from aiopslab.paths import BASE_DIR

        namespace = self.config['dataset']['namespace']
        output_dir = BASE_DIR / f"data/telemetry_output/{namespace}"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def get_volumes(self) -> Dict[str, Dict[str, str]]:
        """Get volume mappings for Docker."""
        return {
            str(self.get_dataset_path()): {
                'bind': '/datasets',
                'mode': 'ro'
            },
            str(self.get_output_path()): {
                'bind': '/telemetry_output',
                'mode': 'rw'
            },
            str(self.config_file): {
                'bind': '/config/replayer_config.yaml',
                'mode': 'ro'
            }
        }

    def get_environment(self) -> Dict[str, str]:
        """Get environment variables for Docker."""
        env = {}

        # Pass through debug mode
        if os.getenv('DEBUG_MODE'):
            env['DEBUG_MODE'] = os.getenv('DEBUG_MODE')

        return env

    def start_replayer(self):
        """Start the replayer Docker container."""
        logger.info(f"Starting replayer for {self.config['dataset']['namespace']}...")

        container_name = f"replayer-{self.config['dataset']['namespace']}"

        # Aggressively clean up any existing container with same name
        # First try using Docker API
        try:
            existing_container = self.docker_client.containers.get(container_name)
            logger.warning(f"Found existing container '{container_name}', removing it...")
            try:
                existing_container.stop(timeout=5)
                logger.info(f"✓ Stopped existing container")
            except Exception as e:
                logger.debug(f"Stop failed (might be already stopped): {e}")
            try:
                existing_container.remove(force=True)
                logger.info(f"✓ Removed existing container")
            except Exception as e:
                logger.debug(f"Remove failed: {e}")
        except docker.errors.NotFound:
            # No existing container, this is expected
            logger.debug(f"No existing container '{container_name}' found")
        except Exception as e:
            logger.warning(f"Error checking for existing container: {e}")

        # Also try CLI cleanup as backup (handles edge cases)
        import subprocess
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=10
            )
            logger.debug(f"CLI cleanup attempted for {container_name}")
        except:
            pass  # Silent fail - container might not exist

        try:
            self.container = self.docker_client.containers.run(
                image=self.get_docker_image(),
                volumes=self.get_volumes(),
                environment=self.get_environment(),
                detach=True,
                remove=True,
                name=container_name
            )

            logger.info(f"Replayer started: {self.container.id[:12]}")

        except docker.errors.ImageNotFound as e:
            logger.error(f"Docker image not found: {self.get_docker_image()}")
            logger.error("Please build the image first: docker build -t <image> .")
            raise
        except Exception as e:
            logger.error(f"Error starting replayer: {e}", exc_info=True)
            raise

    def stop_replayer(self):
        """Stop the replayer Docker container."""
        if self.container:
            logger.info(f"Stopping container {self.container.id[:12]}...")
            try:
                self.container.stop(timeout=10)
                logger.info("Container stopped successfully")
            except Exception as e:
                logger.error(f"Error stopping container: {e}", exc_info=True)

    def cleanup(self):
        """
        Cleanup after problem completion.
        Stop container and clear volume data, keep image for reuse.
        """
        logger.info("Starting cleanup...")

        # Step 1: Stop container
        self.stop_replayer()

        # Step 2: Clear volume data (keep directory structure)
        output_path = self.get_output_path()
        if output_path.exists():
            logger.info(f"Clearing volume data at {output_path}...")

            # Check debug mode
            if os.getenv('DEBUG_MODE') == 'true':
                logger.warning("DEBUG_MODE: Skipping volume cleanup")
                return

            # Remove CSV files
            csv_files = list(output_path.glob("*.csv"))
            for csv_file in csv_files:
                logger.debug(f"Removing {csv_file.name}")
                csv_file.unlink()

            # Remove marker file
            marker_file = output_path / '.phase1_complete'
            if marker_file.exists():
                logger.debug("Removing .phase1_complete marker")
                marker_file.unlink()

            logger.info(f"Removed {len(csv_files)} CSV files and marker file")

        # Step 3: Image is kept automatically (no action needed)
        logger.info("Cleanup completed. Docker image preserved for reuse.")
