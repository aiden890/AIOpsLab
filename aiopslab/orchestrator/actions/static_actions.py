# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Static Actions for querying replayed telemetry data.

Provides a dataset-agnostic interface for agents to query traces, logs, and metrics
from static replayer CSV files through observer APIs.
"""

import logging
from datetime import datetime
from typing import Dict, Optional, Any, List
import pandas as pd
from aiopslab.utils.actions import action

logger = logging.getLogger(__name__)


class StaticActions:
    """
    Actions interface for querying static telemetry data.

    Provides dataset-agnostic methods for agents to query traces, logs, and metrics
    from replayed data. Delegates to dataset-specific observer APIs.
    """

    def __init__(self, observer_apis: Dict[str, Any]):
        """
        Initialize static actions with observer APIs.

        Args:
            observer_apis: Dictionary mapping telemetry type to observer API instance
                          Example: {'trace': OpenRCATraceAPI, 'log': OpenRCALogAPI, ...}
        """
        self.observer_apis = observer_apis
        logger.info(f"StaticActions initialized with {len(observer_apis)} observer APIs")

    @action
    def query_static_traces(
        self,
        start_time: datetime,
        end_time: datetime,
        cmdb_id: Optional[str] = None,
        trace_id: Optional[str] = None
    ) -> pd.DataFrame:
        """
        query_static_traces(start_time, end_time, cmdb_id=None, trace_id=None)

        Query trace spans from replayed data.

        Args:
            start_time: Start of time window (UTC)
            end_time: End of time window (UTC)
            cmdb_id: Optional service filter
            trace_id: Optional trace ID filter

        Returns:
            DataFrame with trace spans

        Raises:
            ValueError: If trace API is not available
        """
        if 'trace' not in self.observer_apis:
            available = ', '.join(self.observer_apis.keys())
            raise ValueError(
                f"Trace API not available for this dataset. "
                f"Available telemetry types: {available}"
            )

        trace_api = self.observer_apis['trace']

        logger.info(f"Querying traces: {start_time} to {end_time}")
        if cmdb_id:
            logger.debug(f"  Filtering by cmdb_id: {cmdb_id}")
        if trace_id:
            logger.debug(f"  Filtering by trace_id: {trace_id}")

        try:
            df = trace_api.query_traces(start_time, end_time, cmdb_id=cmdb_id, trace_id=trace_id)
            logger.info(f"Retrieved {len(df)} trace spans")
            return df
        except Exception as e:
            logger.error(f"Error querying traces: {e}", exc_info=True)
            raise

    @action
    def query_static_logs(
        self,
        start_time: datetime,
        end_time: datetime,
        cmdb_id: Optional[str] = None,
        keyword: Optional[str] = None,
        log_name: Optional[str] = None
    ) -> pd.DataFrame:
        """
        query_static_logs(start_time, end_time, cmdb_id=None, keyword=None, log_name=None)

        Query log entries from replayed data.

        Args:
            start_time: Start of time window (UTC)
            end_time: End of time window (UTC)
            cmdb_id: Optional service filter
            keyword: Optional keyword search (case-insensitive)
            log_name: Optional log type filter (e.g., 'gc', 'app')

        Returns:
            DataFrame with log entries

        Raises:
            ValueError: If log API is not available
        """
        if 'log' not in self.observer_apis:
            available = ', '.join(self.observer_apis.keys())
            raise ValueError(
                f"Log API not available for this dataset. "
                f"Available telemetry types: {available}"
            )

        log_api = self.observer_apis['log']

        logger.info(f"Querying logs: {start_time} to {end_time}")
        if cmdb_id:
            logger.debug(f"  Filtering by cmdb_id: {cmdb_id}")
        if keyword:
            logger.debug(f"  Filtering by keyword: {keyword}")
        if log_name:
            logger.debug(f"  Filtering by log_name: {log_name}")

        try:
            df = log_api.query_logs(
                start_time, end_time,
                cmdb_id=cmdb_id,
                keyword=keyword,
                log_name=log_name
            )
            logger.info(f"Retrieved {len(df)} log entries")
            return df
        except Exception as e:
            logger.error(f"Error querying logs: {e}", exc_info=True)
            raise

    @action
    def query_static_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        cmdb_id: Optional[str] = None,
        metric_name: Optional[str] = None
    ) -> pd.DataFrame:
        """
        query_static_metrics(start_time, end_time, cmdb_id=None)

        Query metric data from replayed data.

        Args:
            start_time: Start of time window (UTC)
            end_time: End of time window (UTC)
            cmdb_id: Optional service filter
            metric_name: Optional metric name filter (e.g., 'cpu_usage', 'memory_usage')

        Returns:
            DataFrame with metric records

        Raises:
            ValueError: If metric API is not available
        """
        if 'metric' not in self.observer_apis:
            available = ', '.join(self.observer_apis.keys())
            raise ValueError(
                f"Metric API not available for this dataset. "
                f"Available telemetry types: {available}"
            )

        metric_api = self.observer_apis['metric']

        logger.info(f"Querying metrics: {start_time} to {end_time}")
        if cmdb_id:
            logger.debug(f"  Filtering by cmdb_id: {cmdb_id}")
        if metric_name:
            logger.debug(f"  Filtering by metric_name: {metric_name}")

        try:
            # Query metrics (API doesn't support metric_name filter)
            df = metric_api.query_metrics(
                start_time, end_time,
                cmdb_id=cmdb_id
            )

            # Apply metric_name filter client-side if specified
            if metric_name and not df.empty and 'name' in df.columns:
                df = df[df['name'] == metric_name]

            logger.info(f"Retrieved {len(df)} metric records")
            return df
        except Exception as e:
            logger.error(f"Error querying metrics: {e}", exc_info=True)
            raise

    @action
    def get_available_services(self) -> List[str]:
        """
        get_available_services()

        Get list of available services (cmdb_ids) in the dataset.

        Returns:
            List of unique service identifiers

        Raises:
            ValueError: If no telemetry APIs are available
        """
        if not self.observer_apis:
            raise ValueError("No telemetry APIs available")

        logger.info("Retrieving available services")

        # Try to get services from trace API first (usually has cmdb_id)
        if 'trace' in self.observer_apis:
            try:
                trace_api = self.observer_apis['trace']
                # Query a wide time range to get all services
                # This assumes the CSV has a 'cmdb_id' column
                df = trace_api.query_traces(
                    datetime(2020, 1, 1),
                    datetime(2030, 1, 1)
                )
                if 'cmdb_id' in df.columns:
                    services = df['cmdb_id'].unique().tolist()
                    logger.info(f"Found {len(services)} services from trace data")
                    return services
            except Exception as e:
                logger.warning(f"Could not get services from trace API: {e}")

        # Try metric API
        if 'metric' in self.observer_apis:
            try:
                metric_api = self.observer_apis['metric']
                df = metric_api.query_metrics(
                    datetime(2020, 1, 1),
                    datetime(2030, 1, 1)
                )
                if 'cmdb_id' in df.columns:
                    services = df['cmdb_id'].unique().tolist()
                    logger.info(f"Found {len(services)} services from metric data")
                    return services
            except Exception as e:
                logger.warning(f"Could not get services from metric API: {e}")

        # Try log API
        if 'log' in self.observer_apis:
            try:
                log_api = self.observer_apis['log']
                df = log_api.query_logs(
                    datetime(2020, 1, 1),
                    datetime(2030, 1, 1)
                )
                if 'cmdb_id' in df.columns:
                    services = df['cmdb_id'].unique().tolist()
                    logger.info(f"Found {len(services)} services from log data")
                    return services
            except Exception as e:
                logger.warning(f"Could not get services from log API: {e}")

        logger.warning("No services found in any telemetry data")
        return []
