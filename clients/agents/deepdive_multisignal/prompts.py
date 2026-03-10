"""Prompts for Deep Dive agent variant: explicit multi-signal criteria."""

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

Multi-signal confirmation policy (required):
- Confirmed minimum condition: metric + (trace or log) signals must agree.
- If only metric evidence exists, do not use high confidence.
- Confidence policy:
  * metric only -> medium at most
  * metric + (trace or log) -> high allowed
"""

DEEPDIVE_PROMPT_TEMPLATE = _BASE_DEEPDIVE_PROMPT_TEMPLATE.replace(
    "Response format:",
    _EXTRA + "\nResponse format:",
)
