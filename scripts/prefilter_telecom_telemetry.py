"""Prefilter Telecom telemetry for TARGET_INDICES (query_start–query_end) per problem.

Writes one directory per task under output_dir, e.g.:
  output_dir/task_2-0/static-telecom/metrics/
  output_dir/task_2-0/static-telecom/traces/
  output_dir/task_2-0/static-telecom/logs/

Use with run_telecom.py --use-prefiltered <output_dir> so RCA reads from these
files instead of running process_telemetry in Docker.

Usage:
  python scripts/prefilter_telecom_telemetry.py --output-dir prefiltered_telemetry
  python scripts/prefilter_telecom_telemetry.py --output-dir prefiltered_telemetry --all
  python scripts/prefilter_telecom_telemetry.py --output-dir prefiltered_telemetry --all --workers 4
  python scripts/prefilter_telecom_telemetry.py --output-dir prefiltered_telemetry --indices 0 2 4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
from pathlib import Path

# Repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aiopslab.paths import BASE_PARENT_DIR
from aiopslab.service.apps.static_dataset.dataset import StaticDataset

# Default indices from run_telecom.py (used when --all is not set)
TARGET_INDICES = [0, 2, 4, 5, 8, 9, 13, 19, 20, 27]

PROCESS_TELEMETRY = REPO_ROOT / "aiopslab-applications/static_dataset/process_telemetry.py"


def _prefilter_one(
    idx: int,
    output_dir: str,
    raw_path: str,
    dataset_config: dict,
    dataset_config_name: str,
    process_telemetry_path: str,
    repo_root: str,
) -> tuple[str, str, int | None]:
    """Run prefilter for one index. Returns (status, problem_key, returncode_or_none)."""
    try:
        app = StaticDataset(dataset_config_name, query_index=idx)
        if not app.time_remapper or not app.query_info:
            return ("skip_no_config", f"idx_{idx}", None)

        mapping = app.time_remapper.mapping
        task_id = app.query_info.task_id
        problem_key = f"{task_id}-{idx}"
        namespace = app.namespace

        task_out = Path(output_dir) / problem_key
        metadata_file = task_out / "metadata.json"
        if metadata_file.exists():
            return ("skip_exists", problem_key, None)

        processing_config = {
            "namespace": namespace,
            "data_mapping": dataset_config.get("data_mapping", {}),
            "telemetry": dataset_config.get("telemetry", {}),
            "replay": dataset_config.get("replay", {}),
            "time_offset": mapping.get("time_offset", 0),
            "init_start_original": mapping.get("init_start_original"),
            "init_end_original": mapping.get("init_end_original"),
        }

        task_out.mkdir(parents=True, exist_ok=True)

        fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="aiopslab_prefilter_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(processing_config, f, indent=2)

            env = os.environ.copy()
            env["RAW_DATA_PATH"] = raw_path
            env["OUTPUT_PATH"] = str(task_out)
            env["CONFIG_PATH"] = cfg_path

            cmd = [sys.executable, process_telemetry_path, "--mode", "init"]
            r = subprocess.run(cmd, env=env, cwd=repo_root, timeout=600)
            if r.returncode != 0:
                return ("fail", problem_key, r.returncode)

            metadata = {
                "problem_key": problem_key,
                "task_id": task_id,
                "query_index": idx,
                "namespace": namespace,
            }
            (task_out / "metadata.json").write_text(json.dumps(metadata, indent=2))
            for name in (".chunk_index", ".stream_state.json"):
                p = task_out / name
                if p.exists():
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
            return ("ok", problem_key, None)
        finally:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
    except Exception as e:
        return ("error", f"idx_{idx}", str(e))


def _get_all_query_indices(dataset_config: dict, raw_path: Path) -> list[int]:
    """Return all valid query indices from query.csv."""
    import pandas as pd

    query_file = raw_path / dataset_config.get("query", {}).get("query_file", "query.csv")
    if not query_file.exists():
        return []
    query_df = pd.read_csv(query_file)
    return list(range(len(query_df)))


def main():
    parser = argparse.ArgumentParser(
        description="Prefilter Telecom telemetry per task (query window) for fast RCA."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("prefiltered_telemetry"),
        help="Base directory for per-task telemetry (default: prefiltered_telemetry)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Prefilter all query indices (skip TARGET_INDICES)",
    )
    parser.add_argument(
        "--indices",
        type=int,
        nargs="+",
        default=None,
        help="Query indices to prefilter (default: TARGET_INDICES; ignored if --all)",
    )
    parser.add_argument(
        "--dataset-config",
        type=str,
        default="openrca_telecom",
        help="Dataset config name (default: openrca_telecom)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1 = sequential)",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = REPO_ROOT / "aiopslab/service/apps/static_dataset/config"
    config_file = config_path / f"{args.dataset_config}.json"
    if not config_file.exists():
        print(f"Config not found: {config_file}")
        sys.exit(1)

    with open(config_file) as f:
        dataset_config = json.load(f)

    raw_path = dataset_config.get("dataset_path")
    if not raw_path:
        print("dataset_path not in config")
        sys.exit(1)
    if not Path(raw_path).is_absolute():
        raw_path = BASE_PARENT_DIR / raw_path
    raw_path = Path(raw_path).resolve()
    if not raw_path.exists():
        print(f"Dataset path does not exist: {raw_path}")
        sys.exit(1)

    if not PROCESS_TELEMETRY.exists():
        print(f"process_telemetry.py not found: {PROCESS_TELEMETRY}")
        sys.exit(1)

    if args.all:
        indices = _get_all_query_indices(dataset_config, raw_path)
        if not indices:
            print("No query indices found (query.csv missing or empty)")
            sys.exit(1)
        print(f"Prefiltering all {len(indices)} indices (0..{len(indices)-1})")
    else:
        indices = args.indices if args.indices is not None else TARGET_INDICES
        print(f"Prefiltering {len(indices)} indices: {indices}")

    workers = max(1, args.workers)
    out_str = str(output_dir)
    raw_str = str(raw_path)
    proc_str = str(PROCESS_TELEMETRY)
    repo_str = str(REPO_ROOT)

    run_one = partial(
        _prefilter_one,
        output_dir=out_str,
        raw_path=raw_str,
        dataset_config=dataset_config,
        dataset_config_name=args.dataset_config,
        process_telemetry_path=proc_str,
        repo_root=repo_str,
    )

    ok_count = fail_count = skip_count = 0

    if workers <= 1:
        for idx in indices:
            status, problem_key, extra = run_one(idx)
            if status == "ok":
                print(f"Prefiltered {problem_key} -> {output_dir / problem_key}")
                ok_count += 1
            elif status in ("skip_exists", "skip_no_config"):
                print(f"[{idx}] Skip {problem_key}: {'already exists' if status == 'skip_exists' else 'no config'}")
                skip_count += 1
            else:
                print(f"  Failed {problem_key}: {extra}")
                fail_count += 1
    else:
        print(f"Using {workers} workers")
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(run_one, idx): idx for idx in indices}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    status, problem_key, extra = future.result()
                    if status == "ok":
                        print(f"Prefiltered {problem_key}")
                        ok_count += 1
                    elif status in ("skip_exists", "skip_no_config"):
                        print(f"[{idx}] Skip {problem_key}")
                        skip_count += 1
                    else:
                        print(f"  Failed {problem_key}: {extra}")
                        fail_count += 1
                except Exception as e:
                    print(f"  Error idx {idx}: {e}")
                    fail_count += 1

    print(f"Done: {ok_count} ok, {skip_count} skipped, {fail_count} failed")
    print(f"Use: python clients/tree_traversal/run_telecom.py --use-prefiltered {output_dir}")


if __name__ == "__main__":
    main()
