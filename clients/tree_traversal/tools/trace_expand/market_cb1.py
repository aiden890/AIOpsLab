from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

import pandas as pd


_OK_STATUS = {"0", "200", "ok"}


def _to_ts(value) -> float:
    if value is None:
        raise ValueError("timestamp is required")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp is required")
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"unsupported timestamp format: {value!r}")


def _normalize_aggs(agg, *, default: list[str], allowed: set[str]) -> list[str]:
    if agg is None:
        raw = list(default)
    elif isinstance(agg, str):
        raw = [agg]
    else:
        raw = [str(x) for x in (agg or [])]
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        k = str(x).strip().lower()
        if not k:
            continue
        if k not in allowed:
            raise ValueError(f"unsupported agg={k!r}; allowed={sorted(allowed)!r}")
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out or list(default)


def _ts_to_utc_str(value) -> str:
    try:
        ts = float(value)
    except Exception:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load_csv(base_path: str | Path, rel: str) -> pd.DataFrame:
    path = Path(base_path) / rel
    if not path.exists():
        raise FileNotFoundError(f"missing file: {path}")
    return pd.read_csv(path)


def _norm_component(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    if "." in text and text.startswith("node-"):
        return text.split(".", 1)[1]
    return text


def _focus_aliases(component: str) -> set[str]:
    """Build matching aliases for focus component in edge tools."""
    raw = str(component or "").strip()
    if not raw:
        return set()
    base = _norm_component(raw)
    aliases: set[str] = set()
    for x in (raw, base):
        t = str(x or "").strip()
        if not t:
            continue
        aliases.add(t.lower())
        # Service-level alias: recommendationservice-grpc -> recommendationservice
        if t.lower().endswith("-grpc") and len(t) > len("-grpc"):
            aliases.add(t[:-5].strip().lower())
    return {a for a in aliases if a}


def _trace_edges_df(base_path: str | Path, start_ts: float, end_ts: float) -> pd.DataFrame:
    df = _load_csv(base_path, "traces/trace_span.csv")
    required = {"timestamp", "cmdb_id", "span_id", "trace_id", "duration", "status_code", "parent_span"}
    if not required.issubset(set(df.columns)):
        missing = sorted(required - set(df.columns))
        raise ValueError(f"trace_span.csv missing required columns: {missing}")

    spans = df.copy()
    spans["timestamp"] = pd.to_numeric(spans["timestamp"], errors="coerce")
    spans = spans[(spans["timestamp"] >= start_ts) & (spans["timestamp"] <= end_ts)].copy()
    if spans.empty:
        return pd.DataFrame(columns=["timestamp", "caller", "callee", "duration", "is_error"])

    spans["span_id"] = spans["span_id"].astype(str)
    spans["trace_id"] = spans["trace_id"].astype(str)
    spans["parent_span"] = spans["parent_span"].astype(str)
    spans["cmdb_id"] = spans["cmdb_id"].astype(str)
    spans["duration"] = pd.to_numeric(spans["duration"], errors="coerce")
    spans["status_code"] = spans["status_code"].astype(str)

    parents = spans[["trace_id", "span_id", "cmdb_id"]].rename(
        columns={"span_id": "parent_span", "cmdb_id": "parent_cmdb_id"}
    )
    merged = spans.merge(parents, on=["trace_id", "parent_span"], how="left")
    merged = merged.dropna(subset=["parent_cmdb_id", "cmdb_id"]).copy()
    if merged.empty:
        return pd.DataFrame(columns=["timestamp", "caller", "callee", "duration", "is_error"])

    merged["caller"] = merged["parent_cmdb_id"].astype(str).map(_norm_component)
    merged["callee"] = merged["cmdb_id"].astype(str).map(_norm_component)
    merged["status_norm"] = merged["status_code"].str.lower().str.strip()
    merged["is_error"] = ~merged["status_norm"].isin(_OK_STATUS)

    out = merged[["timestamp", "caller", "callee", "duration", "is_error"]].copy()
    out = out[(out["caller"] != "") & (out["callee"] != "")]
    return out


_MESH_EDGE_RE = re.compile(r"^(?P<pod>[^.]+)\.(?P<role>source|destination)\.(?P<src>[^.]+)\.(?P<dst>.+)$")


def _parse_mesh_cmdb_edge(
    cmdb_id: str,
    *,
    granularity: str = "service",
) -> tuple[str, str, str] | None:
    m = _MESH_EDGE_RE.match(str(cmdb_id or "").strip())
    if not m:
        return None
    pod = _norm_component(m.group("pod"))
    role = str(m.group("role") or "").strip().lower()
    src = _norm_component(m.group("src"))
    dst = _norm_component(m.group("dst"))
    g = str(granularity or "service").strip().lower()
    if g not in {"service", "pod"}:
        raise ValueError("granularity must be one of service|pod")
    if g == "pod":
        if role == "source":
            src, dst = pod, dst
        elif role == "destination":
            src, dst = src, pod
    if not src or not dst:
        return None
    return src, dst, role


def _mesh_df(
    base_path: str | Path,
    start_ts: float,
    end_ts: float,
    *,
    granularity: str = "service",
) -> pd.DataFrame:
    df = _load_csv(base_path, "metrics/metric_mesh.csv")
    required = {"timestamp", "cmdb_id", "kpi_name", "value"}
    if not required.issubset(set(df.columns)):
        missing = sorted(required - set(df.columns))
        raise ValueError(f"metric_mesh.csv missing required columns: {missing}")

    mesh = df.copy()
    mesh["timestamp"] = pd.to_numeric(mesh["timestamp"], errors="coerce")
    mesh["value"] = pd.to_numeric(mesh["value"], errors="coerce")
    mesh = mesh[(mesh["timestamp"] >= start_ts) & (mesh["timestamp"] <= end_ts)].copy()
    if mesh.empty:
        return pd.DataFrame(columns=["timestamp", "src", "dst", "role", "kpi_name", "value", "is_error"])

    pairs = mesh["cmdb_id"].astype(str).map(
        lambda x: _parse_mesh_cmdb_edge(x, granularity=granularity)
    )
    mesh = mesh[pairs.notna()].copy()
    if mesh.empty:
        return pd.DataFrame(columns=["timestamp", "src", "dst", "role", "kpi_name", "value", "is_error"])

    mesh[["src", "dst", "role"]] = pd.DataFrame(list(pairs[pairs.notna()]), index=mesh.index)
    mesh["kpi_name"] = mesh["kpi_name"].astype(str)

    def _is_error_kpi(k: str) -> bool:
        k = str(k or "").lower()
        if not k.startswith("istio_request_duration_milliseconds"):
            return False
        parts = k.split(".")
        for p in parts:
            if p.isdigit() and len(p) == 3:
                return p not in {"200"}
        return False

    mesh["is_error"] = mesh["kpi_name"].map(_is_error_kpi)
    return mesh[["timestamp", "src", "dst", "role", "kpi_name", "value", "is_error"]].copy()


def _trace_edge_points(base_path: str | Path, start_ts: float, end_ts: float) -> pd.DataFrame:
    edges = _trace_edges_df(base_path, start_ts, end_ts)
    if edges.empty:
        return pd.DataFrame(columns=["minute_time_utc", "edge", "role", "latency", "error_rate"])
    x = edges.copy()
    x["minute_ts"] = (x["timestamp"] // 60) * 60
    x["minute_time_utc"] = x["minute_ts"].map(_ts_to_utc_str)
    x["edge"] = x["caller"].astype(str) + "->" + x["callee"].astype(str)
    x["latency"] = pd.to_numeric(x["duration"], errors="coerce")
    x["error_rate"] = pd.to_numeric(x["is_error"], errors="coerce").fillna(0.0)
    return x[["minute_time_utc", "edge", "latency", "error_rate"]].copy()


def _mesh_edge_points(
    base_path: str | Path,
    start_ts: float,
    end_ts: float,
    *,
    granularity: str = "service",
) -> pd.DataFrame:
    mesh = _mesh_df(base_path, start_ts, end_ts, granularity=granularity)
    if mesh.empty:
        return pd.DataFrame(columns=["minute_time_utc", "edge", "latency", "error_rate"])
    req = mesh[mesh["kpi_name"].str.startswith("istio_request_duration_milliseconds", na=False)].copy()
    if req.empty:
        return pd.DataFrame(columns=["minute_time_utc", "edge", "latency", "error_rate"])
    req["minute_ts"] = (req["timestamp"] // 60) * 60
    req["minute_time_utc"] = req["minute_ts"].map(_ts_to_utc_str)
    req["edge"] = req["src"].astype(str) + "->" + req["dst"].astype(str)
    req["latency"] = pd.to_numeric(req["value"], errors="coerce")
    req["error_rate"] = pd.to_numeric(req["is_error"], errors="coerce").fillna(0.0)
    return req[["minute_time_utc", "edge", "role", "latency", "error_rate"]].copy()


def _mesh_error_rate_points(
    base_path: str | Path,
    start_ts: float,
    end_ts: float,
    *,
    granularity: str = "service",
) -> pd.DataFrame:
    mesh = _mesh_df(base_path, start_ts, end_ts, granularity=granularity)
    if mesh.empty:
        return pd.DataFrame(columns=["minute_time_utc", "edge", "role", "error_rate"])
    req = mesh[mesh["kpi_name"].str.startswith("istio_requests.", na=False)].copy()
    if req.empty:
        return pd.DataFrame(columns=["minute_time_utc", "edge", "role", "error_rate"])

    req["minute_ts"] = (req["timestamp"] // 60) * 60
    req["minute_time_utc"] = req["minute_ts"].map(_ts_to_utc_str)
    req["edge"] = req["src"].astype(str) + "->" + req["dst"].astype(str)
    req["is_error_req"] = ~req["kpi_name"].astype(str).str.contains(r"\.200\.", regex=True, na=False)
    req["value"] = pd.to_numeric(req["value"], errors="coerce").fillna(0.0)
    req["err_value"] = req["value"].where(req["is_error_req"], 0.0)

    agg = (
        req.groupby(["minute_time_utc", "edge", "role"], dropna=False)
        .agg(total=("value", "sum"), err=("err_value", "sum"))
        .reset_index()
    )
    agg["error_rate"] = agg["err"] / agg["total"].where(agg["total"] > 0, 1.0)
    agg["error_rate"] = agg["error_rate"].fillna(0.0)
    return agg[["minute_time_utc", "edge", "role", "error_rate"]].copy()


def _focus_components(
    *,
    base_path: str | Path,
    focus_component: str,
    start_ts: float,
    end_ts: float,
) -> set[str]:
    focus_raw = str(focus_component or "").strip()
    out: set[str] = set(_focus_aliases(focus_raw))
    if focus_raw.startswith("node-"):
        try:
            coloc = list_colocated_components(
                base_path=base_path,
                node_component=focus_raw,
                start_time=start_ts,
                end_time=end_ts,
                top_k=500,
            )
        except Exception:
            coloc = {}
        for item in coloc.get("data", []) or []:
            comp = str(item.get("component") or "").strip().lower()
            if comp:
                out.add(comp)
    return out


def _filter_points_by_focus(
    points: pd.DataFrame,
    *,
    focus_set: set[str],
    direction: str,
) -> pd.DataFrame:
    if points.empty:
        return points
    if not focus_set:
        return points
    pairs = points["edge"].astype(str).str.split("->", n=1, expand=True)
    if pairs.shape[1] != 2:
        return points.iloc[0:0].copy()
    caller = pairs[0].str.lower()
    callee = pairs[1].str.lower()
    d = str(direction or "").strip().lower()
    def _match_focus(s: pd.Series) -> pd.Series:
        out = s.isin(focus_set)
        for alias in focus_set:
            a = str(alias or "").strip()
            if not a:
                continue
            out = out | s.str.startswith(a + "-", na=False)
        return out

    if d == "caller":
        return points[_match_focus(caller)].copy()
    if d == "callee":
        return points[_match_focus(callee)].copy()
    raise ValueError("direction must be one of caller|callee")


def _build_metric_wide_df(
    points: pd.DataFrame,
    *,
    value_col: str,
    aggs: list[str],
    source_suffix: str | None = None,
) -> pd.DataFrame:
    if points.empty:
        return pd.DataFrame()
    allowed = {"mean", "min", "max", "p50", "p95", "p99"}
    use_aggs = [a for a in aggs if a in allowed]
    if not use_aggs:
        return pd.DataFrame()
    g = points.groupby(["minute_time_utc", "edge"], dropna=False)
    base = g.agg(points=(value_col, "size")).reset_index()
    if "mean" in use_aggs:
        base["value_mean"] = g[value_col].mean().values
    if "min" in use_aggs:
        base["value_min"] = g[value_col].min().values
    if "max" in use_aggs:
        base["value_max"] = g[value_col].max().values
    if "p50" in use_aggs:
        base["value_p50"] = g[value_col].quantile(0.50).values
    if "p95" in use_aggs:
        base["value_p95"] = g[value_col].quantile(0.95).values
    if "p99" in use_aggs:
        base["value_p99"] = g[value_col].quantile(0.99).values

    metric_cols = {
        "mean": "value_mean",
        "min": "value_min",
        "max": "value_max",
        "p50": "value_p50",
        "p95": "value_p95",
        "p99": "value_p99",
    }
    pieces: list[pd.DataFrame] = []
    for a in use_aggs:
        val_col = metric_cols.get(a)
        if not val_col or val_col not in base.columns:
            continue
        tmp = base[["minute_time_utc", "edge", val_col]].copy()
        suffix = f"_{source_suffix}" if source_suffix else ""
        tmp["col"] = tmp["edge"].astype(str) + f"_{a}{suffix}"
        pivot = tmp.pivot_table(
            index="minute_time_utc",
            columns="col",
            values=val_col,
            aggfunc="mean",
        )
        pieces.append(pivot)
    if not pieces:
        return pd.DataFrame()
    wide = pd.concat(pieces, axis=1)
    wide = wide.sort_index()
    wide = wide.loc[:, ~wide.columns.duplicated()]
    return wide


def _top_edges(points: pd.DataFrame, *, top_k: int) -> set[str]:
    if points.empty:
        return set()
    cnt = (
        points.groupby("edge", dropna=False)
        .agg(points=("edge", "size"))
        .sort_values("points", ascending=False)
        .head(int(top_k))
    )
    return {str(x) for x in cnt.index.tolist()}


def get_edge_latency_minutely(
    *,
    base_path: str | Path,
    focus_component: str,
    direction: str,
    start_time,
    end_time,
    source: str = "auto",
    granularity: str = "service",
    agg: str | list[str] | None = None,
    top_k: int = 10,
) -> object:
    """Return edge latency minute table for focus caller/callee.

    columns: {edge}_{agg} for single source, {edge}_{agg}_{trace|mesh} for source=both.
    """
    start_ts = _to_ts(start_time)
    end_ts = _to_ts(end_time)
    use_aggs = _normalize_aggs(
        agg,
        default=["p95"],
        allowed={"mean", "min", "max", "p50", "p95", "p99"},
    )
    src = str(source or "auto").strip().lower()
    if src not in {"trace_span", "mesh", "both", "auto"}:
        raise ValueError("source must be one of trace_span|mesh|both|auto")
    gran = str(granularity or "service").strip().lower()
    if gran not in {"service", "pod"}:
        raise ValueError("granularity must be one of service|pod")
    focus_set = _focus_components(
        base_path=base_path,
        focus_component=focus_component,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    use_trace = src in {"trace_span", "both", "auto"}
    use_mesh = src in {"mesh", "both"}
    trace_points = pd.DataFrame()
    mesh_points = pd.DataFrame()
    if use_trace:
        trace_points = _filter_points_by_focus(
            _trace_edge_points(base_path, start_ts, end_ts),
            focus_set=focus_set,
            direction=direction,
        )
        if src == "auto" and trace_points.empty:
            use_mesh = True
    if use_mesh:
        mesh_points = _filter_points_by_focus(
            _mesh_edge_points(base_path, start_ts, end_ts, granularity=gran),
            focus_set=focus_set,
            direction=direction,
        )
        if gran == "pod" and "role" in mesh_points.columns:
            role_need = "destination" if str(direction or "").strip().lower() == "callee" else "source"
            mesh_points = mesh_points[mesh_points["role"].astype(str).str.lower() == role_need].copy()

    if src == "both":
        top_edges = _top_edges(pd.concat([trace_points, mesh_points], ignore_index=True), top_k=top_k)
        if top_edges:
            trace_points = trace_points[trace_points["edge"].isin(top_edges)].copy()
            mesh_points = mesh_points[mesh_points["edge"].isin(top_edges)].copy()
        tdf = _build_metric_wide_df(trace_points, value_col="latency", aggs=use_aggs, source_suffix="trace")
        mdf = _build_metric_wide_df(mesh_points, value_col="latency", aggs=use_aggs, source_suffix="mesh")
        if tdf.empty and mdf.empty:
            return pd.DataFrame()
        if tdf.empty:
            return mdf
        if mdf.empty:
            return tdf
        return pd.concat([tdf, mdf], axis=1).sort_index()

    points = trace_points if (src == "trace_span" or (src == "auto" and not trace_points.empty)) else mesh_points
    if points.empty:
        return pd.DataFrame()
    top_edges = _top_edges(points, top_k=top_k)
    if top_edges:
        points = points[points["edge"].isin(top_edges)].copy()
    return _build_metric_wide_df(points, value_col="latency", aggs=use_aggs, source_suffix=None)


def get_edge_error_rate_minutely(
    *,
    base_path: str | Path,
    focus_component: str,
    direction: str,
    start_time,
    end_time,
    source: str = "auto",
    granularity: str = "service",
    agg: str | list[str] | None = None,
    top_k: int = 10,
) -> object:
    """Return edge error-rate minute table for focus caller/callee."""
    start_ts = _to_ts(start_time)
    end_ts = _to_ts(end_time)
    use_aggs = _normalize_aggs(
        agg,
        default=["mean", "max"],
        allowed={"mean", "min", "max", "p50", "p95", "p99"},
    )
    src = str(source or "auto").strip().lower()
    if src not in {"trace_span", "mesh", "both", "auto"}:
        raise ValueError("source must be one of trace_span|mesh|both|auto")
    gran = str(granularity or "service").strip().lower()
    if gran not in {"service", "pod"}:
        raise ValueError("granularity must be one of service|pod")
    focus_set = _focus_components(
        base_path=base_path,
        focus_component=focus_component,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    # For error-rate, mesh is preferred by default when auto.
    use_trace = src in {"trace_span", "both"}
    use_mesh = src in {"mesh", "both", "auto"}
    trace_points = pd.DataFrame()
    mesh_points = pd.DataFrame()
    if use_mesh:
        mesh_points = _filter_points_by_focus(
            _mesh_error_rate_points(base_path, start_ts, end_ts, granularity=gran),
            focus_set=focus_set,
            direction=direction,
        )
        if gran == "pod" and "role" in mesh_points.columns:
            role_need = "destination" if str(direction or "").strip().lower() == "callee" else "source"
            mesh_points = mesh_points[mesh_points["role"].astype(str).str.lower() == role_need].copy()
        if src == "auto" and mesh_points.empty:
            use_trace = True
    if use_trace:
        trace_points = _filter_points_by_focus(
            _trace_edge_points(base_path, start_ts, end_ts),
            focus_set=focus_set,
            direction=direction,
        )

    if src == "both":
        top_edges = _top_edges(pd.concat([trace_points, mesh_points], ignore_index=True), top_k=top_k)
        if top_edges:
            trace_points = trace_points[trace_points["edge"].isin(top_edges)].copy()
            mesh_points = mesh_points[mesh_points["edge"].isin(top_edges)].copy()
        tdf = _build_metric_wide_df(trace_points, value_col="error_rate", aggs=use_aggs, source_suffix="trace")
        mdf = _build_metric_wide_df(mesh_points, value_col="error_rate", aggs=use_aggs, source_suffix="mesh")
        if tdf.empty and mdf.empty:
            return pd.DataFrame()
        if tdf.empty:
            return mdf
        if mdf.empty:
            return tdf
        return pd.concat([tdf, mdf], axis=1).sort_index()

    points = mesh_points if (src == "mesh" or (src == "auto" and not mesh_points.empty)) else trace_points
    if points.empty:
        return pd.DataFrame()
    top_edges = _top_edges(points, top_k=top_k)
    if top_edges:
        points = points[points["edge"].isin(top_edges)].copy()
    return _build_metric_wide_df(points, value_col="error_rate", aggs=use_aggs, source_suffix=None)


def list_colocated_components(
    *,
    base_path: str | Path,
    node_component: str,
    start_time,
    end_time,
    top_k: int = 20,
) -> dict:
    """List pod components colocated on a given node from metric_container cmdb_id."""
    node = str(node_component or "").strip()
    if not node:
        raise ValueError("node_component is required")
    start_ts = _to_ts(start_time)
    end_ts = _to_ts(end_time)

    df = _load_csv(base_path, "metrics/metric_container.csv")
    required = {"timestamp", "cmdb_id"}
    if not required.issubset(set(df.columns)):
        missing = sorted(required - set(df.columns))
        raise ValueError(f"metric_container.csv missing required columns: {missing}")

    c = df.copy()
    c["timestamp"] = pd.to_numeric(c["timestamp"], errors="coerce")
    c = c[(c["timestamp"] >= start_ts) & (c["timestamp"] <= end_ts)].copy()
    c = c[c["cmdb_id"].astype(str).str.startswith(f"{node}.", na=False)]
    window_meta = {
        "start_ts": float(start_ts),
        "start_time_utc": _ts_to_utc_str(start_ts),
        "end_ts": float(end_ts),
        "end_time_utc": _ts_to_utc_str(end_ts),
    }
    if c.empty:
        return {
            "status": "ok",
            "window": window_meta,
            "data": [],
            "notes": "no colocated components in window",
        }

    c["pod"] = c["cmdb_id"].astype(str).map(_norm_component)
    out = (
        c.groupby("pod", dropna=False)
        .agg(first_ts=("timestamp", "min"), last_ts=("timestamp", "max"), points=("pod", "size"))
        .reset_index()
        .sort_values("points", ascending=False)
        .head(int(top_k))
    )
    rows = []
    for r in out.to_dict("records"):
        rows.append(
            {
                "component": str(r["pod"]),
                "node": node,
                "points": int(r["points"]),
                "first_ts": float(r["first_ts"]),
                "first_time_utc": _ts_to_utc_str(r["first_ts"]),
                "last_ts": float(r["last_ts"]),
                "last_time_utc": _ts_to_utc_str(r["last_ts"]),
            }
        )
    return {"status": "ok", "window": window_meta, "data": rows, "notes": f"components={len(rows)}"}
