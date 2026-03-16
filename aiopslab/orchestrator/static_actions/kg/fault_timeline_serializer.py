"""Serialize FaultTimeline for the 3 executor pipeline injection points.

1. format_for_instruction — before code generation (RAG injector)
2. format_for_result     — result → controller (result enricher)
3. format_for_summary    — before executor LLM summary (summary injector)

All formatters accept a FaultTimeline and a list of component IDs,
and return relevant context only for those components.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from aiopslab.orchestrator.static_actions.kg.fault_timeline_builder import (
    FaultTimeline, ComponentFaultProfile, FaultWindowKPI, TimeWindow,
    BucketAnomaly, BucketComponentKPI,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: float) -> str:
    """Format Unix seconds to readable UTC time."""
    if ts <= 0:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _fmt_window(w: TimeWindow) -> str:
    return f"{_fmt_ts(w.start)} ~ {_fmt_ts(w.end)}"


def _top_kpis(profile: ComponentFaultProfile, n: int = 5) -> list[FaultWindowKPI]:
    """Return top-N KPIs by max(baseline_z, peer_z)."""
    return profile.fault_kpis[:n]  # already sorted in builder


def _format_kpi_line(kpi: FaultWindowKPI, detail: str = "full") -> str:
    """Format a single KPI anomaly line."""
    z = max(kpi.baseline_z, kpi.peer_z)
    if detail == "full":
        return (
            f"    {kpi.kpi_name}: fault_mean={kpi.fault_mean} vs baseline={kpi.baseline_mean} "
            f"({kpi.direction}, baseline_z={kpi.baseline_z}, peer_z={kpi.peer_z})"
        )
    # compact
    return f"    {kpi.kpi_name}: {kpi.fault_mean} vs baseline {kpi.baseline_mean} ({kpi.direction}, z={z:.1f})"


# ---------------------------------------------------------------------------
# 1. Instruction injection (before code generation)
# ---------------------------------------------------------------------------

def format_for_instruction(
    timeline: FaultTimeline,
    components: list[str],
    max_kpis: int = 5,
) -> str:
    """Format fault window data for executor instruction enrichment.

    Provides detailed per-component KPI stats within the fault window,
    baseline comparison, and peer values so the executor generates
    appropriate analysis code.
    """
    if not components or not timeline.component_profiles:
        return ""

    sections: list[str] = []

    for comp_id in components:
        profile = timeline.component_profiles.get(comp_id)
        if profile is None:
            continue

        top_kpis = _top_kpis(profile, max_kpis)
        if not top_kpis:
            continue

        trace_note = "" if profile.has_trace_data else " (NOT in trace data)"
        lines: list[str] = [
            f"[{comp_id}] (type: {profile.component_type}{trace_note}) "
            f"fault window: {_fmt_window(timeline.fault_window)}"
        ]

        for kpi in top_kpis:
            z = max(kpi.baseline_z, kpi.peer_z)
            if z < 1.0:
                continue
            lines.append(_format_kpi_line(kpi, detail="full"))

        if len(lines) > 1:
            sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "=== FAULT WINDOW DATA ==="
    footer = "=== END FAULT WINDOW DATA ==="
    return f"{header}\n\n" + "\n\n".join(sections) + f"\n\n{footer}"


# ---------------------------------------------------------------------------
# 2. Result enrichment (result -> controller)
# ---------------------------------------------------------------------------

def format_for_result(
    timeline: FaultTimeline,
    components: list[str],
    max_kpis: int = 3,
) -> str:
    """Format fault-window peer comparison for result enrichment.

    Compact format showing how each component's KPIs compare to peers
    during the fault window. Helps the controller judge anomaly severity.
    """
    if not components or not timeline.component_profiles:
        return ""

    sections: list[str] = []

    for comp_id in components:
        profile = timeline.component_profiles.get(comp_id)
        if profile is None:
            continue

        top_kpis = _top_kpis(profile, max_kpis)
        anomalous_kpis = [k for k in top_kpis if max(k.baseline_z, k.peer_z) >= 2.0]
        if not anomalous_kpis:
            continue

        trace_note = "" if profile.has_trace_data else ", no trace data"
        lines: list[str] = [
            f"[{comp_id}] ({profile.component_type}{trace_note}) "
            f"during fault window {_fmt_window(timeline.fault_window)}:"
        ]

        for kpi in anomalous_kpis:
            lines.append(_format_kpi_line(kpi, detail="compact"))

        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "=== FAULT-WINDOW PEER CONTEXT ==="
    footer = "=== END PEER CONTEXT ==="
    return f"{header}\n\n" + "\n\n".join(sections) + f"\n\n{footer}"


# ---------------------------------------------------------------------------
# 3. Summary injection (before executor LLM summary)
# ---------------------------------------------------------------------------

def format_for_summary(
    timeline: FaultTimeline,
    components: list[str],
    max_kpis: int = 3,
) -> str:
    """Format fault context for executor summary step.

    Brief context appended to raw result before the executor LLM
    summarizes it, so the summary includes key anomaly findings.
    """
    if not components or not timeline.component_profiles:
        return ""

    sections: list[str] = []

    for comp_id in components:
        profile = timeline.component_profiles.get(comp_id)
        if profile is None:
            continue

        top_kpis = _top_kpis(profile, max_kpis)
        anomalous_kpis = [k for k in top_kpis if max(k.baseline_z, k.peer_z) >= 2.0]
        if not anomalous_kpis:
            continue

        kpi_parts = []
        for kpi in anomalous_kpis:
            z = max(kpi.baseline_z, kpi.peer_z)
            kpi_parts.append(f"{kpi.kpi_name}={kpi.fault_mean} (baseline={kpi.baseline_mean}, z={z:.1f})")

        sections.append(
            f"  {comp_id}: " + ", ".join(kpi_parts)
        )

    if not sections:
        return ""

    return (
        f"[Fault Window Context: {_fmt_window(timeline.fault_window)}]\n"
        + "\n".join(sections)
    )


# ---------------------------------------------------------------------------
# 4. Metrics enrichment (appended to get_metrics result)
# ---------------------------------------------------------------------------

def format_for_metrics(
    timeline: FaultTimeline,
    max_components: int = 10,
    max_kpis: int = 3,
) -> str:
    """Format per-5-minute-bucket anomaly summary for get_metrics result.

    Shows anomalies broken down by each time bucket so the agent can see
    exactly WHEN each component became anomalous, not just a merged window.
    """
    if not timeline.bucket_anomalies:
        return ""

    obs_window = _fmt_window(timeline.observation_window)
    sections: list[str] = [
        f"=== ANOMALY SUMMARY (per {timeline.bucket_seconds // 60}-min bucket, {obs_window}) ===",
    ]

    any_anomaly = False
    for bucket in timeline.bucket_anomalies:
        bucket_label = f"[{_fmt_ts(bucket.bucket_start)}~{_fmt_ts(bucket.bucket_end)}]"

        if not bucket.anomalous_components:
            sections.append(f"{bucket_label} (no anomalies)")
            continue

        # Collect all component entries for this bucket, sorted by max z_score
        comp_entries: list[tuple[float, str]] = []
        for comp_id, kpi_list in bucket.anomalous_components.items():
            top_kpis = kpi_list[:max_kpis]
            kpi_parts = []
            for kpi in top_kpis:
                # Show relative change percentage for interpretability
                denom = max(abs(kpi.overall_mean), 1e-6)
                rel_pct = abs(kpi.bucket_mean - kpi.overall_mean) / denom * 100
                kpi_parts.append(
                    f"{kpi.kpi_name}={kpi.bucket_mean} (avg={kpi.overall_mean}, {kpi.direction} {rel_pct:.0f}%)"
                )
            max_z = max(k.z_score for k in top_kpis)
            comp_entries.append((max_z, f"  {comp_id}: " + "; ".join(kpi_parts)))

        # Sort by max z descending, limit components
        comp_entries.sort(key=lambda x: x[0], reverse=True)
        sections.append(bucket_label)
        for _, line in comp_entries[:max_components]:
            sections.append(line)
            any_anomaly = True

    if not any_anomaly:
        return ""

    sections.append("=== END ANOMALY SUMMARY ===")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# 5. Traces enrichment (appended to get_traces result)
# ---------------------------------------------------------------------------

def _to_unix_sec(ts: pd.Series) -> pd.Series:
    """Normalize timestamps to Unix seconds (detect ms vs s)."""
    if ts.empty:
        return ts
    median_val = float(ts.median())
    if median_val > 1e12:
        return ts / 1000.0
    return ts


def format_for_traces(
    timeline: FaultTimeline,
    trace_df: pd.DataFrame,
    max_services: int = 10,
) -> str:
    """Format per-bucket trace anomaly summary for get_traces result.

    Splits observation window into the same buckets as metrics, computes
    per-service error rate and p95 latency in each bucket, and highlights
    buckets where services show anomalous behavior vs their overall stats.
    """
    if trace_df.empty or timeline.observation_window.start <= 0:
        return ""

    # Find columns
    ts_col = None
    for col in ("startTime", "timestamp"):
        if col in trace_df.columns:
            ts_col = col
            break
    if ts_col is None:
        return ""

    lat_col = None
    for col in ("elapsedTime", "duration"):
        if col in trace_df.columns:
            lat_col = col
            break

    svc_col = None
    for col in ("cmdb_id", "serviceName"):
        if col in trace_df.columns:
            svc_col = col
            break
    if svc_col is None:
        return ""

    df = trace_df.copy()
    df["_ts_sec"] = _to_unix_sec(df[ts_col].astype(float))

    obs_start = timeline.observation_window.start
    obs_end = timeline.observation_window.end
    bucket_seconds = timeline.bucket_seconds

    df = df[(df["_ts_sec"] >= obs_start) & (df["_ts_sec"] <= obs_end)]
    if df.empty:
        return ""

    n_buckets = max(1, int((obs_end - obs_start) / bucket_seconds))
    df["_bucket"] = ((df["_ts_sec"] - obs_start) // bucket_seconds).astype(int)
    df["_bucket"] = df["_bucket"].clip(upper=n_buckets - 1)

    # Per-(service, bucket) stats
    bucket_stats: list[dict] = []
    for (svc, bucket_idx), group in df.groupby([svc_col, "_bucket"]):
        total = len(group)
        fail_count = 0
        if "success" in group.columns:
            fail_count = int((~group["success"].astype(bool)).sum())
        error_rate = fail_count / total if total > 0 else 0.0

        p95_lat = 0.0
        if lat_col is not None:
            lat = group[lat_col].astype(float).dropna()
            if not lat.empty:
                p95_lat = float(lat.quantile(0.95))

        bucket_stats.append({
            "service": str(svc),
            "bucket": int(bucket_idx),
            "total": total,
            "error_rate": error_rate,
            "p95_lat": p95_lat,
        })

    if not bucket_stats:
        return ""

    stats_df = pd.DataFrame(bucket_stats)

    # Per-service overall stats (across all buckets)
    svc_overall = stats_df.groupby("service").agg(
        overall_err=("error_rate", "mean"),
        overall_err_std=("error_rate", "std"),
        overall_lat=("p95_lat", "mean"),
        overall_lat_std=("p95_lat", "std"),
    ).reset_index()
    svc_overall = svc_overall.fillna(0.0)

    stats_df = stats_df.merge(svc_overall, on="service", how="left")

    # Detect per-bucket anomalies: error_rate or latency significantly above overall
    stats_df["err_z"] = 0.0
    valid_err = stats_df["overall_err_std"] > 1e-9
    stats_df.loc[valid_err, "err_z"] = (
        (stats_df.loc[valid_err, "error_rate"] - stats_df.loc[valid_err, "overall_err"])
        / stats_df.loc[valid_err, "overall_err_std"]
    ).clip(upper=100.0)

    stats_df["lat_z"] = 0.0
    valid_lat = stats_df["overall_lat_std"] > 1e-9
    stats_df.loc[valid_lat, "lat_z"] = (
        (stats_df.loc[valid_lat, "p95_lat"] - stats_df.loc[valid_lat, "overall_lat"])
        / stats_df.loc[valid_lat, "overall_lat_std"]
    ).clip(upper=100.0)

    obs_window = _fmt_window(timeline.observation_window)
    sections: list[str] = [
        f"=== TRACE SUMMARY (per {bucket_seconds // 60}-min bucket, {obs_window}) ===",
    ]

    any_anomaly = False
    for bucket_idx in range(n_buckets):
        bucket_start = obs_start + bucket_idx * bucket_seconds
        bucket_end = bucket_start + bucket_seconds
        bucket_label = f"[{_fmt_ts(bucket_start)}~{_fmt_ts(bucket_end)}]"

        bucket_rows = stats_df[stats_df["bucket"] == bucket_idx]
        # Filter for anomalous services in this bucket (z > 2 for error or latency)
        anom_rows = bucket_rows[(bucket_rows["err_z"] > 2.0) | (bucket_rows["lat_z"] > 2.0)]

        if anom_rows.empty:
            sections.append(f"{bucket_label} (no anomalies)")
            continue

        sections.append(bucket_label)
        # Sort by max z desc
        anom_sorted = anom_rows.copy()
        anom_sorted["_max_z"] = anom_sorted[["err_z", "lat_z"]].max(axis=1)
        anom_sorted = anom_sorted.sort_values("_max_z", ascending=False)

        for _, row in anom_sorted.head(max_services).iterrows():
            parts = []
            if row["err_z"] > 2.0:
                parts.append(f"error_rate={row['error_rate']:.1%} (avg={row['overall_err']:.1%}, z={row['err_z']:.1f})")
            if row["lat_z"] > 2.0 and row["p95_lat"] > 0:
                parts.append(f"p95_lat={row['p95_lat']:.0f}ms (avg={row['overall_lat']:.0f}ms, z={row['lat_z']:.1f})")
            if parts:
                sections.append(f"  {row['service']}: " + "; ".join(parts))
                any_anomaly = True

    if not any_anomaly:
        return (
            f"=== TRACE SUMMARY (per {bucket_seconds // 60}-min bucket, {obs_window}) ===\n"
            f"No significant per-bucket error rate or latency anomalies detected.\n"
            f"=== END TRACE SUMMARY ==="
        )

    sections.append("=== END TRACE SUMMARY ===")
    return "\n".join(sections)
