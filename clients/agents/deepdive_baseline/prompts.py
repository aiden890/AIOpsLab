"""Prompts for Deep Dive agent variant: baseline vs incident."""

from clients.agents.deepdive1.prompts import (
    EXPLORATION_PROMPT,
    DEEPDIVE_PROMPT_TEMPLATE as _BASE_DEEPDIVE_PROMPT_TEMPLATE,
    EXPAND_PROMPT_TEMPLATE,
    SYSTEM_TEMPLATE,
    ACTION_LIST_TEMPLATE,
    EXECUTE_SECTION,
    SUMMARIZE_PROMPT,
    FORCE_SUBMIT_TEMPLATE,
)

_EXTRA = """\

Baseline vs Incident (required):
- Compare incident window against at least one nearby baseline window.
- Report deltas (absolute and ratio/percentage) for core KPIs.
- Do not confirm fault from incident window alone without baseline contrast.
"""

DEEPDIVE_PROMPT_TEMPLATE = _BASE_DEEPDIVE_PROMPT_TEMPLATE.replace(
    "Response format:",
    _EXTRA + "\nResponse format:",
)
