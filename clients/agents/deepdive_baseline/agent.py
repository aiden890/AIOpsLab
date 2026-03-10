"""Deep Dive variant agent: baseline vs incident."""

from clients.agents.deepdive1.variant_base import DeepDivePromptVariantAgent
from .prompts import (
    EXPLORATION_PROMPT,
    DEEPDIVE_PROMPT_TEMPLATE,
    EXPAND_PROMPT_TEMPLATE,
    SYSTEM_TEMPLATE,
)


class DeepDiveAgentBaseline(DeepDivePromptVariantAgent):
    EXPLORATION_PROMPT = EXPLORATION_PROMPT
    DEEPDIVE_PROMPT_TEMPLATE = DEEPDIVE_PROMPT_TEMPLATE
    EXPAND_PROMPT_TEMPLATE = EXPAND_PROMPT_TEMPLATE
    SYSTEM_TEMPLATE = SYSTEM_TEMPLATE
