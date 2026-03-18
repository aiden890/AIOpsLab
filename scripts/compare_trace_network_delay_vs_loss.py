"""Compare trace span characteristics: network delay vs network loss.

Compares two Telecom trace files to see how trace volume, duration, fail_rate,
and network_gap differ between network delay and network loss faults.

Usage:
    python scripts/compare_trace_network_delay_vs_loss.py
    python scripts/compare_trace_network_delay_vs_loss.py --delay-path path/to/delay.csv --loss-path path/to/loss.csv

    # Extract caller-callee edges from id/pid + cmdb_id-dsName (JDBC):
    python scripts/compare_trace_network_delay_vs_loss.py --extract-edges prefiltered_telemetry/task_2-0/static-telecom/traces/trace_span.csv
    python scripts/compare_trace_network_delay_vs_loss.py --extract-edges path/to/trace.csv --edges-output caller_callee.csv

    # Verify extraction (edge counts, os→docker, per-component in/out):
    python scripts/compare_trace_network_delay_vs_loss.py --verify
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def _normalize_trace_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize trace schema to include startTime, cmdb_id, dsName, elapsedTime, success."""
    if "dsName" in df.columns and "startTime" in df.columns:
        return df

    parent_col = None
    if "parent_id" in df.columns:
        parent_col = "parent_id"
    elif "parent_span" in df.columns:
        parent_col = "parent_span"

    if "span_id" not in df.columns or parent_col is None:
        return df

    df = df.copy()
    if "startTime" not in df.columns and "timestamp" in df.columns:
        df["startTime"] = df["timestamp"]
    if "elapsedTime" not in df.columns and "duration" in df.columns:
        df["elapsedTime"] = df["duration"]
    if "success" not in df.columns:
        if "status_code" in df.columns:
            df["success"] = df["status_code"].astype(str).str.strip().str.lower().isin(
                ["0", "200", "ok"]
            )
        else:
            df["success"] = True
    if parent_col != "parent_id":
        df["parent_id"] = df[parent_col]

    span_cmdb = df.set_index("span_id")["cmdb_id"].to_dict()
    df["_parent_cmdb"] = df["parent_id"].map(span_cmdb)
    edges = df[df["_parent_cmdb"].notna()].copy()
    if edges.empty:
        df["dsName"] = df["cmdb_id"]
        return df.drop(columns=["_parent_cmdb"], errors="ignore")

    edges["dsName"] = edges["cmdb_id"]
    edges["cmdb_id"] = edges["_parent_cmdb"]
    return edges.drop(columns=["_parent_cmdb"], errors="ignore")


