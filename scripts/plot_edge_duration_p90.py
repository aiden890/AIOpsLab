"""Plot per-minute edge duration p90 changes from a trace_span.csv file.

Examples:
    python scripts/plot_edge_duration_p90.py \
        --input prefiltered_telemetry/openrca_bank/task_1-16/static-bank/traces/trace_span.csv

    python scripts/plot_edge_duration_p90.py \
        --dataset openrca_bank --task task_1-16
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd


_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from render_trace_anomalous_paths_from_csv import resolve_trace_csv_path


DEFAULT_DATASET = "openrca_bank"
DEFAULT_TASK = "task_1-16"


def _build_edge_minute_frame(trace_path: Path) -> pd.DataFrame:
    df = pd.read_csv(trace_path, low_memory=False)
    if df.empty:
        return pd.DataFrame()

    work = df.copy()
    if "span_id" not in work.columns or "parent_id" not in work.columns or "cmdb_id" not in work.columns:
        return pd.DataFrame()

    work["_id"] = work["span_id"].astype(str)
    work["_pid"] = work["parent_id"].fillna("").astype(str)
    parent_map = work.set_index("_id")["cmdb_id"].to_dict()

    children = work[work["_pid"].str.strip() != ""].copy()
    if children.empty:
        return pd.DataFrame()

    children["caller"] = children["_pid"].map(parent_map)
    children["callee"] = children["cmdb_id"].astype(str)
    edges = children[children["caller"].notna() & (children["caller"] != children["callee"])].copy()
    if edges.empty:
        return pd.DataFrame()

    ts = pd.to_numeric(edges["timestamp"], errors="coerce")
    if ts.median() > 1e12:
        ts = ts / 1000.0
    edges["bucket"] = pd.to_datetime(ts, unit="s", utc=True, errors="coerce").dt.floor("1min")
    edges = edges[edges["bucket"].notna()].copy()
    if edges.empty:
        return pd.DataFrame()

    return (
        edges.groupby(["caller", "callee", "bucket"], as_index=False)["duration"]
        .agg(
            count="size",
            p50="median",
            p90=lambda s: s.quantile(0.90),
            p95=lambda s: s.quantile(0.95),
            mean="mean",
            max="max",
        )
        .sort_values(["caller", "callee", "bucket"])
    )


def _select_top_edges(edge_df: pd.DataFrame, top_k: int) -> list[tuple[str, str]]:
    rows = []
    for (caller, callee), g in edge_df.groupby(["caller", "callee"], sort=True):
        g = g.sort_values("bucket").reset_index(drop=True)
        base = g.head(min(10, len(g)))
        base_p90 = float(base["p90"].median()) if not base.empty else 0.0
        peak_p90 = float(g["p90"].max())
        rows.append({
            "caller": caller,
            "callee": callee,
            "edge": f"{caller}->{callee}",
            "count_sum": int(g["count"].sum()),
            "base_p90": base_p90,
            "peak_p90": peak_p90,
            "p90_ratio": peak_p90 / max(base_p90, 1.0),
        })
    ranked = pd.DataFrame(rows)
    if ranked.empty:
        return []
    ranked = ranked.sort_values(["p90_ratio", "peak_p90", "count_sum"], ascending=[False, False, False])
    return [(r["caller"], r["callee"]) for _, r in ranked.head(top_k).iterrows()]


def plot_edge_duration_p90(
    trace_path: Path,
    output_path: Path,
    *,
    top_k: int = 6,
    task_label: str | None = None,
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    edge_df = _build_edge_minute_frame(trace_path)
    if edge_df.empty:
        raise ValueError(f"No edge-minute rows from {trace_path}")

    selected = _select_top_edges(edge_df, top_k=top_k)
    if not selected:
        raise ValueError(f"No edges to plot from {trace_path}")

    fig, axes = plt.subplots(len(selected), 1, figsize=(14, 2.5 * len(selected)), sharex=True)
    if len(selected) == 1:
        axes = [axes]

    for ax, (caller, callee) in zip(axes, selected):
        g = edge_df[(edge_df["caller"] == caller) & (edge_df["callee"] == callee)].sort_values("bucket")
        base = g.head(min(10, len(g)))
        base_p90 = float(base["p90"].median()) if not base.empty else 0.0
        peak_idx = g["p90"].idxmax()
        peak_row = g.loc[peak_idx]

        ax.plot(g["bucket"], g["p90"], color="#d62728", lw=1.8, label="p90")
        ax.plot(g["bucket"], g["p50"], color="#7f7f7f", lw=1.0, alpha=0.8, label="p50")
        ax.axhline(base_p90, color="#1f77b4", ls="--", lw=1.0, alpha=0.8, label="baseline p90")
        ax.scatter([peak_row["bucket"]], [peak_row["p90"]], color="#d62728", s=24, zorder=3)
        ax.set_ylabel("ms")
        ax.set_title(
            f"{caller} -> {callee} | base p90={base_p90:.1f} | "
            f"peak p90={float(peak_row['p90']):.1f} @ {peak_row['bucket'].strftime('%H:%M')}",
            fontsize=10,
            loc="left",
        )
        ax.grid(True, axis="y", alpha=0.25)
        ax.legend(loc="upper right", fontsize=8)

    label = task_label or trace_path.parent.parent.parent.name
    fig.suptitle(f"Per-minute edge duration changes (Top-{len(selected)} by p90 ratio) | {label}", fontsize=14)
    axes[-1].set_xlabel("Time (UTC)")
    fig.autofmt_xdate()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=None, help="trace_span.csv path")
    parser.add_argument("--dataset", type=str, default=DEFAULT_DATASET)
    parser.add_argument("--task", type=str, default=DEFAULT_TASK)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output PNG path; defaults to tmp/edge_duration_p90_<dataset>_<task>.png",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trace_path = resolve_trace_csv_path(args.input, dataset=args.dataset, task=args.task)
    output_path = (
        args.output
        if args.output is not None
        else _REPO_ROOT / "tmp" / f"edge_duration_p90_{args.dataset}_{args.task}.png"
    )
    out = plot_edge_duration_p90(trace_path, output_path, top_k=args.top_k, task_label=f"{args.dataset}/{args.task}")
    print(f"Saved plot to {out}")


if __name__ == "__main__":
    main()
