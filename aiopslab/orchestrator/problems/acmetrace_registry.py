# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""AcmeTrace Problem Registry - Maps problem IDs to Problem instances."""

import re
import pandas as pd
from pathlib import Path

from aiopslab.paths import ACME_KALOS_DIR
from aiopslab.orchestrator.problems.acmetrace.kalos_rca import (
    KalosDetectionProblem,
    KalosLocalizationProblem,
    KalosAnalysisProblem,
)


class AcmeTraceProblemRegistry:
    """Registry for AcmeTrace problems.

    Maps problem IDs to Problem class instances.
    Problem ID format: "acmetrace-kalos-{task_type}-{query_id}"
    Example: "acmetrace-kalos-detection-5"
    """

    SUPPORTED_CLUSTERS = ["kalos"]
    SUPPORTED_TASK_TYPES = ["detection", "localization", "analysis"]

    def __init__(self, sample_dir: str = "samples/kalos_rca"):
        """Initialize the registry.

        Args:
            sample_dir: Path to sampled dataset relative to ACME_KALOS_DIR
        """
        self.sample_dir = sample_dir
        self._cache = {}

    def get_problem_instance(self, problem_id: str):
        """Get a problem instance by ID.

        Args:
            problem_id: Format "acmetrace-{cluster}-{task_type}-{query_id}"
                       Example: "acmetrace-kalos-detection-5"

        Returns:
            Problem instance (e.g., KalosDetectionProblem)

        Raises:
            ValueError: If problem_id format is invalid or cluster not supported
        """
        # Parse problem_id
        match = re.match(r"acmetrace-(\w+)-(\w+)-(\d+)", problem_id)
        if not match:
            raise ValueError(
                f"Invalid problem_id format: {problem_id}. "
                f"Expected: acmetrace-{{cluster}}-{{task_type}}-{{query_id}}"
            )

        cluster = match.group(1).lower()
        task_type = match.group(2).lower()
        query_id = int(match.group(3))

        if cluster not in self.SUPPORTED_CLUSTERS:
            raise ValueError(
                f"Unsupported cluster: {cluster}. "
                f"Supported: {self.SUPPORTED_CLUSTERS}"
            )

        if task_type not in self.SUPPORTED_TASK_TYPES:
            raise ValueError(
                f"Unsupported task_type: {task_type}. "
                f"Supported: {self.SUPPORTED_TASK_TYPES}"
            )

        # Get the appropriate problem class
        if cluster == "kalos":
            return self._create_kalos_problem(task_type, query_id)

    def _create_kalos_problem(self, task_type: str, query_id: int):
        """Create a Kalos RCA problem instance.

        Args:
            task_type: Task type ("detection", "localization", "analysis")
            query_id: Row index in the corresponding query CSV

        Returns:
            Problem instance (KalosDetectionProblem, etc.)
        """
        # Load the appropriate query CSV
        queries_path = ACME_KALOS_DIR / self.sample_dir / "queries" / f"{task_type}.csv"

        if not queries_path.exists():
            raise ValueError(
                f"Query file not found: {queries_path}. "
                f"Please run sample_kalos_rca.py first to generate the sample dataset."
            )

        queries_df = pd.read_csv(queries_path)

        if query_id >= len(queries_df):
            raise ValueError(
                f"query_id {query_id} out of range for {task_type}. "
                f"Available: 0-{len(queries_df)-1}"
            )

        query_row = queries_df.iloc[query_id].to_dict()

        # Create the appropriate problem instance
        if task_type == "detection":
            return KalosDetectionProblem(query_row=query_row, sample_dir=self.sample_dir)
        elif task_type == "localization":
            return KalosLocalizationProblem(query_row=query_row, sample_dir=self.sample_dir)
        elif task_type == "analysis":
            return KalosAnalysisProblem(query_row=query_row, sample_dir=self.sample_dir)

    def get_problem_ids(self, cluster: str = None, task_type: str = None) -> list:
        """Get list of available problem IDs.

        Args:
            cluster: Filter by cluster (optional)
            task_type: Filter by task type (optional)

        Returns:
            List of problem ID strings
        """
        problem_ids = []

        clusters = [cluster] if cluster else self.SUPPORTED_CLUSTERS
        task_types = [task_type] if task_type else self.SUPPORTED_TASK_TYPES

        for c in clusters:
            if c == "kalos":
                for tt in task_types:
                    queries_path = ACME_KALOS_DIR / self.sample_dir / "queries" / f"{tt}.csv"
                    if queries_path.exists():
                        queries_df = pd.read_csv(queries_path)
                        for idx in range(len(queries_df)):
                            pid = f"acmetrace-{c}-{tt}-{idx}"
                            problem_ids.append(pid)

        return problem_ids

    def get_problem_count(self, cluster: str = None, task_type: str = None) -> int:
        """Get count of available problems.

        Args:
            cluster: Filter by cluster (optional)
            task_type: Filter by task type (optional)

        Returns:
            Number of problems
        """
        return len(self.get_problem_ids(cluster, task_type))

    def get_stats(self) -> dict:
        """Get registry statistics.

        Returns:
            dict with problem counts by type
        """
        stats = {}
        for tt in self.SUPPORTED_TASK_TYPES:
            stats[tt] = self.get_problem_count(task_type=tt)
        stats["total"] = sum(stats.values())
        return stats
