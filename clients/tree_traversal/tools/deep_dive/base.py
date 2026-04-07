from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd


def to_ts(value) -> float:
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
    # ISO-8601 with timezone (e.g., 2022-03-20 09:30:00+00:00, 2022-03-20T09:30:00Z)
    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"unsupported timestamp format: {value!r}")


def ts_to_utc_str(value) -> str:
    try:
        ts = float(value)
    except Exception:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _read_metric_long(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()

    cols = set(df.columns)
    out = None
    if {"timestamp", "cmdb_id", "kpi_name", "value"}.issubset(cols):
        out = df[["timestamp", "cmdb_id", "kpi_name", "value"]].copy()
    elif {"timestamp", "cmdb_id", "name", "value"}.issubset(cols):
        out = df[["timestamp", "cmdb_id", "name", "value"]].copy()
        out = out.rename(columns={"name": "kpi_name"})
    elif {"timestamp", "service", "rr", "sr", "mrt", "count"}.issubset(cols):
        base = df[["timestamp", "service", "rr", "sr", "mrt", "count"]].copy()
        parts = []
        for k in ("rr", "sr", "mrt", "count"):
            x = base[["timestamp", "service", k]].copy()
            x.columns = ["timestamp", "cmdb_id", "value"]
            x["kpi_name"] = f"service.{k}"
            parts.append(x[["timestamp", "cmdb_id", "kpi_name", "value"]])
        out = pd.concat(parts, ignore_index=True)
    if out is None or out.empty:
        return pd.DataFrame()

    out["timestamp"] = pd.to_numeric(out["timestamp"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["cmdb_id"] = out["cmdb_id"].astype(str)
    out["kpi_name"] = out["kpi_name"].astype(str)
    out = out.dropna(subset=["timestamp", "value"])
    if out.empty:
        return pd.DataFrame()
    out["source_file"] = path.name
    return out


def load_metric_long(base_path: str | Path) -> pd.DataFrame:
    base = Path(base_path)
    metrics_dir = base / "metrics"
    if not metrics_dir.exists():
        return pd.DataFrame(columns=["timestamp", "cmdb_id", "kpi_name", "value", "source_file"])
    frames = []
    for p in sorted(metrics_dir.glob("metric_*.csv")):
        x = _read_metric_long(p)
        if not x.empty:
            frames.append(x)
    if not frames:
        return pd.DataFrame(columns=["timestamp", "cmdb_id", "kpi_name", "value", "source_file"])
    return pd.concat(frames, ignore_index=True)


def component_aliases_market(component: str) -> set[str]:
    comp = str(component or "").strip()
    if not comp:
        return set()
    out = {comp}
    if "." in comp and comp.startswith("node-"):
        out.add(comp.split(".", 1)[1])
    if "." not in comp and "-" in comp and not comp.startswith("node-"):
        out.add(f"node-1.{comp}")
        out.add(f"node-2.{comp}")
        out.add(f"node-3.{comp}")
        out.add(f"node-4.{comp}")
        out.add(f"node-5.{comp}")
        out.add(f"node-6.{comp}")
    return {x for x in out if x}


def filter_by_component(
    df: pd.DataFrame,
    component: str,
    *,
    aliases_fn=None,
) -> pd.DataFrame:
    comp = str(component or "").strip()
    if not comp or df.empty:
        return pd.DataFrame(columns=df.columns)
    aliases = {comp}
    if callable(aliases_fn):
        aliases |= set(aliases_fn(comp))
    aliases_norm = {a.lower() for a in aliases}
    out = df[df["cmdb_id"].astype(str).str.lower().isin(aliases_norm)].copy()
    return out


def normalize_aggs(agg) -> list[str]:
    if agg is None:
        raw = ["mean"]
    elif isinstance(agg, str):
        raw = [agg]
    else:
        raw = [str(x) for x in (agg or [])]
    out: list[str] = []
    seen: set[str] = set()
    allowed = {"mean", "min", "max", "p50", "p95", "p99"}
    for x in raw:
        k = str(x).strip().lower()
        if not k:
            continue
        if k not in allowed:
            raise ValueError(
                f"unsupported agg={k!r}; allowed={sorted(allowed)!r}"
            )
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out or ["mean"]


def minute_bucket(df: pd.DataFrame, *, by: Iterable[str], aggs: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    use_aggs = normalize_aggs(aggs)
    x = df.copy()
    x["minute_ts"] = (x["timestamp"] // 60) * 60
    grp_cols = ["minute_ts", *list(by)]
    g = x.groupby(grp_cols, dropna=False)
    agg = g.agg(points=("value", "size")).reset_index()
    if "mean" in use_aggs:
        agg["value_mean"] = g["value"].mean().values
    if "min" in use_aggs:
        agg["value_min"] = g["value"].min().values
    if "max" in use_aggs:
        agg["value_max"] = g["value"].max().values
    if "p50" in use_aggs:
        agg["value_p50"] = g["value"].quantile(0.50).values
    if "p95" in use_aggs:
        agg["value_p95"] = g["value"].quantile(0.95).values
    if "p99" in use_aggs:
        agg["value_p99"] = g["value"].quantile(0.99).values
    agg["minute_time_utc"] = agg["minute_ts"].map(ts_to_utc_str)
    return agg


def to_pandas_split(df: pd.DataFrame, *, index_name: str) -> dict:
    if df is None or df.empty:
        return {
            "table_format": "pandas_split",
            "index_name": index_name,
            "index": [],
            "columns": [],
            "data": [],
        }
    out = df.copy()
    out = out.where(pd.notnull(out), None)
    return {
        "table_format": "pandas_split",
        "index_name": index_name,
        "index": [str(x) for x in out.index.tolist()],
        "columns": [str(x) for x in out.columns.tolist()],
        "data": out.values.tolist(),
    }


def split_to_text(table: dict, *, max_rows: int = 80) -> str:
    if not isinstance(table, dict):
        return ""
    idx = table.get("index") or []
    cols = table.get("columns") or []
    data = table.get("data") or []
    if not cols:
        return "(empty table)"
    df = pd.DataFrame(data, index=idx, columns=cols)
    if len(df) > max_rows:
        head = df.head(max_rows).to_string()
        return f"{head}\n... ({len(df)} rows total)"
    return df.to_string()


def build_component_kpi_wide_table(agg_df: pd.DataFrame, *, aggs: list[str]) -> dict:
    if agg_df is None or agg_df.empty:
        return to_pandas_split(pd.DataFrame(), index_name="time_utc")
    metric_cols = {
        "mean": "value_mean",
        "min": "value_min",
        "max": "value_max",
        "p50": "value_p50",
        "p95": "value_p95",
        "p99": "value_p99",
    }
    pieces: list[pd.DataFrame] = []
    for a in aggs:
        val_col = metric_cols.get(a)
        if not val_col or val_col not in agg_df.columns:
            continue
        tmp = agg_df[["minute_time_utc", "kpi_name", val_col]].copy()
        tmp["col"] = tmp["kpi_name"].astype(str) + f"_{a}"
        pivot = tmp.pivot_table(
            index="minute_time_utc",
            columns="col",
            values=val_col,
            aggfunc="mean",
        )
        pieces.append(pivot)
    if not pieces:
        return to_pandas_split(pd.DataFrame(), index_name="time_utc")
    wide = pd.concat(pieces, axis=1)
    wide = wide.sort_index()
    wide = wide.loc[:, ~wide.columns.duplicated()]
    return to_pandas_split(wide, index_name="time_utc")


def build_peer_kpi_wide_table(agg_df: pd.DataFrame, *, aggs: list[str]) -> dict:
    if agg_df is None or agg_df.empty:
        return to_pandas_split(pd.DataFrame(), index_name="time_utc")
    metric_cols = {
        "mean": "value_mean",
        "min": "value_min",
        "max": "value_max",
        "p50": "value_p50",
        "p95": "value_p95",
        "p99": "value_p99",
    }
    multi_agg = len(aggs) > 1
    pieces: list[pd.DataFrame] = []
    for a in aggs:
        val_col = metric_cols.get(a)
        if not val_col or val_col not in agg_df.columns:
            continue
        tmp = agg_df[["minute_time_utc", "cmdb_id", val_col]].copy()
        if multi_agg:
            tmp["col"] = tmp["cmdb_id"].astype(str) + f"_{a}"
        else:
            tmp["col"] = tmp["cmdb_id"].astype(str)
        pivot = tmp.pivot_table(
            index="minute_time_utc",
            columns="col",
            values=val_col,
            aggfunc="mean",
        )
        pieces.append(pivot)
    if not pieces:
        return to_pandas_split(pd.DataFrame(), index_name="time_utc")
    wide = pd.concat(pieces, axis=1)
    wide = wide.sort_index()
    wide = wide.loc[:, ~wide.columns.duplicated()]
    return to_pandas_split(wide, index_name="time_utc")


def build_component_kpi_wide_df(agg_df: pd.DataFrame, *, aggs: list[str]) -> pd.DataFrame:
    if agg_df is None or agg_df.empty:
        return pd.DataFrame()
    metric_cols = {
        "mean": "value_mean",
        "min": "value_min",
        "max": "value_max",
        "p50": "value_p50",
        "p95": "value_p95",
        "p99": "value_p99",
    }
    pieces: list[pd.DataFrame] = []
    for a in aggs:
        val_col = metric_cols.get(a)
        if not val_col or val_col not in agg_df.columns:
            continue
        tmp = agg_df[["minute_time_utc", "kpi_name", val_col]].copy()
        tmp["col"] = tmp["kpi_name"].astype(str) + f"_{a}"
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


def build_peer_kpi_wide_df(agg_df: pd.DataFrame, *, aggs: list[str]) -> pd.DataFrame:
    if agg_df is None or agg_df.empty:
        return pd.DataFrame()
    metric_cols = {
        "mean": "value_mean",
        "min": "value_min",
        "max": "value_max",
        "p50": "value_p50",
        "p95": "value_p95",
        "p99": "value_p99",
    }
    multi_agg = len(aggs) > 1
    pieces: list[pd.DataFrame] = []
    for a in aggs:
        val_col = metric_cols.get(a)
        if not val_col or val_col not in agg_df.columns:
            continue
        tmp = agg_df[["minute_time_utc", "cmdb_id", val_col]].copy()
        if multi_agg:
            tmp["col"] = tmp["cmdb_id"].astype(str) + f"_{a}"
        else:
            tmp["col"] = tmp["cmdb_id"].astype(str)
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
