"""Deep Dive variant agent: explicit evidence table."""

from clients.agents.deepdive1.variant_base import DeepDivePromptVariantAgent
from .prompts import (
    EXPLORATION_PROMPT,
    DEEPDIVE_PROMPT_TEMPLATE,
    EXPAND_PROMPT_TEMPLATE,
    SYSTEM_TEMPLATE,
)


class DeepDiveAgentEvidence(DeepDivePromptVariantAgent):
    EXPLORATION_PROMPT = EXPLORATION_PROMPT
    DEEPDIVE_PROMPT_TEMPLATE = DEEPDIVE_PROMPT_TEMPLATE
    EXPAND_PROMPT_TEMPLATE = EXPAND_PROMPT_TEMPLATE
    SYSTEM_TEMPLATE = SYSTEM_TEMPLATE
