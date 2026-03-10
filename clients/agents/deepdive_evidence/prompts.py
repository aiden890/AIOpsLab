"""Prompts for Deep Dive agent variant: explicit evidence table."""

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

Evidence table discipline (required):
- Maintain an explicit table in your reasoning with columns:
  Claim | Supporting Evidence | Counter-Evidence | Decision.
- Update the table whenever new telemetry arrives.
- Verdict must be consistent with the latest evidence table.
"""

DEEPDIVE_PROMPT_TEMPLATE = _BASE_DEEPDIVE_PROMPT_TEMPLATE.replace(
    "Response format:",
    _EXTRA + "\nResponse format:",
)
