"""Interface to static dataset telemetry.

StaticApp: reads from local filesystem (used in tests).
DockerStaticApp: reads from inside a Docker container via `docker exec`.

Both provide fetch_*_df() methods that return DataFrames (used by actions
to save locally before returning paths to the agent).
"""

import subprocess
from io import StringIO
from datetime import datetime, timezone
from typing import Optional, Tuple

import pandas as pd
from pathlib import Path


def _to_unix(t) -> float | None:
    """Convert t to Unix timestamp (float). Handles int, float, or datetime string."""
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            ts = pd.Timestamp(t)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return ts.timestamp()
        except Exception:
            return None
    return float(t)


def _filter_by_time(df: pd.DataFrame,
                    timestamp_col: str = "timestamp",
                    start_time=None, end_time=None) -> pd.DataFrame:
    """Filter DataFrame to rows within [start_time, end_time] (Unix timestamps or datetime strings).

    Returns all data if neither bound is specified or no rows match.
    """
    if df.empty or timestamp_col not in df.columns:
        return df

    start_ts = _to_unix(start_time)
    end_ts = _to_unix(end_time)

    if start_ts is None and end_ts is None:
        return df

    filtered = df.copy()
    if start_ts is not None:
        filtered = filtered[filtered[timestamp_col] >= start_ts]
    if end_ts is not None:
        filtered = filtered[filtered[timestamp_col] <= end_ts]

    return filtered if not filtered.empty else df


def _filter_by_time_strict(df: pd.DataFrame,
                            timestamp_col: str = "timestamp",
                            start_time=None, end_time=None) -> pd.DataFrame:
    """Filter DataFrame to rows within [start_time, end_time] — strict, no fallback.

    Unlike _filter_by_time, returns an empty DataFrame if no rows match the window.
    Use this when the caller needs to distinguish "no data in window" from "all data".
    """

    if df.empty or timestamp_col not in df.columns:
        print(f"Warning: No timestamp column found in {df.columns}")
        return df

    start_ts = _to_unix(start_time)
    end_ts = _to_unix(end_time)


    if start_ts is None and end_ts is None:
        return df

    filtered = df.copy()
    if start_ts is not None:
        filtered = filtered[filtered[timestamp_col] >= start_ts]

    if end_ts is not None:
        filtered = filtered[filtered[timestamp_col] <= end_ts]


    return filtered


def _filter_logs(df: pd.DataFrame, service=None,
                 start_time=None, end_time=None) -> pd.DataFrame:
    """Apply service and time range filters to log data."""
    if service:
        service_col = None
        for col in ["cmdb_id", "service", "service_name"]:
            if col in df.columns:
                service_col = col
                break
        if service_col:
            df = df[df[service_col].str.contains(service, case=False, na=False)]

    start_ts = _to_unix(start_time)
    end_ts = _to_unix(end_time)
    if start_ts is not None and "timestamp" in df.columns:
        df = df[df["timestamp"] >= start_ts]
    if end_ts is not None and "timestamp" in df.columns:
        df = df[df["timestamp"] <= end_ts]

    return df


def _detect_col(df: pd.DataFrame, candidates: list) -> Optional[str]:
    """Return first matching column name from candidates."""
    return next((c for c in candidates if c in df.columns), None)


