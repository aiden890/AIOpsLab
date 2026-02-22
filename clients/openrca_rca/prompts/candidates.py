"""Build candidate string from config's possible_root_causes dict."""


def build_cand(possible_root_causes: dict) -> str:
    """Build candidate string from config's possible_root_causes dict.

    Supports two formats:
    - Flat: {"components": [...], "reasons": [...]}
    - Leveled: {"component_levels": {"node": [...], "pod": [...], "service": [...]}, "reasons": [...]}
    """
    if not possible_root_causes:
        return ""

    lines = []

    # Components section
    levels = possible_root_causes.get("component_levels")
    components = possible_root_causes.get("components", [])

    lines.append("## POSSIBLE ROOT CAUSE COMPONENTS:\n")
    if levels:
        if levels.get("node"):
            lines.append("(if the root cause is at the node level, i.e., the root cause is a specific node)\n")
            lines.extend(f"- {c}" for c in levels["node"])
            lines.append("")
        if levels.get("pod"):
            lines.append("(if the root cause is at the pod level, i.e., the root cause is a specific container)\n")
            lines.extend(f"- {c}" for c in levels["pod"])
            lines.append("")
        if levels.get("service"):
            lines.append("(if the root cause is at the service level, i.e., if all pods of a specific service are faulty, the root cause is the service itself)\n")
            lines.extend(f"- {c}" for c in levels["service"])
    else:
        lines.extend(f"- {c}" for c in components)

    # Reasons section
    reasons = possible_root_causes.get("reasons", [])
    if reasons:
        lines.append("\n## POSSIBLE ROOT CAUSE REASONS:\n")
        lines.extend(f"- {r}" for r in reasons)

    return "\n".join(lines)
