# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""OpenRCA Problem Registry - Maps problem IDs to Problem instances."""

import re
import pandas as pd
from pathlib import Path

from aiopslab.paths import OPENRCA_DATASET_DIR
from aiopslab.orchestrator.problems.openrca.bank_rca import BankRCAProblem


class OpenRCAProblemRegistry:
    """Registry for OpenRCA problems.

    Maps problem IDs to Problem class instances.
    Problem ID format: "openrca-{domain}-{task_index}-{query_id}"
    Example: "openrca-bank-task_3-5"
    """

    SUPPORTED_DOMAINS = ["bank", "market", "telecom"]

    def __init__(self):
        """Initialize the registry."""
        self._cache = {}

    def get_problem_instance(self, problem_id: str):
        """Get a problem instance by ID.

        Args:
            problem_id: Format "openrca-{domain}-{task_index}-{query_id}"
                       Example: "openrca-bank-task_3-5"

        Returns:
            Problem instance (e.g., BankRCAProblem)

        Raises:
            ValueError: If problem_id format is invalid or domain not supported
        """
        # Parse problem_id
        match = re.match(r"openrca-(\w+)-(\w+)-(\d+)", problem_id)
        if not match:
            raise ValueError(
                f"Invalid problem_id format: {problem_id}. "
                f"Expected: openrca-{{domain}}-{{task_index}}-{{query_id}}"
            )

        domain = match.group(1).lower()
        task_index = match.group(2)
        query_id = int(match.group(3))

        if domain not in self.SUPPORTED_DOMAINS:
            raise ValueError(
                f"Unsupported domain: {domain}. "
                f"Supported: {self.SUPPORTED_DOMAINS}"
            )

        # Get the appropriate problem class
        if domain == "bank":
            return self._create_bank_problem(task_index, query_id)
        elif domain == "market":
            return self._create_market_problem(task_index, query_id)
        elif domain == "telecom":
            return self._create_telecom_problem(task_index, query_id)

    def _create_bank_problem(self, task_index: str, query_id: int) -> BankRCAProblem:
        """Create a BankRCAProblem instance.

        Args:
            task_index: Task type (e.g., "task_1", "task_3")
            query_id: Row index in query.csv

        Returns:
            BankRCAProblem instance
        """
        # Load query.csv
        query_path = OPENRCA_DATASET_DIR / "Bank" / "query.csv"
        queries_df = pd.read_csv(query_path)

        # Filter by task_index if specified
        if task_index:
            task_queries = queries_df[queries_df["task_index"] == task_index]
            if query_id >= len(task_queries):
                raise ValueError(
                    f"query_id {query_id} out of range for {task_index}. "
                    f"Available: 0-{len(task_queries)-1}"
                )
            query_row = task_queries.iloc[query_id].to_dict()
        else:
            if query_id >= len(queries_df):
                raise ValueError(
                    f"query_id {query_id} out of range. "
                    f"Available: 0-{len(queries_df)-1}"
                )
            query_row = queries_df.iloc[query_id].to_dict()

        # Extract date from instruction (assuming format mentions the date)
        date = self._extract_date_from_instruction(query_row["instruction"])

        return BankRCAProblem(query_row=query_row, date=date)

    def _create_market_problem(self, task_index: str, query_id: int):
        """Create a MarketRCAProblem instance.

        TODO: Implement when MarketRCAProblem is created.
        """
        raise NotImplementedError("Market domain not yet implemented")

    def _create_telecom_problem(self, task_index: str, query_id: int):
        """Create a TelecomRCAProblem instance.

        TODO: Implement when TelecomRCAProblem is created.
        """
        raise NotImplementedError("Telecom domain not yet implemented")

    def _extract_date_from_instruction(self, instruction: str) -> str:
        """Extract date from instruction text.

        Args:
            instruction: The task instruction text

        Returns:
            Date string in format "YYYY_MM_DD"
        """
        # Try to find date patterns like "March 4, 2021" or "2021-03-04"
        patterns = [
            # "March 4, 2021" format
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
            # "2021-03-04" format
            r"(\d{4})-(\d{2})-(\d{2})",
        ]

        for pattern in patterns:
            match = re.search(pattern, instruction)
            if match:
                if "January" in pattern:
                    # Month name format
                    month_map = {
                        "January": "01", "February": "02", "March": "03",
                        "April": "04", "May": "05", "June": "06",
                        "July": "07", "August": "08", "September": "09",
                        "October": "10", "November": "11", "December": "12"
                    }
                    month = month_map[match.group(1)]
                    day = match.group(2).zfill(2)
                    year = match.group(3)
                    return f"{year}_{month}_{day}"
                else:
                    # ISO format
                    year, month, day = match.groups()
                    return f"{year}_{month}_{day}"

        # Default to first available date if not found
        telemetry_path = OPENRCA_DATASET_DIR / "Bank" / "telemetry"
        dates = sorted([d.name for d in telemetry_path.iterdir() if d.is_dir() and not d.name.startswith('.')])
        if dates:
            return dates[0]

        raise ValueError(f"Could not extract date from instruction: {instruction}")

    def get_problem_ids(self, domain: str = None, task_index: str = None) -> list:
        """Get list of available problem IDs.

        Args:
            domain: Filter by domain (optional)
            task_index: Filter by task type (optional)

        Returns:
            List of problem ID strings
        """
        problem_ids = []

        domains = [domain] if domain else self.SUPPORTED_DOMAINS

        for d in domains:
            if d == "bank":
                query_path = OPENRCA_DATASET_DIR / "Bank" / "query.csv"
                if query_path.exists():
                    queries_df = pd.read_csv(query_path)

                    if task_index:
                        queries_df = queries_df[queries_df["task_index"] == task_index]

                    for idx, row in queries_df.iterrows():
                        pid = f"openrca-{d}-{row['task_index']}-{idx}"
                        problem_ids.append(pid)

        return problem_ids
