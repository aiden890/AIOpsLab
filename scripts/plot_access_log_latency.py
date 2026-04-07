"""Plot per-minute access-log latency for a selected client/component hint.

Examples:
    python scripts/plot_access_log_latency.py --dataset openrca_bank --task task_1-16 --client IG02
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_log_csv(dataset: str, task: str) -> Path:
    ns = "static-bank" if dataset == "openrca_bank" else "static-telecom"
    return REPO_ROOT / "prefiltered_telemetry" / dataset / task / ns / "logs" / "log_service.csv"


def _extract_latency_seconds(series: pd.Series) -> pd.Series:
    # Access logs end with a latency-like float in seconds.
    return pd.to_numeric(series.astype(str).str.extract(r"\s([0-9]+(?:\.[0-9]+)?)\s*$")[0], errors="coerce")


def build_access_latency_frame(log_path: Path, client: str) -> pd.DataFrame:
    df = pd.read_csv(log_path, usecols=["timestamp", "cmdb_id", "log_name", "value"], low_memory=False)
    df = df[df["log_name"].isin(["apache_access_log", "localhost_access_log"])].copy()
    df["value"] = df["value"].astype(str)
    df = df[df["value"].str.contains(fr"\b{re.escape(client)}\b", regex=True)].copy()
    if df.empty:
        return pd.DataFrame()
    df["latency_s"] = _extract_latency_seconds(df["value"])
    df = df[df["latency_s"].notna()].copy()
    if df.empty:
        return pd.DataFrame()
    ts = pd.to_numeric(df["timestamp"], errors="coerce")
    df["bucket"] = pd.to_datetime(ts, unit="s", utc=True, errors="coerce").dt.floor("1min")
    return (
        df.groupby(["cmdb_id", "log_name", "bucket"], as_index=False)["latency_s"]
        .agg(
            count="size",
            p50="median",
            p90=lambda s: s.quantile(0.90),
            p95=lambda s: s.quantile(0.95),
            mean="mean",
            max="max",
        )
        .sort_values(["cmdb_id", "log_name", "bucket"])
    )


def plot_access_latency(log_path: Path, output_path: Path, *, client: str, top_k: int = 6) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frame = build_access_latency_frame(log_path, client)
    if frame.empty:
        raise ValueError(f"No parsed access-log latency rows for client {client}")

    ranked = []
    for (cmdb_id, log_name), g in frame.groupby(["cmdb_id", "log_name"], sort=True):
        g = g.sort_values("bucket").reset_index(drop=True)
        base = g.head(min(10, len(g)))
        base_p95 = float(base["p95"].median()) if not base.empty else 0.0
        peak = float(g["p95"].max())
        ranked.append({
            "cmdb_id": cmdb_id,
            "log_name": log_name,
            "base_p95": base_p95,
            "peak_p95": peak,
            "ratio": peak / max(base_p95, 0.001),
        })
    top = (
        pd.DataFrame(ranked)
        .sort_values(["ratio", "peak_p95"], ascending=[False, False])
        .head(top_k)
    )

    fig, axes = plt.subplots(len(top), 1, figsize=(14, 2.6 * len(top)), sharex=True)
    if len(top) == 1:
        axes = [axes]

    for ax, (_, row) in zip(axes, top.iterrows()):
        g = frame[
            (frame["cmdb_id"] == row["cmdb_id"])
            & (frame["log_name"] == row["log_name"])
        ].sort_values("bucket")
        base = g.head(min(10, len(g)))
        base_p95 = float(base["p95"].median()) if not base.empty else 0.0
        peak_row = g.loc[g["p95"].idxmax()]
        ax.plot(g["bucket"], g["p95"], color="#d62728", lw=1.8, label="p95")
        ax.plot(g["bucket"], g["p90"], color="#ff7f0e", lw=1.2, label="p90")
        ax.plot(g["bucket"], g["p50"], color="#7f7f7f", lw=1.0, label="p50")
        ax.axhline(base_p95, color="#1f77b4", ls="--", lw=1.0, label="baseline p95")
        ax.set_ylabel("sec")
        ax.grid(True, axis="y", alpha=0.25)
        ax.set_title(
            f"{row['cmdb_id']} / {row['log_name']} | base p95={base_p95:.3f}s | "
            f"peak p95={float(peak_row['p95']):.3f}s @ {peak_row['bucket'].strftime('%H:%M')}",
            fontsize=10,
            loc="left",
        )
        ax.legend(loc="upper right", fontsize=8)

    fig.suptitle(f"Per-minute access-log latency for client {client}", fontsize=14)
    axes[-1].set_xlabel("Time (UTC)")
    fig.autofmt_xdate()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="openrca_bank")
    parser.add_argument("--task", default="task_1-16")
    parser.add_argument("--client", default="IG02")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log_path = resolve_log_csv(args.dataset, args.task)
    output = args.output or (REPO_ROOT / "tmp" / f"access_latency_{args.dataset}_{args.task}_{args.client}.png")
    out = plot_access_latency(log_path, output, client=args.client, top_k=args.top_k)
    print(f"Saved plot to {out}")


if __name__ == "__main__":
    main()
