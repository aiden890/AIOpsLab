"""Prompts for Deep Dive agent variant: robust statistics."""

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

Robust statistics policy (required):
- Use robust anomaly checks before verdict (MAD, IQR, or percentile thresholds).
- Prefer distribution-aware thresholds over single-point absolute values.
- Treat isolated outliers as weak evidence unless sustained.
"""

DEEPDIVE_PROMPT_TEMPLATE = _BASE_DEEPDIVE_PROMPT_TEMPLATE.replace(
    "Response format:",
    _EXTRA + "\nResponse format:",
)
