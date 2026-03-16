"""Fault Timeline: detect fault window and compute per-component anomaly profiles.

Pre-computes:
  1. Fault time window from trace error spikes and metric change points
  2. Per-component KPI stats within the fault window vs baseline
  3. Peer comparison within the fault window

All results are used by the 3 executor pipeline injectors
(instruction, summary, result) — NOT injected into controller system prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TimeWindow:
    """A detected time window [start, end] in Unix seconds."""
    start: float = 0.0
    end: float = 0.0
    source: str = ""  # "trace", "metric", "combined"


@dataclass
class MetricChangePoint:
    """A detected change point in a metric time series."""
    cmdb_id: str = ""
    kpi_name: str = ""
    change_time: float = 0.0
    direction: str = ""       # "spike" or "drop"
    before_mean: float = 0.0
    after_mean: float = 0.0
    magnitude: float = 0.0    # |after - before| / max(|before|, 1e-6)


@dataclass
class FaultWindowKPI:
    """Stats for a single KPI during the fault window vs baseline."""
    kpi_name: str = ""
    fault_mean: float = 0.0
    fault_max: float = 0.0
    baseline_mean: float = 0.0
    baseline_std: float = 0.0
    peer_fault_median: float = 0.0
    baseline_z: float = 0.0   # (fault_mean - baseline_mean) / baseline_std
    peer_z: float = 0.0       # (fault_mean - peer_median) / peer_mad_equiv
    direction: str = ""        # "HIGH" or "LOW"


@dataclass
class ComponentFaultProfile:
    """Per-component anomaly profile within the detected fault window."""
    cmdb_id: str = ""
    component_type: str = ""
    fault_kpis: list[FaultWindowKPI] = field(default_factory=list)
    anomaly_score: float = 0.0
    has_trace_data: bool = False


@dataclass
class BucketComponentKPI:
    """Per-component KPI stats in a single time bucket."""
    kpi_name: str = ""
    bucket_mean: float = 0.0
    overall_mean: float = 0.0
    overall_std: float = 0.0
    z_score: float = 0.0       # (bucket_mean - overall_mean) / overall_std, capped at 100
    peer_median: float = 0.0   # median of same-type peers in same bucket
    direction: str = ""        # "HIGH" or "LOW"


@dataclass
class BucketAnomaly:
    """Anomalies detected in a single time bucket."""
    bucket_start: float = 0.0
    bucket_end: float = 0.0
    bucket_idx: int = 0
    # cmdb_id -> list of anomalous KPIs in this bucket
    anomalous_components: dict[str, list[BucketComponentKPI]] = field(default_factory=dict)


@dataclass
class FaultTimeline:
    """Complete pre-computed fault timeline for an incident."""
    fault_window: TimeWindow = field(default_factory=TimeWindow)
    observation_window: TimeWindow = field(default_factory=TimeWindow)
    component_profiles: dict[str, ComponentFaultProfile] = field(default_factory=dict)
    top_anomalous: list[str] = field(default_factory=list)
    metric_change_points: list[MetricChangePoint] = field(default_factory=list)
    bucket_anomalies: list[BucketAnomaly] = field(default_factory=list)
    bucket_seconds: int = 300
    dataset_type: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctype(name: str) -> str:
    """Extract component type prefix: 'docker_003' → 'docker'."""
    m = re.match(r'^([a-zA-Z]+)', str(name))
    return m.group(1) if m else str(name)


def _to_unix_sec(ts: pd.Series) -> pd.Series:
    """Normalize timestamps to Unix seconds (detect ms vs s)."""
    if ts.empty:
        return ts
    median_val = float(ts.median())
    if median_val > 1e12:
        return ts / 1000.0
    return ts


def _get_ts_col(df: pd.DataFrame) -> str | None:
    """Find the timestamp column with the most non-null values."""
    candidates = [c for c in ("timestamp", "startTime") if c in df.columns]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Pick the column with more non-null values
    return max(candidates, key=lambda c: int(df[c].notna().sum()))


def _get_kpi_col(df: pd.DataFrame) -> str | None:
    """Find the KPI name column."""
    for col in ("name", "kpi_name"):
        if col in df.columns:
            return col
    return None


# ---------------------------------------------------------------------------
# Fault window detection: traces
# ---------------------------------------------------------------------------

def _detect_fault_window_from_traces(
    trace_df: pd.DataFrame,
    obs_start: float,
    obs_end: float,
    bucket_seconds: int = 300,
) -> TimeWindow | None:
    """Detect fault window from trace error rate / latency spikes.

    Buckets traces by time, computes fail_rate and p95_latency per bucket,
    then finds buckets where these metrics exceed mean + 2*std.
    """
    ts_col = _get_ts_col(trace_df)
    if ts_col is None:
        return None

    df = trace_df.copy()
    df["_ts_sec"] = _to_unix_sec(df[ts_col].astype(float))

    # Filter to observation window
    df = df[(df["_ts_sec"] >= obs_start) & (df["_ts_sec"] <= obs_end)]
    if df.empty:
        return None

    # Create time buckets
    df["_bucket"] = ((df["_ts_sec"] - obs_start) // bucket_seconds).astype(int)

    # Compute per-bucket stats
    buckets = []
    for bucket_id, group in df.groupby("_bucket"):
        total = len(group)
        fail_count = 0
        if "success" in group.columns:
            fail_count = int((~group["success"].astype(bool)).sum())

        fail_rate = fail_count / total if total > 0 else 0.0

        latency_col = None
        for col in ("elapsedTime", "duration"):
            if col in group.columns:
                latency_col = col
                break

        p95_lat = 0.0
        if latency_col is not None:
            lat = group[latency_col].astype(float).dropna()
            if not lat.empty:
                p95_lat = float(lat.quantile(0.95))

        buckets.append({
            "bucket_id": int(bucket_id),
            "bucket_start": obs_start + int(bucket_id) * bucket_seconds,
            "total": total,
            "fail_rate": fail_rate,
            "p95_lat": p95_lat,
        })

    if len(buckets) < 2:
        return None

    fail_rates = np.array([b["fail_rate"] for b in buckets])
    p95_lats = np.array([b["p95_lat"] for b in buckets])

    # Detect anomalous buckets: fail_rate or p95_lat > mean + 2*std
    anomalous_ids: set[int] = set()

    fr_mean, fr_std = float(fail_rates.mean()), float(fail_rates.std())
    if fr_std > 1e-9:
        for b in buckets:
            if b["fail_rate"] > fr_mean + 2.0 * fr_std:
                anomalous_ids.add(b["bucket_id"])

    lat_mean, lat_std = float(p95_lats.mean()), float(p95_lats.std())
    if lat_std > 1e-9:
        for b in buckets:
            if b["p95_lat"] > lat_mean + 2.0 * lat_std:
                anomalous_ids.add(b["bucket_id"])

    if not anomalous_ids:
        return None

    # Find earliest and latest anomalous bucket
    min_id = min(anomalous_ids)
    max_id = max(anomalous_ids)
    fault_start = obs_start + min_id * bucket_seconds
    fault_end = obs_start + (max_id + 1) * bucket_seconds

    return TimeWindow(start=fault_start, end=fault_end, source="trace")


# ---------------------------------------------------------------------------
# Fault window detection: metrics
# ---------------------------------------------------------------------------

def _detect_fault_window_from_metrics(
    metric_df: pd.DataFrame,
    obs_start: float,
    obs_end: float,
    bucket_seconds: int = 300,
) -> tuple[TimeWindow | None, list[MetricChangePoint]]:
    """Detect fault window from metric change points.

    For each (cmdb_id, kpi), splits into time buckets, computes per-bucket
    mean, and detects transitions where the change exceeds 2 * overall_std.
    The time bucket with the most change points is the fault window.
    """
    ts_col = _get_ts_col(metric_df)
    kpi_col = _get_kpi_col(metric_df)
    if ts_col is None or kpi_col is None or "cmdb_id" not in metric_df.columns:
        return None, []

    df = metric_df.copy()
    df["_ts_sec"] = _to_unix_sec(df[ts_col].astype(float))
    df = df[(df["_ts_sec"] >= obs_start) & (df["_ts_sec"] <= obs_end)]
    df = df.dropna(subset=["cmdb_id", "value", kpi_col])

    if df.empty:
        return None, []

    df["_bucket"] = ((df["_ts_sec"] - obs_start) // bucket_seconds).astype(int)
    n_buckets = int((obs_end - obs_start) / bucket_seconds) + 1

    # For each (cmdb_id, kpi): compute per-bucket mean, detect change points
    change_points: list[MetricChangePoint] = []
    bucket_change_counts: dict[int, int] = {}

    grouped = df.groupby(["cmdb_id", kpi_col])
    for (cmdb_id, kpi_name), group in grouped:
        vals = group["value"].astype(float)
        overall_std = float(vals.std())
        if overall_std < 1e-9:
            continue

        bucket_means = group.groupby("_bucket")["value"].mean()
        if len(bucket_means) < 2:
            continue

        sorted_buckets = bucket_means.sort_index()
        bucket_ids = sorted_buckets.index.tolist()
        bucket_vals = sorted_buckets.values

        # Find the transition with the largest change
        max_change = 0.0
        max_change_idx = -1
        for i in range(len(bucket_ids) - 1):
            change = abs(bucket_vals[i + 1] - bucket_vals[i])
            if change > max_change:
                max_change = change
                max_change_idx = i

        if max_change_idx < 0:
            continue

        z = max_change / overall_std
        if z < 2.0:
            continue

        before_mean = float(bucket_vals[max_change_idx])
        after_mean = float(bucket_vals[max_change_idx + 1])
        change_bucket = bucket_ids[max_change_idx + 1]
        change_time = obs_start + change_bucket * bucket_seconds

        direction = "spike" if after_mean > before_mean else "drop"
        magnitude = max_change / max(abs(before_mean), 1e-6)

        cp = MetricChangePoint(
            cmdb_id=str(cmdb_id),
            kpi_name=str(kpi_name),
            change_time=change_time,
            direction=direction,
            before_mean=round(before_mean, 4),
            after_mean=round(after_mean, 4),
            magnitude=round(magnitude, 4),
        )
        change_points.append(cp)

        # Count change points per bucket
        bucket_change_counts[change_bucket] = bucket_change_counts.get(change_bucket, 0) + 1

    if not bucket_change_counts:
        return None, change_points

    # Sort change points by magnitude
    change_points.sort(key=lambda cp: cp.magnitude, reverse=True)

    # The bucket with the most change points is the fault start
    peak_bucket = max(bucket_change_counts, key=bucket_change_counts.get)

    # Expand to include adjacent buckets with significant change points
    threshold = max(bucket_change_counts.values()) * 0.3
    fault_buckets = [b for b, cnt in bucket_change_counts.items() if cnt >= threshold]
    min_bucket = min(fault_buckets)
    max_bucket = max(fault_buckets)

    fault_start = obs_start + min_bucket * bucket_seconds
    fault_end = obs_start + (max_bucket + 1) * bucket_seconds

    return TimeWindow(start=fault_start, end=fault_end, source="metric"), change_points


# ---------------------------------------------------------------------------
# Combine fault windows
# ---------------------------------------------------------------------------

def _combine_fault_windows(
    trace_window: TimeWindow | None,
    metric_window: TimeWindow | None,
    obs_start: float,
    obs_end: float,
) -> TimeWindow:
    """Combine trace and metric fault windows."""
    if trace_window is not None and metric_window is not None:
        # Use union of the two windows
        start = min(trace_window.start, metric_window.start)
        end = max(trace_window.end, metric_window.end)
        return TimeWindow(start=start, end=end, source="combined")

    if trace_window is not None:
        return trace_window
    if metric_window is not None:
        return metric_window

    # Fallback: use the latter half of the observation window
    mid = (obs_start + obs_end) / 2.0
    return TimeWindow(start=mid, end=obs_end, source="fallback")


# ---------------------------------------------------------------------------
# Per-component fault profiles
# ---------------------------------------------------------------------------

def _build_component_fault_profiles(
    metric_df: pd.DataFrame,
    trace_df: pd.DataFrame | None,
    fault_window: TimeWindow,
    obs_start: float,
    all_components: list[str] | None,
) -> dict[str, ComponentFaultProfile]:
    """Compute per-component KPI stats in fault window vs baseline.

    For each component-KPI:
    1. baseline = data before fault_window.start
    2. fault = data within fault_window
    3. baseline_z = (fault_mean - baseline_mean) / baseline_std
    4. peer_z = (fault_mean - peer_median) / peer_mad_equiv (same type)
    """
    ts_col = _get_ts_col(metric_df)
    kpi_col = _get_kpi_col(metric_df)
    if ts_col is None or kpi_col is None or "cmdb_id" not in metric_df.columns:
        return {}

    df = metric_df.copy()
    df["_ts_sec"] = _to_unix_sec(df[ts_col].astype(float))
    df = df.dropna(subset=["cmdb_id", "value", kpi_col])
    df = df[df["cmdb_id"].apply(lambda x: isinstance(x, str))]

    # Split into baseline and fault periods
    baseline_df = df[df["_ts_sec"] < fault_window.start]
    fault_df = df[
        (df["_ts_sec"] >= fault_window.start) & (df["_ts_sec"] <= fault_window.end)
    ]

    if fault_df.empty:
        return {}

    # Determine which components have trace data
    traced_components: set[str] = set()
    if trace_df is not None and not trace_df.empty:
        for col in ("cmdb_id", "dsName"):
            if col in trace_df.columns:
                traced_components.update(trace_df[col].dropna().unique().tolist())

    # Step 1: Compute per-component-KPI fault and baseline stats
    # {cmdb_id: {kpi_name: FaultWindowKPI}}
    profiles_raw: dict[str, dict[str, FaultWindowKPI]] = {}

    fault_grouped = fault_df.groupby(["cmdb_id", kpi_col])
    baseline_grouped = baseline_df.groupby(["cmdb_id", kpi_col]) if not baseline_df.empty else None

    for (cmdb_id, kpi_name), group in fault_grouped:
        cmdb_id = str(cmdb_id)
        kpi_name = str(kpi_name)
        vals = group["value"].astype(float).dropna()
        if vals.empty:
            continue

        fault_mean = float(vals.mean())
        fault_max = float(vals.max())

        # Baseline stats
        baseline_mean = 0.0
        baseline_std = 0.0
        if baseline_grouped is not None:
            try:
                bl_group = baseline_grouped.get_group((cmdb_id, kpi_name))
                bl_vals = bl_group["value"].astype(float).dropna()
                if not bl_vals.empty:
                    baseline_mean = float(bl_vals.mean())
                    baseline_std = float(bl_vals.std()) if len(bl_vals) > 1 else 0.0
            except KeyError:
                pass

        # Baseline z-score (capped at 100)
        baseline_z = 0.0
        if baseline_std > 1e-9:
            baseline_z = min(abs(fault_mean - baseline_mean) / baseline_std, 100.0)

        direction = "HIGH" if fault_mean > baseline_mean else "LOW"

        kpi_stat = FaultWindowKPI(
            kpi_name=kpi_name,
            fault_mean=round(fault_mean, 4),
            fault_max=round(fault_max, 4),
            baseline_mean=round(baseline_mean, 4),
            baseline_std=round(baseline_std, 4),
            baseline_z=round(baseline_z, 2),
            direction=direction,
        )
        profiles_raw.setdefault(cmdb_id, {})[kpi_name] = kpi_stat

    # Step 2: Peer comparison within fault window
    # Group components by type
    type_members: dict[str, list[str]] = {}
    for cmdb_id in profiles_raw:
        ctype = _ctype(cmdb_id)
        type_members.setdefault(ctype, []).append(cmdb_id)

    for ctype, members in type_members.items():
        if len(members) < 2:
            continue

        # Collect fault means per KPI across same-type peers
        kpi_peer_vals: dict[str, list[tuple[str, float]]] = {}
        for cmdb_id in members:
            for kpi_name, kpi_stat in profiles_raw.get(cmdb_id, {}).items():
                kpi_peer_vals.setdefault(kpi_name, []).append(
                    (cmdb_id, kpi_stat.fault_mean)
                )

        # Compute peer median and MAD per KPI
        for kpi_name, peer_vals in kpi_peer_vals.items():
            vals_arr = np.array([v for _, v in peer_vals])
            peer_median = float(np.median(vals_arr))
            mad = float(np.median(np.abs(vals_arr - peer_median)))
            std_equiv = mad * 1.4826 if mad > 1e-9 else 0.0

            for cmdb_id, val in peer_vals:
                kpi_stat = profiles_raw[cmdb_id].get(kpi_name)
                if kpi_stat is None:
                    continue
                kpi_stat.peer_fault_median = round(peer_median, 4)

                if std_equiv > 1e-9:
                    kpi_stat.peer_z = round(min(
                        abs(val - peer_median) / std_equiv, 100.0
                    ), 2)
                elif abs(val - peer_median) > 1e-6:
                    # All peers identical — use relative deviation (capped)
                    if abs(peer_median) > 1e-6:
                        kpi_stat.peer_z = round(min(
                            abs(val - peer_median) / abs(peer_median) * 10.0, 100.0
                        ), 2)
                    else:
                        kpi_stat.peer_z = round(min(
                            abs(val - peer_median) * 100.0, 100.0
                        ), 2)

    # Step 3: Build final profiles
    component_set = set(all_components) if all_components else None
    profiles: dict[str, ComponentFaultProfile] = {}

    for cmdb_id, kpi_dict in profiles_raw.items():
        if component_set is not None and cmdb_id not in component_set:
            continue

        kpi_list = list(kpi_dict.values())
        # Sort by max(baseline_z, peer_z) descending
        kpi_list.sort(
            key=lambda k: max(k.baseline_z, k.peer_z), reverse=True
        )

        # Anomaly score = max score across all KPIs
        anomaly_score = max(
            (max(k.baseline_z, k.peer_z) for k in kpi_list), default=0.0
        )

        profiles[cmdb_id] = ComponentFaultProfile(
            cmdb_id=cmdb_id,
            component_type=_ctype(cmdb_id),
            fault_kpis=kpi_list,
            anomaly_score=round(anomaly_score, 2),
            has_trace_data=cmdb_id in traced_components,
        )

    return profiles


# ---------------------------------------------------------------------------
# Per-bucket anomaly detection
# ---------------------------------------------------------------------------

def _build_bucket_anomalies(
    metric_df: pd.DataFrame,
    obs_start: float,
    obs_end: float,
    bucket_seconds: int = 300,
    all_components: list[str] | None = None,
    z_threshold: float = 2.0,
) -> list[BucketAnomaly]:
    """Compute per-bucket per-component-KPI anomalies using peer comparison.

    For each time bucket and each KPI, computes per-component bucket means,
    then compares each component against the median of same-type peers
    within that same bucket. Components that deviate significantly from
    peers (z >= threshold) are flagged as anomalous.

    Also computes self-temporal anomaly: each component's bucket mean
    vs its own overall mean across all buckets.

    The final z_score is max(peer_z, temporal_z).
    """
    ts_col = _get_ts_col(metric_df)
    kpi_col = _get_kpi_col(metric_df)
    if ts_col is None or kpi_col is None or "cmdb_id" not in metric_df.columns:
        return []

    df = metric_df.copy()
    df["_ts_sec"] = _to_unix_sec(df[ts_col].astype(float))
    df = df[(df["_ts_sec"] >= obs_start) & (df["_ts_sec"] <= obs_end)]
    df = df.dropna(subset=["cmdb_id", "value", kpi_col])
    df = df[df["cmdb_id"].apply(lambda x: isinstance(x, str))]

    if df.empty:
        return []

    component_set = set(all_components) if all_components else None
    if component_set is not None:
        df = df[df["cmdb_id"].isin(component_set)]

    n_buckets = max(1, int((obs_end - obs_start) / bucket_seconds))
    df["_bucket"] = ((df["_ts_sec"] - obs_start) // bucket_seconds).astype(int)
    df["_bucket"] = df["_bucket"].clip(upper=n_buckets - 1)

    # Component type mapping
    cmdb_types = {cid: _ctype(cid) for cid in df["cmdb_id"].unique()}
    df["_ctype"] = df["cmdb_id"].map(cmdb_types)

    # Step 1: per-(cmdb_id, kpi, bucket) mean
    bucket_means = (
        df.groupby(["cmdb_id", "_ctype", kpi_col, "_bucket"])["value"]
        .mean()
        .reset_index()
        .rename(columns={"value": "bucket_mean"})
    )

    # Step 2: Peer comparison — per (kpi, bucket, ctype) median and MAD
    peer_stats = (
        bucket_means.groupby([kpi_col, "_bucket", "_ctype"])["bucket_mean"]
        .agg(["median", "count"])
        .reset_index()
        .rename(columns={"median": "peer_median", "count": "n_peers"})
    )

    # Compute MAD per (kpi, bucket, ctype) for robust std estimate
    merged = bucket_means.merge(peer_stats, on=[kpi_col, "_bucket", "_ctype"], how="left")
    merged["_abs_dev"] = (merged["bucket_mean"] - merged["peer_median"]).abs()

    mad_df = (
        merged.groupby([kpi_col, "_bucket", "_ctype"])["_abs_dev"]
        .median()
        .reset_index()
        .rename(columns={"_abs_dev": "peer_mad"})
    )
    merged = merged.merge(mad_df, on=[kpi_col, "_bucket", "_ctype"], how="left")
    merged["peer_std_equiv"] = merged["peer_mad"] * 1.4826  # MAD to std conversion

    # Peer z-score
    merged["peer_z"] = 0.0
    valid_peer = (merged["peer_std_equiv"] > 1e-9) & (merged["n_peers"] >= 2)
    merged.loc[valid_peer, "peer_z"] = (
        merged.loc[valid_peer, "_abs_dev"] / merged.loc[valid_peer, "peer_std_equiv"]
    )
    # Fallback: when MAD=0 but value differs from median (all peers identical except outlier)
    zero_mad = (~valid_peer) & (merged["n_peers"] >= 2) & (merged["_abs_dev"] > 1e-6)
    merged.loc[zero_mad, "peer_z"] = np.where(
        merged.loc[zero_mad, "peer_median"].abs() > 1e-6,
        (merged.loc[zero_mad, "_abs_dev"] / merged.loc[zero_mad, "peer_median"].abs() * 10.0),
        100.0,
    )
    merged["peer_z"] = merged["peer_z"].clip(upper=100.0)

    # Step 3: Temporal anomaly — each component's bucket vs its own overall mean
    # Use leave-one-out: overall mean = mean of OTHER buckets (excludes current bucket)
    comp_overall = (
        bucket_means.groupby(["cmdb_id", kpi_col])["bucket_mean"]
        .agg(["mean", "std", "count"])
        .reset_index()
        .rename(columns={"mean": "overall_mean", "std": "overall_std", "count": "n_total_buckets"})
    )
    comp_overall["overall_std"] = comp_overall["overall_std"].fillna(0.0)

    merged = merged.merge(comp_overall, on=["cmdb_id", kpi_col], how="left")

    # Leave-one-out mean for temporal comparison
    merged["loo_mean"] = np.where(
        merged["n_total_buckets"] > 1,
        (merged["overall_mean"] * merged["n_total_buckets"] - merged["bucket_mean"])
        / (merged["n_total_buckets"] - 1),
        merged["overall_mean"],
    )

    # Temporal z: use overall_std computed from all buckets (includes spike)
    # This underestimates for single spikes, so also use loo_std when possible
    merged["temporal_z"] = 0.0
    valid_temporal = (merged["overall_std"] > 1e-9) & (merged["n_total_buckets"] >= 2)
    merged.loc[valid_temporal, "temporal_z"] = (
        (merged.loc[valid_temporal, "bucket_mean"] - merged.loc[valid_temporal, "loo_mean"]).abs()
        / merged.loc[valid_temporal, "overall_std"]
    )
    merged["temporal_z"] = merged["temporal_z"].clip(upper=100.0)

    # Final z = max(peer_z, temporal_z)
    merged["z_score"] = np.maximum(merged["peer_z"], merged["temporal_z"])
    merged["direction"] = np.where(
        merged["bucket_mean"] > merged["overall_mean"], "HIGH", "LOW"
    )

    # Minimum relative change filter: reject "statistically significant but
    # practically meaningless" deviations.  E.g. 2.3499 vs 2.3417 = 0.35%
    # change should NOT be z=100 just because std ≈ 0.
    min_rel_change = 0.10  # 10% relative change minimum
    abs_dev = (merged["bucket_mean"] - merged["overall_mean"]).abs()
    denom = merged["overall_mean"].abs().clip(lower=1e-6)
    merged["rel_change"] = abs_dev / denom

    # Filter anomalous entries: z >= threshold AND relative change >= 10%
    anomalous = merged[
        (merged["z_score"] >= z_threshold) & (merged["rel_change"] >= min_rel_change)
    ].copy()

    # Step 4: build BucketAnomaly list
    result: list[BucketAnomaly] = []
    for bucket_idx in range(n_buckets):
        bucket_start = obs_start + bucket_idx * bucket_seconds
        bucket_end = bucket_start + bucket_seconds

        bucket_rows = anomalous[anomalous["_bucket"] == bucket_idx]
        components_dict: dict[str, list[BucketComponentKPI]] = {}

        for _, row in bucket_rows.iterrows():
            comp_id = str(row["cmdb_id"])
            bkpi = BucketComponentKPI(
                kpi_name=str(row[kpi_col]),
                bucket_mean=round(float(row["bucket_mean"]), 4),
                overall_mean=round(float(row["overall_mean"]), 4),
                overall_std=round(float(row["overall_std"]), 4),
                z_score=round(float(row["z_score"]), 2),
                peer_median=round(float(row["peer_median"]), 4),
                direction=str(row["direction"]),
            )
            components_dict.setdefault(comp_id, []).append(bkpi)

        # Sort KPIs by z_score desc within each component
        for comp_id in components_dict:
            components_dict[comp_id].sort(key=lambda k: k.z_score, reverse=True)

        result.append(BucketAnomaly(
            bucket_start=bucket_start,
            bucket_end=bucket_end,
            bucket_idx=bucket_idx,
            anomalous_components=components_dict,
        ))

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_fault_timeline(
    metric_df: pd.DataFrame,
    trace_df: pd.DataFrame | None,
    time_range: dict | None,
    all_components: list[str] | None = None,
    dataset_type: str = "",
    bucket_minutes: int = 5,
    top_k: int = 10,
) -> FaultTimeline:
    """Build complete fault timeline from metric and trace data.

    Args:
        metric_df: Raw metric DataFrame.
        trace_df: Raw trace DataFrame (optional).
        time_range: {"start": unix_ts, "end": unix_ts} from QueryResult.
        all_components: List of known component IDs.
        dataset_type: "bank" or "telecom".
        bucket_minutes: Time bucket size in minutes.
        top_k: Number of top anomalous components to highlight.

    Returns:
        FaultTimeline with detected fault window and per-component profiles.
    """
    if metric_df.empty:
        return FaultTimeline(dataset_type=dataset_type)

    # Derive observation window from actual data timestamps
    ts_col = _get_ts_col(metric_df)
    if ts_col is None:
        return FaultTimeline(dataset_type=dataset_type)

    actual_ts = _to_unix_sec(metric_df[ts_col].dropna().astype(float))
    if actual_ts.empty:
        return FaultTimeline(dataset_type=dataset_type)

    obs_start = float(actual_ts.min())
    obs_end = float(actual_ts.max())

    bucket_seconds = bucket_minutes * 60
    observation_window = TimeWindow(start=obs_start, end=obs_end)

    # Detect fault window from traces
    trace_window = None
    if trace_df is not None and not trace_df.empty:
        trace_window = _detect_fault_window_from_traces(
            trace_df, obs_start, obs_end, bucket_seconds
        )

    # Detect fault window from metrics
    metric_window = None
    change_points: list[MetricChangePoint] = []
    if not metric_df.empty:
        metric_window, change_points = _detect_fault_window_from_metrics(
            metric_df, obs_start, obs_end, bucket_seconds
        )

    # Combine
    fault_window = _combine_fault_windows(
        trace_window, metric_window, obs_start, obs_end
    )

    # Build per-component fault profiles
    component_profiles = _build_component_fault_profiles(
        metric_df=metric_df,
        trace_df=trace_df,
        fault_window=fault_window,
        obs_start=obs_start,
        all_components=all_components,
    )

    # Rank top anomalous components
    sorted_comps = sorted(
        component_profiles.values(),
        key=lambda p: p.anomaly_score,
        reverse=True,
    )
    top_anomalous = [p.cmdb_id for p in sorted_comps[:top_k]]

    # Build per-bucket anomalies
    bucket_anomalies = _build_bucket_anomalies(
        metric_df=metric_df,
        obs_start=obs_start,
        obs_end=obs_end,
        bucket_seconds=bucket_seconds,
        all_components=all_components,
    )

    return FaultTimeline(
        fault_window=fault_window,
        observation_window=observation_window,
        component_profiles=component_profiles,
        top_anomalous=top_anomalous,
        metric_change_points=change_points,
        bucket_anomalies=bucket_anomalies,
        bucket_seconds=bucket_seconds,
        dataset_type=dataset_type,
    )
