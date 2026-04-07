from __future__ import annotations

from pathlib import Path

from clients.tree_traversal.tools.deep_dive.base import (
    build_component_kpi_wide_df,
    build_peer_kpi_wide_df,
    filter_by_component,
    load_metric_long,
    minute_bucket,
    normalize_aggs,
    to_ts,
)


def get_component_kpis_minutely(
    *,
    base_path: str | Path,
    component: str,
    kpis: list[str],
    start_time,
    end_time,
    agg: str | list[str] | None = None,
) -> object:
    start_ts = to_ts(start_time)
    end_ts = to_ts(end_time)
    use_aggs = normalize_aggs(agg)
    kpi_set = {str(x).strip() for x in (kpis or []) if str(x).strip()}
    if not component:
        raise ValueError("component is required")
    if not kpi_set:
        raise ValueError("kpis is required")
    df = load_metric_long(base_path)
    if df.empty:
        return build_component_kpi_wide_df(agg_df=df, aggs=use_aggs)
    df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].copy()
    df = filter_by_component(df, component)
    if df.empty:
        return build_component_kpi_wide_df(agg_df=df, aggs=use_aggs)
    df = df[df["kpi_name"].isin(kpi_set)].copy()
    if df.empty:
        return build_component_kpi_wide_df(agg_df=df, aggs=use_aggs)
    agg_df = minute_bucket(df, by=("cmdb_id", "kpi_name"), aggs=use_aggs)
    return build_component_kpi_wide_df(
        agg_df.sort_values(["minute_ts", "cmdb_id", "kpi_name"]),
        aggs=use_aggs,
    )


def get_peer_kpi_minutely(
    *,
    base_path: str | Path,
    kpi: str,
    target_component: str,
    start_time,
    end_time,
    peer_components: list[str] | None = None,
    agg: str | list[str] | None = None,
) -> object:
    start_ts = to_ts(start_time)
    end_ts = to_ts(end_time)
    use_aggs = normalize_aggs(agg)
    kpi_name = str(kpi or "").strip()
    target = str(target_component or "").strip()
    if not kpi_name or not target:
        raise ValueError("kpi and target_component are required")
    df = load_metric_long(base_path)
    if df.empty:
        return build_peer_kpi_wide_df(agg_df=df, aggs=use_aggs)
    df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].copy()
    df = df[df["kpi_name"] == kpi_name].copy()
    if df.empty:
        return build_peer_kpi_wide_df(agg_df=df, aggs=use_aggs)
    peers = {target}
    if isinstance(peer_components, list):
        peers |= {str(x).strip() for x in peer_components if str(x).strip()}
    df = df[df["cmdb_id"].astype(str).isin(peers)].copy()
    if df.empty:
        return build_peer_kpi_wide_df(agg_df=df, aggs=use_aggs)
    agg_df = minute_bucket(df, by=("cmdb_id",), aggs=use_aggs)
    agg_df["kpi_name"] = kpi_name
    return build_peer_kpi_wide_df(
        agg_df.sort_values(["minute_ts", "cmdb_id"]),
        aggs=use_aggs,
    )
