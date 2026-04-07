from __future__ import annotations

from pathlib import Path

import pandas as pd

from clients.tree_traversal.tools.deep_dive.base import (
    build_component_kpi_wide_df,
    build_peer_kpi_wide_df,
    component_aliases_market,
    filter_by_component,
    load_metric_long,
    minute_bucket,
    normalize_aggs,
    to_ts,
)
from clients.tree_traversal.tools.trace_expand.market_cb1 import (
    get_edge_error_rate_minutely,
    get_edge_latency_minutely,
)


def _is_mesh_edge_kpi(name: str) -> bool:
    k = str(name or "").strip().lower()
    if not k.startswith("istio_"):
        return False
    # Keep istio agent/runtime-like metrics on component path.
    if k.startswith("istio_agent_"):
        return False
    # Edge/traffic/request/tcp families should be read from metric_mesh edges.
    return (
        k.startswith("istio_request")
        or k.startswith("istio_requests")
        or k.startswith("istio_tcp_")
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

    edge_kpis = {k for k in kpi_set if _is_mesh_edge_kpi(k)}
    comp_kpis = set(kpi_set) - edge_kpis
    parts: list[pd.DataFrame] = []

    # 1) Component-scoped KPIs (metric_service/metric_container/metric_node/metric_runtime).
    if comp_kpis:
        df = load_metric_long(base_path)
        if not df.empty:
            df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].copy()
            df = filter_by_component(df, component, aliases_fn=component_aliases_market)
            if not df.empty:
                df = df[df["kpi_name"].isin(comp_kpis)].copy()
                if not df.empty:
                    agg_df = minute_bucket(df, by=("cmdb_id", "kpi_name"), aggs=use_aggs)
                    comp_wide = build_component_kpi_wide_df(
                        agg_df.sort_values(["minute_ts", "cmdb_id", "kpi_name"]),
                        aggs=use_aggs,
                    )
                    if isinstance(comp_wide, pd.DataFrame) and not comp_wide.empty:
                        parts.append(comp_wide)

    # 2) Istio edge KPIs (metric_mesh) auto-routed via edge tools.
    if edge_kpis:
        # Use pod granularity so replicas (e.g., recommendationservice-0/1/2) are distinguishable.
        lat_df = get_edge_latency_minutely(
            base_path=base_path,
            focus_component=component,
            direction="callee",
            start_time=start_ts,
            end_time=end_ts,
            source="mesh",
            granularity="pod",
            agg=use_aggs,
            top_k=20,
        )
        if isinstance(lat_df, pd.DataFrame) and not lat_df.empty:
            lat_df = lat_df.rename(columns={c: f"{c}_lat" for c in lat_df.columns})
            parts.append(lat_df)
        err_df = get_edge_error_rate_minutely(
            base_path=base_path,
            focus_component=component,
            direction="callee",
            start_time=start_ts,
            end_time=end_ts,
            source="mesh",
            granularity="pod",
            agg=["mean", "max"],
            top_k=20,
        )
        if isinstance(err_df, pd.DataFrame) and not err_df.empty:
            err_df = err_df.rename(columns={c: f"{c}_err" for c in err_df.columns})
            parts.append(err_df)

    if not parts:
        return pd.DataFrame()
    wide = pd.concat(parts, axis=1).sort_index()
    wide = wide.loc[:, ~wide.columns.duplicated()]
    return wide

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
    if not kpi_name:
        raise ValueError("kpi is required")
    target = str(target_component or "").strip()
    if not target:
        raise ValueError("target_component is required")

    df = load_metric_long(base_path)
    if df.empty:
        return build_peer_kpi_wide_df(agg_df=df, aggs=use_aggs)
    df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].copy()
    df = df[df["kpi_name"] == kpi_name].copy()
    if df.empty:
        return build_peer_kpi_wide_df(agg_df=df, aggs=use_aggs)

    comps = {target}
    if isinstance(peer_components, list):
        comps |= {str(x).strip() for x in peer_components if str(x).strip()}
    comp_aliases = set()
    for c in comps:
        comp_aliases |= component_aliases_market(c)
    comp_aliases = {x.lower() for x in comp_aliases if x}
    scoped = df[df["cmdb_id"].astype(str).str.lower().isin(comp_aliases)].copy()
    if scoped.empty:
        return build_peer_kpi_wide_df(agg_df=scoped, aggs=use_aggs)

    agg_df = minute_bucket(scoped, by=("cmdb_id",), aggs=use_aggs)
    agg_df["kpi_name"] = kpi_name
    return build_peer_kpi_wide_df(
        agg_df.sort_values(["minute_ts", "cmdb_id"]),
        aggs=use_aggs,
    )
