"""Action profile registry for controlling agent-visible actions.

This module lets runners expose different action sets to agents by profile.
Profiles can be built-in or loaded/overridden from a JSON file.
"""

from __future__ import annotations

import json
from pathlib import Path


BUILTIN_ACTION_PROFILES: dict[str, dict] = {
    # Backward-compatible default behavior.
    "legacy_execute_only": {
        "include": ["execute", "submit"],
    },
    # Structured outlier report API only (no raw output pathway).
    "outlier_report_only": {
        "include": ["execute_outlier_report", "submit"],
    },
    # Expose both executor APIs.
    "dual_execute": {
        "include": ["execute", "execute_outlier_report", "submit"],
    },
    # Keep every available action (except explicit excludes).
    "all_actions": {
        "include": [],
        "exclude": [],
    },
}


LEGACY_EXECUTOR_API_ALIAS = {
    "legacy": "legacy_execute_only",
    "anomaly_report": "outlier_report_only",
    "outlier_report": "outlier_report_only",
}


def resolve_profile_name(
    action_profile: str | None,
    executor_api_legacy: str | None = None,
) -> str:
    """Resolve final profile name with backward-compatible alias support."""
    if action_profile:
        return action_profile
    if executor_api_legacy:
        return LEGACY_EXECUTOR_API_ALIAS.get(executor_api_legacy, "legacy_execute_only")
    return "legacy_execute_only"


def load_profiles(profile_file: str | None = None) -> dict[str, dict]:
    """Load built-in profiles and merge optional JSON-defined profiles.

    JSON shape:
    {
      "profiles": {
        "name": {"include": [...], "exclude": [...]}
      }
    }

    For convenience, a top-level mapping without "profiles" is also accepted.
    """
    profiles = {k: dict(v) for k, v in BUILTIN_ACTION_PROFILES.items()}
    if not profile_file:
        return profiles

    path = Path(profile_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    custom = data.get("profiles", data)
    if not isinstance(custom, dict):
        raise ValueError(f"Invalid action profile file format: {profile_file}")

    for name, cfg in custom.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Profile '{name}' must be an object.")
        merged = dict(profiles.get(name, {}))
        merged.update(cfg)
        profiles[name] = merged

    return profiles


def select_agent_actions(
    all_apis: dict[str, str],
    action_profile: str,
    profile_file: str | None = None,
) -> tuple[dict[str, str], dict]:
    """Select agent-visible actions using an action profile."""
    profiles = load_profiles(profile_file=profile_file)
    if action_profile not in profiles:
        known = ", ".join(sorted(profiles.keys()))
        raise ValueError(
            f"Unknown action profile '{action_profile}'. Known profiles: {known}"
        )

    cfg = profiles[action_profile]
    include = cfg.get("include", [])
    exclude = set(cfg.get("exclude", []))

    if include:
        selected = {k: v for k, v in all_apis.items() if k in include}
    else:
        selected = dict(all_apis)

    for name in exclude:
        selected.pop(name, None)

    # Safety: submit should remain available when present in the task.
    if "submit" in all_apis and "submit" not in selected:
        selected["submit"] = all_apis["submit"]

    return selected, cfg


def list_known_profiles(profile_file: str | None = None) -> list[str]:
    """List available profile names (built-in + optional file)."""
    return sorted(load_profiles(profile_file=profile_file).keys())
