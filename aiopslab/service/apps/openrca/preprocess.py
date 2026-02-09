"""Data preprocessing for OpenRCA static log replay.

Supports all OpenRCA datasets: Market (cloudbed-1/2), Telecom, Bank.

Handles:
- Loading fault records (different column orders across datasets)
- Filtering CSVs to 90-minute window around fault
- Metric name mapping to Prometheus conventions
- Timestamp remapping (current-time anchoring)
- Per-service log file generation
- Trace data normalization across different trace formats
"""

import os
import re
import time
import pandas as pd
from pathlib import Path


def load_records(dataset_path: str) -> pd.DataFrame:
    """Load fault records from record.csv.

    All datasets have the same columns (level, component, timestamp, datetime, reason)
    but in different orders. pandas reads by column name so order doesn't matter.
    """
    record_path = os.path.join(dataset_path, "record.csv")
    df = pd.read_csv(record_path)
    # Normalize timestamp to int (Bank has float timestamps like 1614841020.0)
    df["timestamp"] = df["timestamp"].astype(float).astype(int)
    return df


def get_fault_info(record: pd.Series, dataset_type: str = "market") -> dict:
    """Extract fault info from a record row.

    Returns dict with original dataset fields:
        timestamp, level, component, reason, datetime, faulty_service

    The faulty_service is derived from component for localization tasks.
    """
    component = str(record["component"])
    level = record["level"]

    # Extract service name from component for localization
    if dataset_type == "market":
        if level == "node":
            faulty_service = component  # node-1 stays as-is
        else:
            # Remove trailing instance number: "shippingservice-1" -> "shippingservice"
            # Handle "adservice2-0" -> "adservice2" (cloudbed-2 naming)
            faulty_service = re.sub(r"-\d+$", "", component)
    else:
        # Telecom and Bank: component IS the service (docker_003, Tomcat01, etc.)
        faulty_service = component

    return {
        "timestamp": int(record["timestamp"]),
        "level": level,
        "component": component,
        "reason": record["reason"],
        "datetime": record["datetime"],
        "faulty_service": faulty_service,
    }


def compute_time_offset(fault_timestamp: int) -> int:
    """Compute offset to remap dataset timestamps to current time.

    Returns offset such that: remapped_time = original_time + offset
    The fault timestamp will map to approximately 'now'.
    """
    return int(time.time()) - fault_timestamp


def find_telemetry_day(dataset_path: str, fault_timestamp: int) -> str:
    """Find the telemetry directory that contains data for the fault timestamp.

    Converts fault datetime to directory format (e.g., "2022_03_20") and checks
    if that directory exists. Falls back to scanning available directories.
    """
    from datetime import datetime

    # Convert timestamp to date string in directory format
    fault_dt = datetime.fromtimestamp(fault_timestamp)
    day_str = fault_dt.strftime("%Y_%m_%d")

    telemetry_base = os.path.join(dataset_path, "telemetry")
    if os.path.isdir(os.path.join(telemetry_base, day_str)):
        return day_str

    # Fallback: check available directories
    if os.path.isdir(telemetry_base):
        available = sorted(os.listdir(telemetry_base))
        if available:
            # Find closest date
            for d in available:
                try:
                    dir_dt = datetime.strptime(d, "%Y_%m_%d")
                    if dir_dt.date() == fault_dt.date():
                        return d
                except ValueError:
                    continue
            # If no exact match, return first available
            return available[0]

    return day_str


def get_telemetry_dir(dataset_path: str, day: str) -> str:
    """Get the telemetry directory for a given day."""
    return os.path.join(dataset_path, "telemetry", day)