def _compute_log_overview(df: pd.DataFrame) -> dict:
    """Compute summary stats for a log DataFrame."""
    overview = {"total_rows": len(df)}

    if "timestamp" in df.columns:
        ts = df["timestamp"]
        overview["time_range"] = {
            "start": datetime.fromtimestamp(ts.min(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "end": datetime.fromtimestamp(ts.max(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

    service_col = _detect_col(df, ["cmdb_id", "service", "service_name"])
    if service_col:
        overview["rows_per_service"] = df[service_col].value_counts().to_dict()

    type_col = _detect_col(df, ["log_name", "level", "log_type", "type"])
    if type_col:
        overview["rows_per_log_type"] = df[type_col].value_counts().to_dict()

    return overview


def _search_in_df(df: pd.DataFrame, keyword: str = None, limit: int = 100,
                  service: str = None) -> pd.DataFrame:
    """Filter log rows by service and/or keyword.

    - service: filters rows where cmdb_id matches (case-insensitive substring).
    - keyword: searches across value/message (log text), cmdb_id, and log_name.
    Either or both may be provided; at least one should be specified.
    """
    # Service filter
    if service:
        svc_col = _detect_col(df, ["cmdb_id", "service", "service_name"])
        if svc_col:
            df = df[df[svc_col].astype(str).str.contains(service, case=False, na=False)]

    if df.empty:
        return df

    # Keyword filter (optional)
    if keyword:
        masks = []
        for col in ["value", "message", "log_message", "content", "body",
                    "cmdb_id", "log_name", "level", "log_type"]:
            if col in df.columns:
                masks.append(df[col].astype(str).str.contains(keyword, case=False, na=False, regex=False))
        if masks:
            combined = masks[0]
            for m in masks[1:]:
                combined = combined | m
            df = df[combined]

    return df.head(limit)


def _compute_metric_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate metrics per service (min/avg/max for numeric columns)."""
    service_col = _detect_col(df, ["cmdb_id", "service", "service_name", "tc"])
    if not service_col:
        return df.describe().T

    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c != "timestamp"
    ]
    if not numeric_cols:
        return pd.DataFrame()

    agg = df.groupby(service_col)[numeric_cols].agg(["mean", "min", "max"])
    agg.columns = [f"{col}_{stat}" for col, stat in agg.columns]
    return agg.round(2).reset_index()


def _compute_anomaly_metrics(
    df: pd.DataFrame,
    sr_threshold: float = 95.0,
    mrt_threshold: float = 500.0,
) -> pd.DataFrame:
    """Return per-service rows with anomaly flags based on success rate / response time."""
    service_col = _detect_col(df, ["cmdb_id", "service", "service_name", "tc"])
    if not service_col:
        return pd.DataFrame()

    rows = []
    for service, grp in df.groupby(service_col):
        row: dict = {"service": service, "data_points": len(grp)}

        if "sr" in df.columns:
            row["min_success_rate"] = round(float(grp["sr"].min()), 2)
            row["avg_success_rate"] = round(float(grp["sr"].mean()), 2)
            row["sr_anomaly"] = bool(grp["sr"].min() < sr_threshold)

        if "mrt" in df.columns:
            row["max_response_time_ms"] = round(float(grp["mrt"].max()), 2)
            row["avg_response_time_ms"] = round(float(grp["mrt"].mean()), 2)
            row["mrt_anomaly"] = bool(grp["mrt"].max() > mrt_threshold)

        if "rr" in df.columns:
            row["min_request_rate"] = round(float(grp["rr"].min()), 2)
            row["avg_request_rate"] = round(float(grp["rr"].mean()), 2)

        anomaly_flags = [v for k, v in row.items() if k.endswith("_anomaly")]
        row["is_anomaly"] = any(anomaly_flags)
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    return result.sort_values("is_anomaly", ascending=False).reset_index(drop=True)


def _compute_kpi_deviation(
    full_df: pd.DataFrame,
    window_df: pd.DataFrame,
    components: list | None = None,
    top_n: int = 20,
    high_pct: float = 0.90,
    low_pct: float = 0.10,
) -> dict:
    """Compute percentile deviation for metric KPIs in a fault window vs full-dataset baseline.
    
    Returns dict with keys 'high' and 'low', each a sorted DataFrame.
    """
    cmdb_col = _detect_col(full_df, ["cmdb_id", "service", "service_name"])
    ts_col   = _detect_col(full_df, ["timestamp", "startTime"])
    name_col = _detect_col(full_df, ["name", "kpi_name", "kpi", "metric_name"])
    if not cmdb_col or not name_col or "value" not in full_df.columns:
        return {"high": pd.DataFrame(), "low": pd.DataFrame()}

    if components:
        full_df = full_df[full_df[cmdb_col].isin(components)]
        if not window_df.empty:
            window_df = window_df[window_df[cmdb_col].isin(components)]

    if full_df.empty or window_df.empty:
        return {"high": pd.DataFrame(), "low": pd.DataFrame()}

    p_high_label = f"p{int(high_pct * 100)}"
    p_low_label  = f"p{int(low_pct * 100)}"

    # Baseline: global percentiles per (component, KPI) from the full dataset
    baseline = (
        full_df.groupby([cmdb_col, name_col])["value"]
        .agg(**{
            p_high_label: lambda x: x.quantile(high_pct),
            p_low_label:  lambda x: x.quantile(low_pct),
        })
        .reset_index()
    )

    # Window stats: max, min, and the exact timestamp of each extreme
    has_ts = ts_col is not None and ts_col in window_df.columns

    def _agg_window(grp):
        row: dict = {
            "max_value": grp["value"].max(),
            "min_value": grp["value"].min(),
        }
        if has_ts:
            row["peak_high_ts"] = grp.loc[grp["value"].idxmax(), ts_col]
            row["peak_low_ts"]  = grp.loc[grp["value"].idxmin(), ts_col]
        return pd.Series(row)

    window_stats = window_df.groupby([cmdb_col, name_col]).apply(_agg_window).reset_index()

    merged = window_stats.merge(baseline, on=[cmdb_col, name_col], how="left")
    dev_high_col = f"deviation_above_{p_high_label}"
    dev_low_col  = f"drop_below_{p_low_label}"
    merged[dev_high_col] = (merged["max_value"] - merged[p_high_label]).clip(lower=0)
    merged[dev_low_col]  = (merged[p_low_label] - merged["min_value"]).clip(lower=0)

    # Relative deviation = absolute deviation / baseline (clamped to ≥1 to avoid /0).
    # Sorting by relative deviation ensures CPU/network KPIs (small units but large % spike)
    # rank above memory KPIs (large absolute bytes but modest % increase).
    merged["_rel_high"] = merged[dev_high_col] / merged[p_high_label].abs().clip(lower=1.0)
    merged["_rel_low"]  = merged[dev_low_col]  / merged[p_low_label].abs().clip(lower=1.0)

    if has_ts:
        for col in ["peak_high_ts", "peak_low_ts"]:
            merged[col] = (
                pd.to_datetime(merged[col], unit="s", utc=True)
                .dt.strftime("%Y-%m-%d %H:%M:%S")
            )

    high_cols = [cmdb_col, name_col, p_high_label, "max_value", dev_high_col]
    low_cols  = [cmdb_col, name_col, p_low_label,  "min_value", dev_low_col]
    if has_ts:
        high_cols.append("peak_high_ts")
        low_cols.append("peak_low_ts")

    high = (
        merged[merged[dev_high_col] > 0]
        .sort_values("_rel_high", ascending=False)
        .head(top_n)[high_cols]
        .rename(columns={cmdb_col: "component", name_col: "kpi"})
        .round(4)
        .reset_index(drop=True)
    )
    low = (
        merged[merged[dev_low_col] > 0]
        .sort_values("_rel_low", ascending=False)
        .head(top_n)[low_cols]
        .rename(columns={cmdb_col: "component", name_col: "kpi"})
        .round(4)
        .reset_index(drop=True)
    )

    return {"high": high, "low": low}


def _compute_kpi_deviation_table(
    full_df: pd.DataFrame,
    window_df: pd.DataFrame,
    components: list | None = None,
    high_pct: float = 0.90,
    low_pct: float = 0.10,
) -> pd.DataFrame:
    """Return one-row-per-component summary of worst KPI deviations.

    Columns: component, max_high_dev, top_high_kpi, peak_high_ts,
                        max_low_dev,  top_low_kpi,  peak_low_ts.
    Sorted by worst deviation descending. Shows ALL components (even those with dev=0).
    """
    cmdb_col = _detect_col(full_df, ["cmdb_id", "service", "service_name"])
    name_col = _detect_col(full_df, ["name", "kpi_name", "kpi", "metric_name"])
    ts_col   = _detect_col(full_df, ["timestamp", "startTime"])

    if not cmdb_col or not name_col or "value" not in full_df.columns:
        return pd.DataFrame()

    if components:
        full_df   = full_df[full_df[cmdb_col].isin(components)]
        window_df = window_df[window_df[cmdb_col].isin(components)]

    if full_df.empty or window_df.empty:
        return pd.DataFrame()

    baseline = (
        full_df.groupby([cmdb_col, name_col])["value"]
        .agg(p90=lambda x: x.quantile(high_pct), p10=lambda x: x.quantile(low_pct))
        .reset_index()
    )

    has_ts = ts_col is not None and ts_col in window_df.columns

    def _agg(grp):
        row = {"max_value": grp["value"].max(), "min_value": grp["value"].min()}
        if has_ts:
            row["peak_high_ts"] = grp.loc[grp["value"].idxmax(), ts_col]
            row["peak_low_ts"]  = grp.loc[grp["value"].idxmin(), ts_col]
        return pd.Series(row)

    window_stats = window_df.groupby([cmdb_col, name_col]).apply(_agg).reset_index()
    merged = window_stats.merge(baseline, on=[cmdb_col, name_col], how="left")
    merged["high_dev"] = (merged["max_value"] - merged["p90"]).clip(lower=0)
    merged["low_dev"]  = (merged["p10"] - merged["min_value"]).clip(lower=0)

    def _fmt_ts(ts):
        if has_ts and pd.notna(ts):
            try:
                return pd.to_datetime(ts, unit="s", utc=True).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return str(ts)
        return "-"

    all_comps = components if components else sorted(merged[cmdb_col].unique())
    rows = []
    for comp in all_comps:
        grp = merged[merged[cmdb_col] == comp]
        if grp.empty:
            rows.append({"component": comp,
                         "max_high_dev": 0, "top_high_kpi": "-", "peak_high_ts": "-",
                         "max_low_dev":  0, "top_low_kpi":  "-", "peak_low_ts":  "-"})
            continue
        bh = grp.loc[grp["high_dev"].idxmax()]
        bl = grp.loc[grp["low_dev"].idxmax()]
        rows.append({
            "component":    comp,
            "max_high_dev": round(float(bh["high_dev"]), 2),
            "top_high_kpi": bh[name_col] if bh["high_dev"] > 0 else "-",
            "peak_high_ts": _fmt_ts(bh.get("peak_high_ts")) if bh["high_dev"] > 0 else "-",
            "max_low_dev":  round(float(bl["low_dev"]), 2),
            "top_low_kpi":  bl[name_col]  if bl["low_dev"]  > 0 else "-",
            "peak_low_ts":  _fmt_ts(bl.get("peak_low_ts"))  if bl["low_dev"]  > 0 else "-",
        })

    df = pd.DataFrame(rows)
    df["_sort"] = df[["max_high_dev", "max_low_dev"]].max(axis=1)
    return df.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)


def _compute_trace_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trace spans per service."""
    service_col = _detect_col(df, ["cmdb_id", "service", "service_name"])
    if not service_col or "duration" not in df.columns:
        return pd.DataFrame([{"total_spans": len(df)}])

    summary = (
        df.groupby(service_col)["duration"]
        .agg(
            span_count="count",
            avg_duration_ms="mean",
            max_duration_ms="max",
            p95_duration_ms=lambda x: x.quantile(0.95),
        )
        .round(2)
        .reset_index()
    )
    return summary.sort_values("avg_duration_ms", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# StaticApp — reads from local filesystem (tests, fallback)
# ---------------------------------------------------------------------------

class StaticApp:
    """Service client for accessing static dataset telemetry on local filesystem."""

    def __init__(self, base_path: str):
        self.base_path = Path(base_path)

    def _get_namespace_path(self, namespace: str) -> Path:
        return self.base_path / namespace

    def _read_csv_files(self, directory: Path, pattern: str = "*.csv") -> pd.DataFrame:
        csv_files = sorted(directory.glob(pattern))
        if not csv_files:
            return pd.DataFrame()

        frames = []
        for f in csv_files:
            try:
                df = pd.read_csv(f)
                frames.append(df)
            except Exception as e:
                print(f"Warning: Failed to read {f}: {e}")

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # -- DataFrame-returning methods (used by get_* actions) --

    def fetch_logs_df(self, namespace: str, service: str = None) -> pd.DataFrame:
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(log_dir)
        return _filter_logs(df, service)

    def fetch_metrics_df(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        return _filter_by_time(df, start_time=start_time, end_time=end_time)

    def fetch_traces_df(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        trace_dir = self._get_namespace_path(namespace) / "traces"
        if not trace_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(trace_dir)
        return _filter_by_time(df, start_time=start_time, end_time=end_time)

    # -- New analytical methods --

    def fetch_log_overview(self, namespace: str) -> dict:
        """Return log summary stats (no raw data returned)."""
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return {}
        df = self._read_csv_files(log_dir)
        if df.empty:
            return {}
        return _compute_log_overview(df)

    def search_logs_df(self, namespace: str, keyword: str = None,
                       start_time=None, end_time=None, limit: int = 100,
                       service: str = None) -> pd.DataFrame:
        """Filter logs by service and/or keyword. Both are optional; at least one should be given."""
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(log_dir)
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _search_in_df(df, keyword, limit, service=service)

    def fetch_metric_summary(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        """Return per-service aggregated metric stats."""
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _compute_metric_summary(df)

    def fetch_anomaly_metrics(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        """Return services with degraded success rate or high response time."""
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _compute_anomaly_metrics(df)

    def fetch_trace_summary(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        """Return aggregated trace stats per service."""
        trace_dir = self._get_namespace_path(namespace) / "traces"
        if not trace_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(trace_dir)
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _compute_trace_summary(df)

    def _fetch_kpi_deviation_impl(
        self, namespace: str,
        start_time=None, end_time=None,
        components: list | None = None,
        top_n: int = 20,
        high_pct: float = 0.90,
        low_pct: float = 0.10,
    ) -> dict:
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return {"high": pd.DataFrame(), "low": pd.DataFrame()}
        full_df = self._read_csv_files(metric_dir)
        if full_df.empty:
            return {"high": pd.DataFrame(), "low": pd.DataFrame()}
        ts_col = _detect_col(full_df, ["timestamp", "startTime"])
        window_df = _filter_by_time_strict(full_df, timestamp_col=ts_col or "timestamp",
                                           start_time=start_time, end_time=end_time)
        if window_df.empty:
            return {"high": pd.DataFrame(), "low": pd.DataFrame()}
        return _compute_kpi_deviation(full_df, window_df, components=components,
                                      top_n=top_n, high_pct=high_pct, low_pct=low_pct)

    def fetch_kpi_high_deviation(self, namespace: str, start_time=None, end_time=None,
                                  components=None, top_n: int = 20) -> pd.DataFrame:
        """Return components whose KPI values exceed the P90 baseline in the fault window."""
        return self._fetch_kpi_deviation_impl(
            namespace, start_time=start_time, end_time=end_time,
            components=components, top_n=top_n, high_pct=0.90, low_pct=0.10,
        )["high"]

    def fetch_kpi_low_deviation(self, namespace: str, start_time=None, end_time=None,
                                 components=None, top_n: int = 20) -> pd.DataFrame:
        """Return components whose KPI values drop below the P10 baseline in the fault window."""
        return self._fetch_kpi_deviation_impl(
            namespace, start_time=start_time, end_time=end_time,
            components=components, top_n=top_n, high_pct=0.90, low_pct=0.10,
        )["low"]

    def fetch_component_kpi_deviation(self, namespace: str, component: str,
                                       start_time=None, end_time=None) -> dict:
        """Return all HIGH and LOW KPI deviations for a single component."""
        return self._fetch_kpi_deviation_impl(
            namespace, start_time=start_time, end_time=end_time,
            components=[component], top_n=50, high_pct=0.90, low_pct=0.10,
        )

    def fetch_kpi_deviation_table(self, namespace: str, start_time=None, end_time=None,
                                   components: list | None = None) -> pd.DataFrame:
        """Return one-row-per-component worst-KPI deviation summary table."""
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        full_df = self._read_csv_files(metric_dir)
        if full_df.empty:
            return pd.DataFrame()
        ts_col = _detect_col(full_df, ["timestamp", "startTime"])
        window_df = _filter_by_time_strict(full_df, timestamp_col=ts_col or "timestamp",
                                           start_time=start_time, end_time=end_time)
        if window_df.empty:
            return pd.DataFrame()
        return _compute_kpi_deviation_table(full_df, window_df, components=components)

    def fetch_metric_app_df(self, namespace: str,
                            start_time=None, end_time=None) -> pd.DataFrame:
        """Read app-level SR/MRT data from the metrics directory.

        Tries metric_app.csv first (Bank/Telecom), then metric_service.csv (Market).
        """
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        # Try metric_app.csv first, then metric_service.csv (Market)
        target = metric_dir / "metric_app.csv"
        if not target.exists():
            target = metric_dir / "metric_service.csv"
        if not target.exists():
            return pd.DataFrame()
        try:
            df = pd.read_csv(target)
        except Exception:
            return pd.DataFrame()
        ts_col = "startTime" if "startTime" in df.columns else "timestamp"
        return _filter_by_time(df, timestamp_col=ts_col,
                               start_time=start_time, end_time=end_time)

    # -- Convenience string-returning methods -- (StaticApp only)

    def get_logs(self, namespace: str, service: str = None,
                 start_time=None, end_time=None) -> str:
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return f"Error: No log data found for namespace '{namespace}'"
        df = self._read_csv_files(log_dir)
        if df.empty:
            return "No log data available."
        df = _filter_logs(df, service, start_time, end_time)
        if df.empty:
            return f"No logs found for service '{service}'"
        return df.to_string(index=False)

    def get_metrics(self, namespace: str, duration_minutes: int = 5) -> str:
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return f"Error: No metric data found for namespace '{namespace}'"
        df = self._read_csv_files(metric_dir)
        if df.empty:
            return "No metric data available."
        df = _filter_by_time(df, duration_minutes)
        if df.empty:
            return f"No metrics found within last {duration_minutes} minutes."
        return df.to_string(index=False)

    def get_traces(self, namespace: str, duration_minutes: int = 5) -> str:
        trace_dir = self._get_namespace_path(namespace) / "traces"
        if not trace_dir.exists():
            return f"Error: No trace data found for namespace '{namespace}'"
        df = self._read_csv_files(trace_dir)
        if df.empty:
            return "No trace data available."
        df = _filter_by_time(df, duration_minutes)
        if df.empty:
            return f"No traces found within last {duration_minutes} minutes."
        return df.to_string(index=False)

    def store_telemetry(self, namespace: str, telemetry_type: str,
                        data: pd.DataFrame, time_remapper=None) -> int:
        target_dir = self._get_namespace_path(namespace) / telemetry_type
        target_dir.mkdir(parents=True, exist_ok=True)
        if data.empty:
            return 0
        if time_remapper and "timestamp" in data.columns:
            data = data.copy()
            data["timestamp"] = data["timestamp"].apply(
                time_remapper.remap_timestamp
            )
        output_file = target_dir / f"{telemetry_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        data.to_csv(output_file, index=False)
        return len(data)

    def clear_telemetry(self, namespace: str):
        ns_dir = self._get_namespace_path(namespace)
        if ns_dir.exists():
            import shutil
            shutil.rmtree(ns_dir)


# ---------------------------------------------------------------------------
# DockerStaticApp — reads from inside a Docker container via docker exec
# ---------------------------------------------------------------------------

class DockerStaticApp:
    """Service client that reads telemetry from inside a Docker container.

    Uses `docker exec` to access data stored at /agent/telemetry/ inside
    the container, without exposing data to the host filesystem.
    """

    def __init__(self, container_name: str, data_path: str = "/agent/telemetry"):
        self.container_name = container_name
        self.data_path = data_path

    def _docker_exec(self, command: str) -> str:
        """Run a shell command inside the Docker container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-c", command],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout

    # Mapping from telemetry type name → single CSV filename used by the replayer
    _REPLAYER_FILE_MAP = {
        "logs": "log.csv",
        "metrics": "metric.csv",
        "traces": "trace.csv",
    }

    def _dir_exists(self, path: str) -> bool:
        """Check if a directory exists inside the container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-d", path],
            capture_output=True,
        )
        return result.returncode == 0

    def _file_exists(self, path: str) -> bool:
        """Check if a file exists inside the container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-f", path],
            capture_output=True,
        )
        return result.returncode == 0

    def _resolve_telemetry_dir(self, namespace: str,
                                type_name: str) -> Tuple[Optional[str], bool]:
        """Resolve where telemetry data lives inside the container.

        Tries two layouts in order:
          1. StaticDataset layout: {data_path}/{namespace}/{type_name}/ (directory)
          2. Replayer flat layout:  {data_path}/{file_name}            (single CSV)

        Returns:
            (path, is_flat) where is_flat=True means single-file replayer mode.
            Returns (None, False) if no data found.
        """
        # Try standard StaticDataset path: namespace subdirectory
        standard_dir = f"{self.data_path}/{namespace}/{type_name}"
        if self._dir_exists(standard_dir):
            return standard_dir, False

        # Fallback: replayer flat structure — single file at data_path root
        file_name = self._REPLAYER_FILE_MAP.get(type_name)
        if file_name:
            flat_file = f"{self.data_path}/{file_name}"
            if self._file_exists(flat_file):
                return self.data_path, True

        return None, False

    def _read_csv_files(self, directory: str) -> pd.DataFrame:
        """Read and concatenate all CSV files from a directory inside Docker."""
        ls_output = self._docker_exec(f"ls {directory}/*.csv 2>/dev/null")
        if not ls_output.strip():
            return pd.DataFrame()

        frames = []
        for csv_path in ls_output.strip().split("\n"):
            csv_path = csv_path.strip()
            if not csv_path:
                continue
            content = self._docker_exec(f"cat '{csv_path}'")
            if content.strip():
                try:
                    df = pd.read_csv(StringIO(content))
                    frames.append(df)
                except Exception as e:
                    print(f"Warning: Failed to parse {csv_path}: {e}")

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _read_telemetry_df(self, namespace: str, type_name: str) -> pd.DataFrame:
        """Read telemetry data, auto-detecting StaticDataset vs replayer layout."""
        dir_path, is_flat = self._resolve_telemetry_dir(namespace, type_name)
        if dir_path is None:
            return pd.DataFrame()

        if is_flat:
            # Replayer mode: single CSV file
            file_name = self._REPLAYER_FILE_MAP[type_name]
            content = self._docker_exec(f"cat '{dir_path}/{file_name}'")
            if not content.strip():
                return pd.DataFrame()
            try:
                return pd.read_csv(StringIO(content))
            except Exception as e:
                print(f"Warning: Failed to parse replayer {file_name}: {e}")
                return pd.DataFrame()

        return self._read_csv_files(dir_path)

    # -- DataFrame-returning methods (used by get_* actions) --

    def fetch_logs_df(self, namespace: str, service: str = None,
                      limit: int = None) -> pd.DataFrame:
        df = self._read_telemetry_df(namespace, "logs")
        df = _filter_logs(df, service)
        return df.head(limit) if limit else df

    def fetch_metrics_df(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        df = self._read_telemetry_df(namespace, "metrics")
        return _filter_by_time(df, start_time=start_time, end_time=end_time)

    def fetch_traces_df(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        df = self._read_telemetry_df(namespace, "traces")
        return _filter_by_time(df, start_time=start_time, end_time=end_time)

    # -- New analytical methods --

    def fetch_log_overview(self, namespace: str) -> dict:
        """Return log summary stats without returning raw data."""
        df = self._read_telemetry_df(namespace, "logs")
        if df.empty:
            return {}
        return _compute_log_overview(df)

    def search_logs_df(self, namespace: str, keyword: str = None,
                       start_time=None, end_time=None, limit: int = 100,
                       service: str = None) -> pd.DataFrame:
        """Filter logs by service and/or keyword. Both are optional; at least one should be given."""
        df = self._read_telemetry_df(namespace, "logs")
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _search_in_df(df, keyword, limit, service=service)

    def fetch_metric_summary(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        """Return per-service aggregated metric stats."""
        df = self._read_telemetry_df(namespace, "metrics")
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _compute_metric_summary(df)

    def fetch_anomaly_metrics(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        """Return services with degraded success rate or high response time."""
        df = self._read_telemetry_df(namespace, "metrics")
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _compute_anomaly_metrics(df)

    def fetch_trace_summary(self, namespace: str, start_time=None, end_time=None) -> pd.DataFrame:
        """Return aggregated trace stats per service."""
        df = self._read_telemetry_df(namespace, "traces")
        if df.empty:
            return pd.DataFrame()
        df = _filter_by_time(df, start_time=start_time, end_time=end_time)
        return _compute_trace_summary(df)

    def _fetch_kpi_deviation_impl(
        self, namespace: str,
        start_time=None, end_time=None,
        components: list | None = None,
        top_n: int = 20,
        high_pct: float = 0.90,
        low_pct: float = 0.10,
    ) -> dict:
        full_df = self._read_telemetry_df(namespace, "metrics")
        if full_df.empty:
            return {"high": pd.DataFrame(), "low": pd.DataFrame()}
        ts_col = _detect_col(full_df, ["timestamp", "startTime"])
        window_df = _filter_by_time_strict(full_df, timestamp_col=ts_col or "timestamp",
                                           start_time=start_time, end_time=end_time)
        if window_df.empty:
            return {"high": pd.DataFrame(), "low": pd.DataFrame()}
        return _compute_kpi_deviation(full_df, window_df, components=components,
                                      top_n=top_n, high_pct=high_pct, low_pct=low_pct)

    def fetch_kpi_high_deviation(self, namespace: str, start_time=None, end_time=None,
                                  components=None, top_n: int = 20) -> pd.DataFrame:
        """Return components whose KPI values exceed the P90 baseline in the fault window."""
        return self._fetch_kpi_deviation_impl(
            namespace, start_time=start_time, end_time=end_time,
            components=components, top_n=top_n, high_pct=0.90, low_pct=0.10,
        )["high"]

    def fetch_kpi_low_deviation(self, namespace: str, start_time=None, end_time=None,
                                 components=None, top_n: int = 20) -> pd.DataFrame:
        """Return components whose KPI values drop below the P10 baseline in the fault window."""
        return self._fetch_kpi_deviation_impl(
            namespace, start_time=start_time, end_time=end_time,
            components=components, top_n=top_n, high_pct=0.90, low_pct=0.10,
        )["low"]

    def fetch_component_kpi_deviation(self, namespace: str, component: str,
                                       start_time=None, end_time=None) -> dict:
        """Return all HIGH and LOW KPI deviations for a single component."""
        return self._fetch_kpi_deviation_impl(
            namespace, start_time=start_time, end_time=end_time,
            components=[component], top_n=50, high_pct=0.90, low_pct=0.10,
        )

    def fetch_kpi_deviation_table(self, namespace: str, start_time=None, end_time=None,
                                   components: list | None = None) -> pd.DataFrame:
        """Return one-row-per-component worst-KPI deviation summary table."""
        full_df = self._read_telemetry_df(namespace, "metrics")
        if full_df.empty:
            return pd.DataFrame()
        ts_col = _detect_col(full_df, ["timestamp", "startTime"])
        window_df = _filter_by_time_strict(full_df, timestamp_col=ts_col or "timestamp",
                                           start_time=start_time, end_time=end_time)
        if window_df.empty:
            return pd.DataFrame()
        return _compute_kpi_deviation_table(full_df, window_df, components=components)

    def fetch_metric_app_df(self, namespace: str,
                            start_time=None, end_time=None) -> pd.DataFrame:
        """Read app-level SR/MRT data from the metrics directory.

        Tries metric_app.csv first (Bank/Telecom), then metric_service.csv (Market).
        """
        dir_path, is_flat = self._resolve_telemetry_dir(namespace, "metrics")
        if dir_path is None:
            return pd.DataFrame()

        if is_flat:
            # Replayer flat layout — metric_app.csv doesn't exist separately
            return pd.DataFrame()

        # Try metric_app.csv first, then metric_service.csv (Market)
        file_path = f"{dir_path}/metric_app.csv"
        if not self._file_exists(file_path):
            file_path = f"{dir_path}/metric_service.csv"
        if not self._file_exists(file_path):
            return pd.DataFrame()

        content = self._docker_exec(f"cat '{file_path}'")
        if not content.strip():
            return pd.DataFrame()
        try:
            df = pd.read_csv(StringIO(content))
        except Exception:
            return pd.DataFrame()

        ts_col = "startTime" if "startTime" in df.columns else "timestamp"
        return _filter_by_time(df, timestamp_col=ts_col,
                               start_time=start_time, end_time=end_time)