def extract_caller_callee_from_dsname(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Extract caller→callee edges from cmdb_id + dsName (JDBC: docker→db).

    Telecom JDBC spans: cmdb_id = caller (e.g. docker_007), dsName = callee (e.g. db_003).
    Returns DataFrame with columns: caller, callee, span_count, avg_duration_ms, fail_count.
    """
    if "cmdb_id" not in raw_df.columns or "dsName" not in raw_df.columns:
        return pd.DataFrame()
    dur_col = "elapsedTime" if "elapsedTime" in raw_df.columns else "duration"
    if dur_col not in raw_df.columns:
        dur_col = None
    success_col = "success" if "success" in raw_df.columns else None

    df = raw_df.copy()
    df["caller"] = df["cmdb_id"].astype(str).str.strip()
    df["callee"] = df["dsName"].astype(str).str.strip()
    # Keep only rows where dsName is present and different from cmdb_id
    edges = df[(df["callee"] != "") & (df["caller"] != df["callee"])].copy()
    if edges.empty:
        return pd.DataFrame()

    result = edges.groupby(["caller", "callee"]).size().reset_index()
    result.columns = ["caller", "callee", "span_count"]
    if dur_col:
        dur_agg = edges.groupby(["caller", "callee"])[dur_col].mean().round(2).reset_index()
        dur_agg = dur_agg.rename(columns={dur_col: "avg_duration_ms"})
        result = result.merge(dur_agg, on=["caller", "callee"], how="left")
    if success_col:
        edges["_fail"] = ~edges[success_col].astype(str).str.strip().str.lower().isin(["true", "0", "200", "ok"])
        fail_agg = edges.groupby(["caller", "callee"])["_fail"].sum().reset_index()
        fail_agg = fail_agg.rename(columns={"_fail": "fail_count"})
        result = result.merge(fail_agg, on=["caller", "callee"], how="left")
    return result


def extract_caller_callee_all(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Merge caller-callee edges from id/pid (RPC, docker-docker) and cmdb_id+dsName (JDBC, docker-db).

    Returns combined DataFrame with columns: caller, callee, span_count, avg_duration_ms, fail_count.
    """
    id_pid = extract_caller_callee_from_id_pid(raw_df)
    dsname = extract_caller_callee_from_dsname(raw_df)
    if id_pid.empty and dsname.empty:
        return pd.DataFrame()
    if id_pid.empty:
        return dsname
    if dsname.empty:
        return id_pid
    # Concatenate and aggregate (same caller-callee typically not in both: id/pid=cmdb_id→cmdb_id, dsName=cmdb_id→dsName)
    combined = pd.concat([id_pid, dsname], ignore_index=True)
    agg_kw = {"span_count": ("span_count", "sum")}
    if "avg_duration_ms" in combined.columns:
        agg_kw["avg_duration_ms"] = ("avg_duration_ms", "mean")
    if "fail_count" in combined.columns:
        agg_kw["fail_count"] = ("fail_count", "sum")
    result = combined.groupby(["caller", "callee"], as_index=False).agg(**agg_kw)
    if "fail_count" in result.columns:
        result["fail_count"] = result["fail_count"].fillna(0).astype(int)
    return result


def extract_caller_callee_from_id_pid(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Extract caller→callee edges from id/pid parent-child mapping.

    Join: child.pid = parent.id → parent.cmdb_id = caller, child.cmdb_id = callee.
    Returns DataFrame with columns: caller, callee, span_count, avg_duration_ms, fail_count.
    """
    id_col = "id" if "id" in raw_df.columns else ("span_id" if "span_id" in raw_df.columns else None)
    pid_col = "pid" if "pid" in raw_df.columns else ("parent_id" if "parent_id" in raw_df.columns else ("parent_span" if "parent_span" in raw_df.columns else None))
    if id_col is None or pid_col is None or "cmdb_id" not in raw_df.columns:
        return pd.DataFrame()
    dur_col = "elapsedTime" if "elapsedTime" in raw_df.columns else "duration"
    if dur_col not in raw_df.columns:
        dur_col = None
    success_col = "success" if "success" in raw_df.columns else None

    df = raw_df.copy()
    df["_id"] = df[id_col].astype(str)
    df["_pid"] = df[pid_col].fillna("").astype(str)
    # Drop root spans (no parent)
    children = df[df["_pid"].str.strip() != ""].copy()
    if children.empty:
        return pd.DataFrame()

    # Build parent id -> cmdb_id map
    parent_map = df.set_index("_id")["cmdb_id"].to_dict()
    children["caller"] = children["_pid"].map(parent_map)
    children["callee"] = children["cmdb_id"]
    # Keep only edges where we found parent
    edges = children[children["caller"].notna()].copy()
    if edges.empty:
        return pd.DataFrame()
    # Exclude self-loops (caller == callee)
    edges = edges[edges["caller"] != edges["callee"]].copy()
    if edges.empty:
        return pd.DataFrame()

    # Aggregate by caller, callee
    result = edges.groupby(["caller", "callee"], as_index=False).agg(
        span_count=("_id", "count"),
    )
    if dur_col:
        dur_agg = edges.groupby(["caller", "callee"])[dur_col].mean().round(2).reset_index()
        dur_agg = dur_agg.rename(columns={dur_col: "avg_duration_ms"})
        result = result.merge(dur_agg, on=["caller", "callee"], how="left")
    if success_col:
        edges["_fail"] = ~edges[success_col].astype(str).str.strip().str.lower().isin(["true", "0", "200", "ok"])
        fail_agg = edges.groupby(["caller", "callee"])["_fail"].sum().reset_index()
        fail_agg = fail_agg.rename(columns={"_fail": "fail_count"})
        result = result.merge(fail_agg, on=["caller", "callee"], how="left")
    return result


def _compute_network_gap(raw_df: pd.DataFrame) -> dict:
    """Compute parent-child duration gap per cmdb_id."""
    # Telecom: id=span_id, pid=parent_id, elapsedTime
    # Bank/Market: span_id, parent_id/parent_span, duration
    span_col = "span_id" if "span_id" in raw_df.columns else ("id" if "id" in raw_df.columns else None)
    parent_col = next(
        (c for c in ["parent_id", "parent_span", "pid"] if c in raw_df.columns),
        None,
    )
    if span_col is None or parent_col is None:
        return {}
    dur_col = "duration" if "duration" in raw_df.columns else "elapsedTime"
    if dur_col not in raw_df.columns or "cmdb_id" not in raw_df.columns:
        return {}

    df = raw_df.copy()
    df["_span_id"] = df[span_col].astype(str)
    df["_parent_id"] = df[parent_col].astype(str)

    span_dur = df.set_index("_span_id")[dur_col].astype(float).to_dict()
    span_cmdb = df.set_index("_span_id")["cmdb_id"].to_dict()
    parent_ids = set(span_dur)
    children = df[df["_parent_id"].isin(parent_ids)]
    if children.empty:
        return {}

    child_sum = children.groupby("_parent_id")[dur_col].sum()
    gaps = []
    for span_id, child_total in child_sum.items():
        parent_dur = span_dur.get(span_id, 0)
        parent_cmdb = span_cmdb.get(span_id)
        if parent_cmdb is None or parent_dur <= 0:
            continue
        gap = max(0.0, float(parent_dur) - float(child_total))
        gaps.append({"cmdb_id": parent_cmdb, "gap": gap, "parent_dur": float(parent_dur)})

    if not gaps:
        return {}
    gap_df = pd.DataFrame(gaps)
    agg = gap_df.groupby("cmdb_id").agg(
        avg_gap=("gap", "mean"),
        avg_parent_dur=("parent_dur", "mean"),
        span_count=("gap", "count"),
    ).reset_index()
    agg["gap_ratio"] = agg["avg_gap"] / (agg["avg_parent_dur"] + 1e-6)
    return agg.to_dict("records")


def _filter_to_fault_window(df: pd.DataFrame, fault_time_str: str | None, window_minutes: int) -> pd.DataFrame:
    """Filter df to rows within ±window_minutes of fault_time. fault_time: 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    if not fault_time_str or window_minutes <= 0:
        return df
    try:
        fault_dt = datetime.strptime(fault_time_str.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        fault_ts = fault_dt.timestamp()
    except ValueError:
        return df
    ts_col = "startTime" if "startTime" in df.columns else "timestamp"
    if ts_col not in df.columns:
        return df
    ts = pd.to_numeric(df[ts_col], errors="coerce")
    if ts.median() > 1e12:
        ts = ts / 1000.0
    half = window_minutes * 60
    mask = (ts >= fault_ts - half) & (ts <= fault_ts + half)
    return df[mask].copy()


def load_and_summarize(
    path: Path,
    label: str,
    nrows: int | None = None,
    focus_components: list[str] | None = None,
    fault_time: str | None = None,
    window_minutes: int = 30,
) -> dict:
    """Load trace CSV and compute summary stats. If focus_components given, filter to those (caller or callee)."""
    df = pd.read_csv(path, low_memory=False, nrows=nrows)
    if df.empty:
        return {"label": label, "path": str(path), "error": "Empty file"}

    # Filter to fault window if specified
    if fault_time:
        df = _filter_to_fault_window(df, fault_time, window_minutes)
        if df.empty:
            return {"label": label, "path": str(path), "error": f"No rows in fault window ±{window_minutes}min of {fault_time}"}

    # Raw df for network_gap (needs span_id, parent_*, duration)
    raw_df = df.copy()
    if "elapsedTime" not in raw_df.columns and "duration" in raw_df.columns:
        raw_df["elapsedTime"] = raw_df["duration"]

    df = _normalize_trace_schema(df)
    if df.empty:
        return {"label": label, "path": str(path), "error": "Empty after normalize"}

    # Parse time
    ts_col = "startTime" if "startTime" in df.columns else "timestamp"
    ts = df[ts_col].copy()
    if pd.to_numeric(ts, errors="coerce").median() > 1e12:
        ts = pd.to_numeric(ts, errors="coerce") / 1000.0
    df["datetime"] = pd.to_datetime(ts, unit="s", utc=True, errors="coerce")
    df = df[df["datetime"].notna()].copy()

    # Success
    if "success" in df.columns:
        if df["success"].dtype == object:
            df["success_bool"] = df["success"].astype(str).str.strip().str.lower().isin(
                ["true", "0", "200", "ok"]
            )
        else:
            df["success_bool"] = df["success"].astype(bool)
    else:
        df["success_bool"] = True

    # Filter to focus components (caller cmdb_id or callee dsName)
    if focus_components:
        comp_set = set(c.strip() for c in focus_components if c.strip())
        if comp_set:
            mask = df["cmdb_id"].astype(str).str.strip().isin(comp_set) | df["dsName"].astype(str).str.strip().isin(comp_set)
            df = df[mask].copy()

    # Summary
    total_spans = len(df)
    fail_count = (~df["success_bool"]).sum()
    fail_rate = fail_count / total_spans if total_spans > 0 else 0.0

    dur_col = "elapsedTime" if "elapsedTime" in df.columns else "duration"
    if dur_col in df.columns:
        avg_duration_ms = df[dur_col].astype(float).mean()
        p50_duration_ms = df[dur_col].astype(float).median()
        p95_duration_ms = df[dur_col].astype(float).quantile(0.95)
    else:
        avg_duration_ms = p50_duration_ms = p95_duration_ms = None

    # Network gap (from raw spans with parent-child); filter raw_df if focus_components
    _raw = raw_df.copy()
    if focus_components:
        comp_set = set(c.strip() for c in focus_components if c.strip())
        if comp_set and "cmdb_id" in _raw.columns:
            mask = _raw["cmdb_id"].astype(str).str.strip().isin(comp_set)
            _raw = _raw[mask]
    gap_records = _compute_network_gap(_raw)
    if gap_records:
        gap_df = pd.DataFrame(gap_records)
        avg_gap_ratio = gap_df["gap_ratio"].mean()
        max_gap_ratio = gap_df["gap_ratio"].max()
        total_gap_spans = int(gap_df["span_count"].sum())
    else:
        avg_gap_ratio = max_gap_ratio = total_gap_spans = None

    # Per-component volume
    comp_vol = df.groupby("cmdb_id").size()
    unique_components = len(comp_vol)

    # Per-component in/out volume from id-pid (RPC) + cmdb_id-dsName (JDBC docker→db)
    comp_stats = {}
    edges_df = extract_caller_callee_all(raw_df)
    if focus_components:
        comp_set = set(c.strip() for c in focus_components if c.strip())
        if not edges_df.empty:
            for c in comp_set:
                inc_edges = edges_df[edges_df["callee"].astype(str).str.strip() == c]
                out_edges = edges_df[edges_df["caller"].astype(str).str.strip() == c]
                inc_n = int(inc_edges["span_count"].sum()) if not inc_edges.empty else 0
                out_n = int(out_edges["span_count"].sum()) if not out_edges.empty else 0
                inc_fail = int(inc_edges["fail_count"].sum()) if "fail_count" in inc_edges.columns else 0
                out_fail = int(out_edges["fail_count"].sum()) if "fail_count" in out_edges.columns else 0
                inc_fr = round(100 * inc_fail / inc_n, 2) if inc_n > 0 else 0.0
                out_fr = round(100 * out_fail / out_n, 2) if out_n > 0 else 0.0
                out_in_ratio = round(out_n / inc_n, 3) if inc_n > 0 else None
                dur_inc = inc_edges["avg_duration_ms"].mean() if "avg_duration_ms" in inc_edges.columns and not inc_edges.empty else None
                dur_out = out_edges["avg_duration_ms"].mean() if "avg_duration_ms" in out_edges.columns and not out_edges.empty else None
                dur_avg = None
                if dur_inc is not None and dur_out is not None:
                    dur_avg = round((dur_inc * inc_n + dur_out * out_n) / (inc_n + out_n), 2) if (inc_n + out_n) > 0 else round((dur_inc + dur_out) / 2, 2)
                elif dur_inc is not None and dur_out is None:
                    dur_avg = round(dur_inc, 2) if inc_n > 0 else None
                elif dur_out is not None:
                    dur_avg = round(dur_out, 2) if out_n > 0 else None
                comp_stats[c] = {
                    "spans": inc_n + out_n,
                    "incoming": inc_n,
                    "outgoing": out_n,
                    "out_in_ratio": out_in_ratio,
                    "incoming_fail_pct": inc_fr,
                    "outgoing_fail_pct": out_fr,
                    "fail_rate_pct": round(100 * (inc_fail + out_fail) / (inc_n + out_n), 2) if (inc_n + out_n) > 0 else 0,
                    "avg_duration_ms": dur_avg,
                }
        else:
            # Fallback: no id/pid, use flat cmdb_id/dsName (incoming=0 for Telecom containers)
            for c in comp_set:
                inc = df[df["dsName"].astype(str).str.strip() == c]
                out = df[df["cmdb_id"].astype(str).str.strip() == c]
                inc_n, out_n = len(inc), len(out)
                inc_fail = (~inc["success_bool"]).sum() if not inc.empty else 0
                out_fail = (~out["success_bool"]).sum() if not out.empty else 0
                comp_stats[c] = {
                    "spans": inc_n + out_n,
                    "incoming": inc_n,
                    "outgoing": out_n,
                    "out_in_ratio": round(out_n / inc_n, 3) if inc_n > 0 else None,
                    "incoming_fail_pct": round(100 * inc_fail / inc_n, 2) if inc_n > 0 else 0.0,
                    "outgoing_fail_pct": round(100 * out_fail / out_n, 2) if out_n > 0 else 0.0,
                    "fail_rate_pct": round(100 * (inc_fail + out_fail) / (inc_n + out_n), 2) if (inc_n + out_n) > 0 else 0,
                    "avg_duration_ms": round(pd.concat([inc, out])[dur_col].astype(float).mean(), 2) if (inc_n + out_n) > 0 and dur_col in df.columns else None,
                }

    return {
        "label": label,
        "path": str(path),
        "focus_components": focus_components,
        "comp_stats": comp_stats,
        "total_spans": total_spans,
        "unique_components": unique_components,
        "fail_count": int(fail_count),
        "fail_rate_pct": round(fail_rate * 100, 2),
        "avg_duration_ms": round(avg_duration_ms, 2) if avg_duration_ms is not None else None,
        "p50_duration_ms": round(p50_duration_ms, 2) if p50_duration_ms is not None else None,
        "p95_duration_ms": round(p95_duration_ms, 2) if p95_duration_ms is not None else None,
        "avg_gap_ratio": round(avg_gap_ratio, 4) if avg_gap_ratio is not None else None,
        "max_gap_ratio": round(max_gap_ratio, 4) if max_gap_ratio is not None else None,
        "gap_span_count": total_gap_spans,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Compare trace span: network delay vs network loss"
    )
    parser.add_argument(
        "--delay-path",
        type=Path,
        default=Path("prefiltered_telemetry/task_4-19/static-telecom/traces/trace_span.csv"),
        help="Trace CSV for network delay (task_4-19)",
    )
    parser.add_argument(
        "--loss-path",
        type=Path,
        default=Path("prefiltered_telemetry/task_2-20/static-telecom/traces/trace_span.csv"),
        help="Trace CSV for network loss (task_2-20)",
    )
    parser.add_argument(
        "--nrows",
        type=int,
        default=None,
        help="Limit rows per file (for testing); default=all",
    )
    parser.add_argument(
        "--delay-components",
        type=str,
        default="docker_003,docker_007",
        help="Comma-separated faulty components for delay task (task_4-19)",
    )
    parser.add_argument(
        "--loss-components",
        type=str,
        default="docker_001,docker_005",
        help="Comma-separated faulty components for loss task (task_2-20)",
    )
    parser.add_argument(
        "--extract-edges",
        type=Path,
        default=None,
        metavar="TRACE_CSV",
        help="Extract caller-callee edges from id/pid and save; skip comparison",
    )
    parser.add_argument(
        "--edges-output",
        type=Path,
        default=None,
        help="Output path for --extract-edges (default: stdout)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Print extraction verification: edge counts, sample caller-callee, os→docker",
    )
    parser.add_argument(
        "--delay-fault-time",
        type=str,
        default="2020-05-24 17:47:00",
        help="Fault time for delay task (YYYY-MM-DD HH:MM:SS UTC). Filter trace to ±window around this.",
    )
    parser.add_argument(
        "--loss-fault-time",
        type=str,
        default="2020-05-24 19:47:00",
        help="Fault time for loss task (YYYY-MM-DD HH:MM:SS UTC). Filter trace to ±window around this.",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=30,
        help="Minutes before/after fault time to include (default: 30)",
    )
    args = parser.parse_args()

    # --extract-edges mode: extract caller-callee from id/pid and exit
    if args.extract_edges is not None:
        path = args.extract_edges.resolve()
        if not path.exists():
            print(f"[ERROR] Trace file not found: {path}")
            return 1
        df = pd.read_csv(path, low_memory=False, nrows=args.nrows)
        edges = extract_caller_callee_all(df)
        if edges.empty:
            print("[WARN] No caller-callee edges extracted (need id/pid or cmdb_id+dsName)")
            return 0
        edges = edges.sort_values(["caller", "callee"])
        if args.edges_output:
            edges.to_csv(args.edges_output, index=False)
            print(f"Wrote {len(edges)} edges to {args.edges_output}")
        else:
            print(edges.to_string(index=False))
        return 0

    delay_comps = [c.strip() for c in args.delay_components.split(",") if c.strip()]
    loss_comps = [c.strip() for c in args.loss_components.split(",") if c.strip()]

    delay_path = args.delay_path.resolve()
    loss_path = args.loss_path.resolve()

    if not delay_path.exists():
        print(f"[ERROR] Delay trace not found: {delay_path}")
        return 1
    if not loss_path.exists():
        print(f"[ERROR] Loss trace not found: {loss_path}")
        return 1

    delay_stats = load_and_summarize(
        delay_path,
        "network delay (task_4-19)",
        args.nrows,
        focus_components=delay_comps,
        fault_time=args.delay_fault_time,
        window_minutes=args.window_minutes,
    )
    loss_stats = load_and_summarize(
        loss_path,
        "network loss (task_2-20)",
        args.nrows,
        focus_components=loss_comps,
        fault_time=args.loss_fault_time,
        window_minutes=args.window_minutes,
    )

    if "error" in delay_stats:
        print(f"[ERROR] Delay: {delay_stats['error']}")
        return 1
    if "error" in loss_stats:
        print(f"[ERROR] Loss: {loss_stats['error']}")
        return 1

    # Verification: re-extract edges and show sanity check
    if args.verify:
        for label, path, comps in [
            ("Delay", delay_path, delay_comps),
            ("Loss", loss_path, loss_comps),
        ]:
            df = pd.read_csv(path, low_memory=False, nrows=args.nrows)
            edges = extract_caller_callee_all(df)
            print(f"\n  [VERIFY] {label} ({path.name}):")
            print(f"    Raw rows: {len(df)}, Edges extracted: {len(edges)}")
            if not edges.empty:
                total_spans = int(edges["span_count"].sum())
                print(f"    Total span_count in edges: {total_spans}")
                print(f"    Unique callers: {edges['caller'].nunique()}, callees: {edges['callee'].nunique()}")
                os_docker = edges[edges["caller"].str.startswith("os_") & edges["callee"].str.startswith("docker_")]
                if not os_docker.empty:
                    print(f"    os→docker edges: {len(os_docker)}, span_count={int(os_docker['span_count'].sum())}")
                    print(f"    Top os→docker: {os_docker.nlargest(3, 'span_count')[['caller','callee','span_count']].to_dict('records')}")
                docker_docker = edges[
                    edges["caller"].str.startswith("docker_")
                    & edges["callee"].str.startswith("docker_")
                    & (edges["caller"] != edges["callee"])
                ]
                if not docker_docker.empty:
                    print(f"    docker→docker (diff): {len(docker_docker)}, span_count={int(docker_docker['span_count'].sum())}")
                    print(f"    Top docker→docker: {docker_docker.nlargest(3, 'span_count')[['caller','callee','span_count']].to_dict('records')}")
                else:
                    print(f"    docker→docker (diff): 0 (none in trace)")
                docker_db = edges[
                    edges["caller"].str.startswith("docker_") & edges["callee"].str.startswith("db_")
                ]
                if not docker_db.empty:
                    print(f"    docker→db (cmdb_id-dsName): {len(docker_db)}, span_count={int(docker_db['span_count'].sum())}")
                    print(f"    Top docker→db: {docker_db.nlargest(3, 'span_count')[['caller','callee','span_count']].to_dict('records')}")
                top = edges.nlargest(5, "span_count")[["caller", "callee", "span_count"]]
                print(f"    Top 5 edges: {top.to_dict('records')}")
                for c in comps:
                    inc = edges[edges["callee"] == c]["span_count"].sum()
                    out = edges[edges["caller"] == c]["span_count"].sum()
                    print(f"    {c}: incoming={int(inc)}, outgoing={int(out)}")
            else:
                print(f"    [WARN] No edges (need id/pid or cmdb_id+dsName)")
        print()

    # Print comparison
    print("\n" + "=" * 70)
    print("  Trace Span Comparison: Network Delay vs Network Loss")
    print("  (focus: faulty components only)")
    print("=" * 70)
    print(f"\n  Delay (task_4-19): {delay_path}")
    print(f"  Focus components: {delay_comps}")
    print(f"  Fault window: ±{args.window_minutes}min of {args.delay_fault_time}")
    print(f"\n  Loss (task_2-20):  {loss_path}")
    print(f"  Focus components: {loss_comps}")
    print(f"  Fault window: ±{args.window_minutes}min of {args.loss_fault_time}")
    print()

    metrics = [
        ("total_spans", "Trace volume (span count)", ""),
        ("unique_components", "Unique components", ""),
        ("fail_count", "Fail count (success=false)", ""),
        ("fail_rate_pct", "Fail rate (%)", "%"),
        ("avg_duration_ms", "Avg duration (ms)", "ms"),
        ("p50_duration_ms", "P50 duration (ms)", "ms"),
        ("p95_duration_ms", "P95 duration (ms)", "ms"),
        ("avg_gap_ratio", "Avg network gap ratio", ""),
        ("max_gap_ratio", "Max network gap ratio", ""),
        ("gap_span_count", "Spans with parent-child (gap)", ""),
    ]

    print(f"  {'Metric':<35}  {'Delay':>12}  {'Loss':>12}  {'Diff':>10}")
    print("  " + "-" * 72)
    for key, label, unit in metrics:
        d_val = delay_stats.get(key)
        l_val = loss_stats.get(key)
        if d_val is None and l_val is None:
            continue
        d_str = str(d_val) if d_val is not None else "N/A"
        l_str = str(l_val) if l_val is not None else "N/A"
        if isinstance(d_val, (int, float)) and isinstance(l_val, (int, float)):
            diff = l_val - d_val
            diff_str = f"{diff:+.0f}" if isinstance(d_val, int) else f"{diff:+.2f}"
        else:
            diff_str = "-"
        print(f"  {label:<35}  {d_str:>12}  {l_str:>12}  {diff_str:>10}")

    # Per-component breakdown (in/out from id-pid + cmdb_id-dsName)
    # Loss signal: out/in < 1 (outgoing dropped vs incoming)
    print("\n  --- In/Out volume (id/pid RPC + cmdb_id-dsName JDBC) ---")
    print("  Incoming = others→this, Outgoing = this→others.")
    print("  Loss signal: out/in < 1 (패킷 손실 시 나가는 것 감소)")
    all_comps = sorted(set(delay_comps) | set(loss_comps))
    print(f"\n  {'Component':<12}  {'Delay in':>10}  {'Delay out':>10}  {'Delay out/in':>12}  {'Loss in':>10}  {'Loss out':>10}  {'Loss out/in':>12}")
    print("  " + "-" * 90)
    for c in all_comps:
        d_cs = delay_stats.get("comp_stats", {}).get(c, {})
        l_cs = loss_stats.get("comp_stats", {}).get(c, {})
        d_in = d_cs.get("incoming", 0)
        d_out = d_cs.get("outgoing", 0)
        d_ratio = d_cs.get("out_in_ratio")
        l_in = l_cs.get("incoming", 0)
        l_out = l_cs.get("outgoing", 0)
        l_ratio = l_cs.get("out_in_ratio")
        d_ratio_str = f"{d_ratio:.2f}" if d_ratio is not None else "N/A"
        l_ratio_str = f"{l_ratio:.2f}" if l_ratio is not None else "N/A"
        print(f"  {c:<12}  {d_in:>10}  {d_out:>10}  {d_ratio_str:>12}  {l_in:>10}  {l_out:>10}  {l_ratio_str:>12}")

    print("\n" + "=" * 70)
    print("  Interpretation")
    print("=" * 70)
    d_vol = delay_stats.get("total_spans", 0)
    l_vol = loss_stats.get("total_spans", 0)
    d_fail = delay_stats.get("fail_rate_pct", 0)
    l_fail = loss_stats.get("fail_rate_pct", 0)
    d_dur = delay_stats.get("avg_duration_ms")
    l_dur = loss_stats.get("avg_duration_ms")

    print(f"\n  • Trace volume:  Delay={d_vol:,} spans  |  Loss={l_vol:,} spans")
    if d_vol > 0:
        vol_ratio = l_vol / d_vol
        if vol_ratio < 0.5:
            print(f"    → Loss has {100*(1-vol_ratio):.0f}% FEWER spans (volume drops with packet loss)")
        elif vol_ratio > 1.5:
            print(f"    → Loss has more spans (unexpected)")
        else:
            print(f"    → Similar volume (ratio={vol_ratio:.2f})")
    print(f"\n  • Fail rate:     Delay={d_fail}%  |  Loss={l_fail}%")
    if l_fail > d_fail * 1.5:
        print(f"    → Loss has HIGHER fail rate (failed requests produce error spans)")
    elif l_fail < d_fail * 0.5:
        print(f"    → Loss has LOWER fail rate (failed requests may not create spans)")
    print(f"\n  • Avg duration:  Delay={d_dur}ms  |  Loss={l_dur}ms")
    if d_dur and l_dur:
        if l_dur > d_dur * 1.2:
            print(f"    → Loss has longer duration (timeouts?)")
        elif l_dur < d_dur * 0.8:
            print(f"    → Loss has shorter duration (early failures?)")
    print(f"\n  • In/Out (out/in): in대비 out 개수. Loss 시 out 감소 → out/in < 1")
    print(f"    → 같은 컴포넌트의 Delay vs Loss out/in 비교로 loss 신호 확인")
    print(f"\n  • If difference is small: run with --verify to check extraction.")
    print(f"    → Delay/loss tasks use different time windows & components.")
    print(f"    → Telecom faults are at node (os_*); docker may be propagation.")
    print()
    return 0


if __name__ == "__main__":
    exit(main())