def filter_time_window(
    df: pd.DataFrame,
    fault_timestamp: int,
    before_minutes: int = 20,
    after_minutes: int = 10,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Filter dataframe to a time window around the fault.

    Automatically handles both second and millisecond timestamps.
    """
    start = fault_timestamp - (before_minutes * 60)
    end = fault_timestamp + (after_minutes * 60)

    if df.empty:
        return df

    ts = df[timestamp_col]
    # Handle millisecond timestamps (some datasets use ms)
    if ts.iloc[0] > 1e12:
        start_ms = start * 1000
        end_ms = end * 1000
        return df[(ts >= start_ms) & (ts <= end_ms)]

    return df[(ts >= start) & (ts <= end)]


# ---------------------------------------------------------------------------
# Metric name mapping (Market dataset uses Prometheus-like names)
# ---------------------------------------------------------------------------

def map_metric_name(kpi_name: str, dataset_type: str = "market") -> tuple:
    """Map dataset metric name to Prometheus metric name.

    Returns (prometheus_name, extra_labels, needs_byte_conversion).

    For Market: applies counter suffix, MB->bytes, interface extraction rules.
    For Telecom/Bank: uses the metric name as-is (already descriptive names).
    """
    extra_labels = {}
    name = kpi_name
    needs_byte_conversion = False

    if dataset_type == "market":
        # Rule: Extract interface suffix (e.g., ".eth0")
        if "." in name:
            parts = name.rsplit(".", 1)
            if parts[1] in ("eth0", "eth1", "lo", "net1"):
                name = parts[0]
                extra_labels["interface"] = parts[1]

        # Rule: Convert MB metrics
        if "_MB" in name:
            name = name.replace("_MB", "_bytes")
            needs_byte_conversion = True

        # Rule: Add _total for counters
        counter_patterns = [
            "container_cpu_usage_seconds",
            "container_cpu_cfs_periods",
            "container_cpu_cfs_throttled_periods",
            "container_cpu_cfs_throttled_seconds",
            "container_network_receive_bytes",
            "container_network_transmit_bytes",
            "container_network_receive_packets",
            "container_network_transmit_packets",
            "container_network_receive_packets_dropped",
            "container_network_transmit_packets_dropped",
            "container_network_receive_errors",
            "container_network_transmit_errors",
        ]
        for pattern in counter_patterns:
            if name == pattern or name.startswith(pattern):
                if not name.endswith("_total"):
                    name = name + "_total"
                break

    return name, extra_labels, needs_byte_conversion


def parse_cmdb_id(cmdb_id: str, namespace: str, dataset_type: str = "market") -> dict:
    """Parse cmdb_id into Prometheus labels.

    Market: "node-6.adservice2-0" -> {pod: "adservice2-0", node: "node-6"}
    Telecom: "docker_003" -> {pod: "docker_003"}
    Bank: "Tomcat01" -> {pod: "Tomcat01"}
    """
    labels = {"namespace": namespace}

    if dataset_type == "market":
        if "." in cmdb_id:
            parts = cmdb_id.split(".", 1)
            labels["node"] = parts[0]
            labels["pod"] = parts[1]
        elif cmdb_id.startswith("node-"):
            labels["node"] = cmdb_id
        else:
            labels["pod"] = cmdb_id
    else:
        # Telecom and Bank: cmdb_id is the component name directly
        labels["pod"] = cmdb_id

    return labels


# ---------------------------------------------------------------------------
# Log preparation
# ---------------------------------------------------------------------------

def prepare_logs_for_service(
    dataset_path: str,
    service_name: str,
    fault_timestamp: int,
    time_offset: int,
    day: str = None,
    dataset_type: str = "market",
) -> pd.DataFrame:
    """Prepare log data for a specific service.

    Market: combines log_service.csv and log_proxy.csv
    Bank: uses log_service.csv only
    Telecom: no logs available, returns empty DataFrame
    """
    if dataset_type == "telecom":
        return pd.DataFrame(columns=["timestamp", "cmdb_id", "value", "remapped_timestamp"])

    if day is None:
        day = find_telemetry_day(dataset_path, fault_timestamp)

    telemetry_dir = get_telemetry_dir(dataset_path, day)
    log_dir = os.path.join(telemetry_dir, "log")

    if not os.path.isdir(log_dir):
        return pd.DataFrame(columns=["timestamp", "cmdb_id", "value", "remapped_timestamp"])

    frames = []

    # Load application logs
    log_service_path = os.path.join(log_dir, "log_service.csv")
    if os.path.exists(log_service_path):
        df_svc = pd.read_csv(log_service_path)
        # Filter to this service's pods
        if dataset_type == "market":
            df_svc = df_svc[df_svc["cmdb_id"].str.startswith(service_name)]
        else:
            # Bank: exact match on cmdb_id
            df_svc = df_svc[df_svc["cmdb_id"] == service_name]
        if not df_svc.empty:
            df_svc = filter_time_window(df_svc, fault_timestamp)
            frames.append(df_svc[["timestamp", "cmdb_id", "value"]])

    # Load proxy logs (Market only)
    if dataset_type == "market":
        log_proxy_path = os.path.join(log_dir, "log_proxy.csv")
        if os.path.exists(log_proxy_path):
            df_proxy = pd.read_csv(log_proxy_path)
            df_proxy = df_proxy[df_proxy["cmdb_id"].str.startswith(service_name)]
            if not df_proxy.empty:
                df_proxy = filter_time_window(df_proxy, fault_timestamp)
                frames.append(df_proxy[["timestamp", "cmdb_id", "value"]])

    if not frames:
        return pd.DataFrame(columns=["timestamp", "cmdb_id", "value", "remapped_timestamp"])

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("timestamp")
    combined["remapped_timestamp"] = combined["timestamp"] + time_offset

    return combined


# ---------------------------------------------------------------------------
# Metric preparation
# ---------------------------------------------------------------------------

def _process_standard_metrics(
    metric_dir: str,
    metric_files: list,
    fault_timestamp: int,
    time_offset: int,
    namespace: str,
    dataset_type: str,
) -> list:
    """Process standard-format metric CSVs (timestamp, cmdb_id, kpi_name, value).

    Works for: metric_container, metric_node, metric_runtime, metric_mesh,
               metric_middleware (Telecom).
    """
    timeseries = []

    for metric_file in metric_files:
        filepath = os.path.join(metric_dir, metric_file)
        if not os.path.exists(filepath):
            continue

        df = pd.read_csv(filepath)

        # Telecom metrics have different column names
        if "name" in df.columns and "kpi_name" not in df.columns:
            df = df.rename(columns={"name": "kpi_name"})

        if "kpi_name" not in df.columns or "cmdb_id" not in df.columns:
            continue

        # Determine timestamp column
        ts_col = "timestamp"
        if ts_col not in df.columns and "startTime" in df.columns:
            ts_col = "startTime"

        df = filter_time_window(df, fault_timestamp, timestamp_col=ts_col)
        if df.empty:
            continue

        for (cmdb_id, kpi_name), group in df.groupby(["cmdb_id", "kpi_name"]):
            prom_name, extra_labels, needs_byte_conversion = map_metric_name(
                kpi_name, dataset_type
            )
            labels = parse_cmdb_id(cmdb_id, namespace, dataset_type)
            labels.update(extra_labels)
            labels["__name__"] = prom_name

            samples = []
            for _, row in group.iterrows():
                ts_val = row[ts_col]
                # Normalize to milliseconds
                if ts_val > 1e12:
                    ts_ms = int(ts_val + time_offset * 1000)
                else:
                    ts_ms = int((ts_val + time_offset) * 1000)
                value = float(row["value"])
                if needs_byte_conversion:
                    value *= 1048576
                samples.append((ts_ms, value))

            timeseries.append({"labels": labels, "samples": samples})

    return timeseries


def _process_service_metrics_market(
    metric_dir: str,
    fault_timestamp: int,
    time_offset: int,
    namespace: str,
) -> list:
    """Process Market service metrics (service, timestamp, rr, sr, mrt, count)."""
    timeseries = []
    service_path = os.path.join(metric_dir, "metric_service.csv")
    if not os.path.exists(service_path):
        return timeseries

    df = pd.read_csv(service_path)
    df = filter_time_window(df, fault_timestamp)
    if df.empty:
        return timeseries

    for service_name, group in df.groupby("service"):
        base_labels = {"namespace": namespace, "service": service_name}
        for col, metric_name in [
            ("rr", "service_request_rate"),
            ("sr", "service_success_rate"),
            ("mrt", "service_mean_response_time_ms"),
        ]:
            if col not in group.columns:
                continue
            samples = [
                (int((r["timestamp"] + time_offset) * 1000), float(r[col]))
                for _, r in group.iterrows()
            ]
            timeseries.append({
                "labels": {**base_labels, "__name__": metric_name},
                "samples": samples,
            })

    return timeseries


def _process_app_metrics_telecom(
    metric_dir: str,
    fault_timestamp: int,
    time_offset: int,
    namespace: str,
) -> list:
    """Process Telecom app metrics (serviceName, startTime, avg_time, num, succee_num, succee_rate)."""
    timeseries = []
    app_path = os.path.join(metric_dir, "metric_app.csv")
    if not os.path.exists(app_path):
        return timeseries

    df = pd.read_csv(app_path)

    # Telecom metric_app uses startTime (ms) and has column 'tc' or 'serviceName'
    ts_col = "startTime" if "startTime" in df.columns else "timestamp"
    df = filter_time_window(df, fault_timestamp, timestamp_col=ts_col)
    if df.empty:
        return timeseries

    svc_col = "tc" if "tc" in df.columns else "serviceName"
    for service_name, group in df.groupby(svc_col):
        base_labels = {"namespace": namespace, "service": service_name}
        for col, metric_name in [
            ("avg_time", "service_avg_response_time_ms"),
            ("num", "service_request_count"),
            ("succee_rate", "service_success_rate"),
        ]:
            if col not in group.columns:
                continue
            samples = []
            for _, r in group.iterrows():
                ts_val = r[ts_col]
                if ts_val > 1e12:
                    ts_ms = int(ts_val + time_offset * 1000)
                else:
                    ts_ms = int((ts_val + time_offset) * 1000)
                samples.append((ts_ms, float(r[col])))
            timeseries.append({
                "labels": {**base_labels, "__name__": metric_name},
                "samples": samples,
            })

    return timeseries


def _process_app_metrics_bank(
    metric_dir: str,
    fault_timestamp: int,
    time_offset: int,
    namespace: str,
) -> list:
    """Process Bank app metrics (timestamp, rr, sr, cnt, mrt, tc)."""
    timeseries = []
    app_path = os.path.join(metric_dir, "metric_app.csv")
    if not os.path.exists(app_path):
        return timeseries

    df = pd.read_csv(app_path)
    df = filter_time_window(df, fault_timestamp)
    if df.empty:
        return timeseries

    svc_col = "tc" if "tc" in df.columns else "serviceName"
    for service_name, group in df.groupby(svc_col):
        base_labels = {"namespace": namespace, "service": service_name}
        for col, metric_name in [
            ("rr", "service_request_rate"),
            ("sr", "service_success_rate"),
            ("mrt", "service_mean_response_time_ms"),
        ]:
            if col not in group.columns:
                continue
            samples = [
                (int((r["timestamp"] + time_offset) * 1000), float(r[col]))
                for _, r in group.iterrows()
            ]
            timeseries.append({
                "labels": {**base_labels, "__name__": metric_name},
                "samples": samples,
            })

    return timeseries


def prepare_metrics(
    dataset_path: str,
    fault_timestamp: int,
    time_offset: int,
    namespace: str,
    day: str = None,
    dataset_type: str = "market",
    metric_files: list = None,
) -> list:
    """Prepare all metrics for Prometheus remote_write backfill.

    Returns list of dicts with: labels, samples
    where samples is list of (timestamp_ms, value).
    """
    if day is None:
        day = find_telemetry_day(dataset_path, fault_timestamp)

    telemetry_dir = get_telemetry_dir(dataset_path, day)
    metric_dir = os.path.join(telemetry_dir, "metric")

    if not os.path.isdir(metric_dir):
        return []

    # Standard format metric files (timestamp, cmdb_id, kpi_name, value)
    if metric_files is None:
        if dataset_type == "market":
            standard_files = [
                "metric_container.csv", "metric_node.csv",
                "metric_runtime.csv", "metric_mesh.csv",
            ]
        elif dataset_type == "telecom":
            standard_files = [
                "metric_container.csv", "metric_node.csv",
                "metric_service.csv", "metric_middleware.csv",
            ]
        elif dataset_type == "bank":
            standard_files = ["metric_container.csv"]
        else:
            standard_files = ["metric_container.csv"]
    else:
        # Use provided list, but separate out special-format files
        standard_files = [f for f in metric_files if f not in ("metric_app.csv", "metric_service.csv")]

    all_timeseries = _process_standard_metrics(
        metric_dir, standard_files, fault_timestamp, time_offset, namespace, dataset_type
    )

    # Process service/app metrics (special format per dataset)
    if dataset_type == "market":
        all_timeseries.extend(
            _process_service_metrics_market(metric_dir, fault_timestamp, time_offset, namespace)
        )
    elif dataset_type == "telecom":
        all_timeseries.extend(
            _process_app_metrics_telecom(metric_dir, fault_timestamp, time_offset, namespace)
        )
    elif dataset_type == "bank":
        all_timeseries.extend(
            _process_app_metrics_bank(metric_dir, fault_timestamp, time_offset, namespace)
        )

    return all_timeseries


# ---------------------------------------------------------------------------
# Trace preparation
# ---------------------------------------------------------------------------

def prepare_traces(
    dataset_path: str,
    fault_timestamp: int,
    time_offset: int,
    namespace: str,
    day: str = None,
    dataset_type: str = "market",
) -> pd.DataFrame:
    """Prepare trace data for Jaeger backfill.

    Normalizes trace formats across datasets to a common schema:
        trace_id, span_id, parent_span, service_name, operation_name,
        start_time (ms), duration (ms), status_code, remapped_timestamp (ms)

    Market trace columns: timestamp, cmdb_id, span_id, trace_id, duration, type, status_code, operation_name, parent_span
    Telecom trace columns: callType, startTime, elapsedTime, success, traceId, id, pid, cmdb_id, dsName, serviceName
    Bank trace columns: timestamp, cmdb_id, parent_id, span_id, trace_id, duration
    """
    if day is None:
        day = find_telemetry_day(dataset_path, fault_timestamp)

    telemetry_dir = get_telemetry_dir(dataset_path, day)
    trace_dir = os.path.join(telemetry_dir, "trace")

    if not os.path.isdir(trace_dir):
        return pd.DataFrame()

    # Find the trace file
    trace_file = None
    for fname in os.listdir(trace_dir):
        if fname.endswith(".csv"):
            trace_file = os.path.join(trace_dir, fname)
            break

    if trace_file is None:
        return pd.DataFrame()

    df = pd.read_csv(trace_file)
    if df.empty:
        return df

    time_offset_ms = time_offset * 1000

    if dataset_type == "market":
        # timestamp is in ms, has span_id, trace_id, duration, operation_name, parent_span
        df = filter_time_window(df, fault_timestamp, timestamp_col="timestamp")
        if df.empty:
            return df
        df["remapped_timestamp"] = df["timestamp"] + time_offset_ms
        df["service_name"] = df["cmdb_id"].apply(lambda x: re.sub(r"\d*-\d+$", "", str(x)))
        # Rename to common schema
        df = df.rename(columns={"parent_span": "parent_span"})

    elif dataset_type == "telecom":
        # startTime is in ms, id=span_id, pid=parent_span, traceId=trace_id
        df = filter_time_window(df, fault_timestamp, timestamp_col="startTime")
        if df.empty:
            return df
        df["remapped_timestamp"] = df["startTime"] + time_offset_ms
        df = df.rename(columns={
            "startTime": "timestamp",
            "traceId": "trace_id",
            "id": "span_id",
            "pid": "parent_span",
            "elapsedTime": "duration",
            "callType": "operation_name",
        })
        df["service_name"] = df["cmdb_id"]
        df["status_code"] = df["success"].apply(lambda x: 0 if x else 1)

    elif dataset_type == "bank":
        # timestamp is in ms, has span_id, trace_id, parent_id, duration
        df = filter_time_window(df, fault_timestamp, timestamp_col="timestamp")
        if df.empty:
            return df
        df["remapped_timestamp"] = df["timestamp"] + time_offset_ms
        df = df.rename(columns={"parent_id": "parent_span"})
        df["service_name"] = df["cmdb_id"]
        df["operation_name"] = "unknown"
        df["status_code"] = 0

    return df


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def get_available_scenarios(dataset_path: str, dataset_type: str = "market") -> list:
    """Get list of scenario IDs that have matching telemetry data.

    Returns list of valid scenario indices (0-based).
    """
    records = load_records(dataset_path)
    valid = []

    for idx, record in records.iterrows():
        fault_ts = int(record["timestamp"])
        day = find_telemetry_day(dataset_path, fault_ts)
        telemetry_dir = get_telemetry_dir(dataset_path, day)
        if os.path.isdir(telemetry_dir):
            valid.append(idx)

    return valid
