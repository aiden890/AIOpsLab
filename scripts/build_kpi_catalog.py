import json
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dataset_config(name: str) -> dict:
    cfg_path = PROJECT_ROOT / "aiopslab/service/apps/static_dataset/config" / f"{name}.json"
    if not cfg_path.exists():
        raise FileNotFoundError(cfg_path)
    return json.loads(cfg_path.read_text())


def _resolve_metric_files(dataset_cfg: dict) -> list[Path]:
    base = PROJECT_ROOT / dataset_cfg["dataset_path"]
    base = base.resolve()
    metric_files = dataset_cfg.get("data_mapping", {}).get("metric_files", []) or []
    paths: list[Path] = []
    for pattern in metric_files:
        # pattern can be a filename like "metric_container.csv"
        matches = list(base.rglob(pattern))
        if not matches:
            matches = list(base.rglob(f"*{pattern}*"))
        paths.extend(matches)
    return sorted(set(paths))


def _is_dead_series(series: pd.Series) -> bool:
    """Return True if KPI is 'dead' – all values 0 or all 1 (after dropping NaNs)."""
    s = series.dropna()
    if s.empty:
        return True
    uni = s.unique()
    if len(uni) == 1 and (uni[0] == 0 or uni[0] == 1):
        return True
    return False


def _collect_non_dead_kpis_telecom(paths: list[Path]) -> list[str]:
    """Telecom metric schema: metric_container.csv etc with name/value columns."""
    kpis: set[str] = set()
    for p in paths:
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        name_col = None
        for cand in ["name", "kpi_name", "metric", "kpi"]:
            if cand in df.columns:
                name_col = cand
                break
        if not name_col or "value" not in df.columns:
            continue
        for kpi, grp in df.groupby(name_col):
            if _is_dead_series(grp["value"]):
                continue
            kpis.add(str(kpi))
    return sorted(kpis)


def _collect_non_dead_kpis_market(paths: list[Path]) -> list[str]:
    """Market metric schema: metric_container.csv / metric_node.csv with kpi_name/value."""
    kpis: set[str] = set()
    for p in paths:
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        name_col = None
        for cand in ["kpi_name", "name", "metric"]:
            if cand in df.columns:
                name_col = cand
                break
        if not name_col or "value" not in df.columns:
            continue
        for kpi, grp in df.groupby(name_col):
            if _is_dead_series(grp["value"]):
                continue
            kpis.add(str(kpi))
    return sorted(kpis)


def _collect_non_dead_kpis_bank(paths: list[Path]) -> list[str]:
    """Bank metric schema: metric_app.csv etc with name/value columns."""
    kpis: set[str] = set()
    for p in paths:
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        name_col = None
        for cand in ["name", "kpi_name", "metric"]:
            if cand in df.columns:
                name_col = cand
                break
        if not name_col or "value" not in df.columns:
            continue
        for kpi, grp in df.groupby(name_col):
            if _is_dead_series(grp["value"]):
                continue
            kpis.add(str(kpi))
    return sorted(kpis)


def build_catalog_openrca_market_cb1() -> None:
    cfg = _load_dataset_config("openrca_market_cloudbed1")
    all_paths = _resolve_metric_files(cfg)
    # Map metric file stem -> logical type
    file_type_map = {
        "metric_service": "service",
        "metric_container": "container",
        "metric_node": "node",
        "metric_runtime": "runtime",
        "metric_mesh": "mesh",
    }
    grouped: dict[str, list[Path]] = {}
    for p in all_paths:
        t = file_type_map.get(p.stem)
        if not t:
            continue
        grouped.setdefault(t, []).append(p)
    kpis_by_type: dict[str, list[str]] = {}
    for t, paths in grouped.items():
        kpis_by_type[t] = _collect_non_dead_kpis_market(paths)
    out_path = PROJECT_ROOT / "clients/tree_traversal/kpi_catalog_market.json"
    out_path.write_text(json.dumps({"kpis": kpis_by_type}, indent=2), encoding="utf-8")
    total = sum(len(v) for v in kpis_by_type.values())
    print(f"Market catalog written: {out_path} ({total} KPIs across {len(kpis_by_type)} types)")


def build_catalog_openrca_telecom() -> None:
    cfg = _load_dataset_config("openrca_telecom")
    all_paths = _resolve_metric_files(cfg)
    file_type_map = {
        "metric_app": "app",
        "metric_container": "container",
        "metric_node": "node",
        "metric_service": "service",
        "metric_middleware": "middleware",
    }
    grouped: dict[str, list[Path]] = {}
    for p in all_paths:
        t = file_type_map.get(p.stem)
        if not t:
            continue
        grouped.setdefault(t, []).append(p)
    kpis_by_type: dict[str, list[str]] = {}
    for t, paths in grouped.items():
        kpis_by_type[t] = _collect_non_dead_kpis_telecom(paths)
    out_path = PROJECT_ROOT / "clients/tree_traversal/kpi_catalog_telecom.json"
    out_path.write_text(json.dumps({"kpis": kpis_by_type}, indent=2), encoding="utf-8")
    total = sum(len(v) for v in kpis_by_type.values())
    print(f"Telecom catalog written: {out_path} ({total} KPIs across {len(kpis_by_type)} types)")


def build_catalog_openrca_bank() -> None:
    cfg = _load_dataset_config("openrca_bank")
    all_paths = _resolve_metric_files(cfg)
    file_type_map = {
        "metric_app": "app",
        "metric_container": "container",
    }
    grouped: dict[str, list[Path]] = {}
    for p in all_paths:
        t = file_type_map.get(p.stem)
        if not t:
            continue
        grouped.setdefault(t, []).append(p)
    kpis_by_type: dict[str, list[str]] = {}
    for t, paths in grouped.items():
        kpis_by_type[t] = _collect_non_dead_kpis_bank(paths)
    out_path = PROJECT_ROOT / "clients/tree_traversal/kpi_catalog_bank.json"
    out_path.write_text(json.dumps({"kpis": kpis_by_type}, indent=2), encoding="utf-8")
    total = sum(len(v) for v in kpis_by_type.values())
    print(f"Bank catalog written: {out_path} ({total} KPIs across {len(kpis_by_type)} types)")


def main():
    # Build all catalogs; run this manually when datasets change.
    build_catalog_openrca_market_cb1()
    build_catalog_openrca_telecom()
    build_catalog_openrca_bank()


if __name__ == "__main__":
    main()

