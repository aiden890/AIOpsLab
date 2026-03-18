#!/usr/bin/env python3
"""Run only Stage 4 global judge on a constructed RCA tree.

Examples:
  python scripts/test_stage4_global_judge.py \
    --dataset-key openrca_telecom \
    --query-start "2020-05-21 17:33:00" \
    --query-end "2020-05-21 17:53:00"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from clients.openrca_rca.api_router import load_config
from clients.tree_traversal.dataset_profile import build_profile
from clients.tree_traversal.rca_search_tree import SearchTree
from clients.tree_traversal.staged_rca_pipeline import StagedRCAPipeline


def _parse_time_arg(value: str | None):
    if not value:
        return None
    s = value.strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return s


def _default_profile_config(dataset_key: str) -> str | None:
    ds = dataset_key.replace("openrca_", "")
    config_map = {
        "telecom": REPO_ROOT / "aiopslab/service/apps/static_dataset/config/openrca_telecom.json",
        "bank": REPO_ROOT / "aiopslab/service/apps/static_dataset/config/openrca_bank.json",
        "market_cloudbed1": REPO_ROOT / "aiopslab/service/apps/static_dataset/config/openrca_market_cloudbed1.json",
        "market_cb1": REPO_ROOT / "aiopslab/service/apps/static_dataset/config/openrca_market_cloudbed1.json",
    }
    for key, path in config_map.items():
        if ds.startswith(key) and path.exists():
            return str(path)
    return None


def build_sample_tree() -> SearchTree:
    """Build a small tree that mirrors the current telecom task_5-8 shape."""
    tree = SearchTree()

    loc_err = tree.add_candidate(
        stage="localize",
        component="docker_006",
        reason="Trace Error Rate (%)",
        time_str="2020-05-21 17:49:00",
        kpi="Trace Error Rate (%)",
        evidence="error rate spike on docker_006 around 17:49",
        severity=92.0,
    )
    loc_lat = tree.add_candidate(
        stage="localize",
        component="docker_006",
        reason="Trace Latency (p50)",
        time_str="2020-05-21 17:48:00",
        kpi="Trace Latency (p50)",
        evidence="trace latency increase on docker_006 around 17:48",
        severity=95.0,
    )
    loc_os18 = tree.add_candidate(
        stage="localize",
        component="os_018",
        reason="Sent_queue",
        time_str="2020-05-21 17:51:00",
        kpi="Sent_queue",
        evidence="small queue increase on os_018",
        severity=76.0,
    )
    loc_os22 = tree.add_candidate(
        stage="localize",
        component="os_022",
        reason="Received_queue",
        time_str="2020-05-21 17:46:00",
        kpi="Received_queue",
        evidence="received queue rises but remains low absolute level",
        severity=74.0,
    )

    tree.add_candidate(
        stage="expand",
        component="db_003",
        time_str="2020-05-21 17:47:23",
        parent_id=loc_err,
        relation="call_downstream, shared_resource",
        confidence=0.88,
        evidence=(
            "kpi=Session_pct | value=sustained high | value_is_problematic=true | "
            "value_judgment=DB connection pressure is materially elevated"
        ),
    )
    tree.add_candidate(
        stage="expand",
        component="db_003",
        time_str="2020-05-21 17:47:23",
        parent_id=loc_lat,
        relation="call_downstream, shared_resource",
        confidence=0.88,
        evidence=(
            "kpi=Session_pct | value=sustained high | value_is_problematic=true | "
            "value_judgment=DB connection pressure is materially elevated"
        ),
    )
    tree.add_candidate(
        stage="expand",
        component="docker_006",
        time_str="2020-05-21 17:49:00",
        parent_id=loc_os18,
        relation="deploy_sibling",
        confidence=0.94,
        evidence=(
            "kpi=Trace Error Rate (%) | value=sharp rise | value_is_problematic=true | "
            "localized_hit=2020-05-21 17:48:00 | localized_severity=95 | "
            "clues=service-level latency and errors are stronger than OS queue signals"
        ),
        localized_match=True,
        localized_time="2020-05-21 17:48:00",
        localized_severity=95.0,
    )

    tree.add_trace_edge("docker_006", "db_003")
    return tree


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-key", default="openrca_telecom", help="Dataset key, e.g. openrca_telecom")
    parser.add_argument("--namespace", default="stage4-test", help="Namespace string used only in prompt context")
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=REPO_ROOT / "tmp" / "stage4_global_judge_test",
        help="Directory to save Stage 4 outputs",
    )
    parser.add_argument(
        "--api-config",
        type=Path,
        default=REPO_ROOT / "clients/openrca_rca/api_config.yaml",
        help="API config YAML for the Stage 4 LLM call",
    )
    parser.add_argument("--profile-config", type=Path, help="Dataset config JSON for build_profile()")
    parser.add_argument("--query-start", help="Query start as epoch seconds or UTC datetime string")
    parser.add_argument("--query-end", help="Query end as epoch seconds or UTC datetime string")
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path to save the Stage 4 verdict/result JSON",
    )
    parser.add_argument(
        "--save-tree-json",
        type=Path,
        help="Optional path to save the tree after Stage 4 confirmation",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    save_dir = args.save_dir
    save_dir.mkdir(parents=True, exist_ok=True)
    api_config_path = args.api_config
    if not api_config_path.exists():
        raise SystemExit(f"API config not found: {api_config_path}")

    profile_config = args.profile_config
    if profile_config is None:
        inferred = _default_profile_config(args.dataset_key)
        profile_config = Path(inferred) if inferred else None

    llm_configs = load_config(str(api_config_path))
    llm_configs["_in_tokens"] = 0
    llm_configs["_out_tokens"] = 0

    profile = build_profile(
        args.dataset_key,
        config_path=str(profile_config) if profile_config else None,
    )

    time_range = {}
    start_val = _parse_time_arg(args.query_start)
    end_val = _parse_time_arg(args.query_end)
    if start_val is not None:
        time_range["start"] = start_val
    if end_val is not None:
        time_range["end"] = end_val

    pipeline = StagedRCAPipeline(
        actions=None,
        llm_configs=llm_configs,
        profile=profile,
        namespace=args.namespace,
        save_dir=str(save_dir),
        time_range=time_range,
        sprint=None,
        problem=None,
        enable_deep_dive=False,
        live_viewer=None,
    )
    pipeline.tree = build_sample_tree()
    input_tree_json = save_dir / "tree.input.json"
    pipeline.tree.save(input_tree_json)

    chosen = pipeline.stage4_global_judge()
    best = chosen or pipeline.tree.get_best_or_highest_confidence()
    result = {
        "input_tree_json": str(input_tree_json),
        "dataset_key": args.dataset_key,
        "selected_node_id": getattr(best, "id", ""),
        "component": getattr(best, "component", ""),
        "reason": getattr(best, "root_cause_reason_class", "") or getattr(best, "reason", ""),
        "datetime": getattr(best, "time", ""),
        "confidence": float(getattr(best, "confidence", 0.0) or 0.0),
        "evidence": getattr(best, "evidence", ""),
        "in_tokens": llm_configs.get("_in_tokens", 0),
        "out_tokens": llm_configs.get("_out_tokens", 0),
    }

    output_json = args.output_json or (save_dir / "stage4_judge_result.json")
    output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    save_tree_json = args.save_tree_json
    if save_tree_json is None:
        save_tree_json = save_dir / "tree.stage4.json"
    pipeline.tree.save(save_tree_json)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nSaved Stage 4 result to: {output_json}")
    print(f"Saved Stage 4 tree to: {save_tree_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
