"""Prompts for Deep Dive agent variant: hypothesis-driven loop."""

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

Hypothesis-driven loop (required):
- In DEEP DIVE, evaluate reason candidates one by one.
- For each reason candidate, collect supporting and contradicting evidence.
- Explicitly reject reasons that fail evidence checks before moving to the next.
- Final verdict must name the best-supported surviving reason.
"""

DEEPDIVE_PROMPT_TEMPLATE = _BASE_DEEPDIVE_PROMPT_TEMPLATE.replace(
    "Response format:",
    _EXTRA + "\nResponse format:",
)
