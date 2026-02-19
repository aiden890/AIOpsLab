from clients.openrca_rca.prompts.controller_prompt import rules as controller_rules
from clients.openrca_rca.prompts.executor_prompt import rule as executor_rule
from clients.openrca_rca.prompts import (
    basic_prompt_bank,
    basic_prompt_telecom,
    basic_prompt_market,
)

# Map dataset keys to their basic prompts
BASIC_PROMPTS = {
    "openrca_bank": basic_prompt_bank,
    "openrca_telecom": basic_prompt_telecom,
    "openrca_market_cb1": basic_prompt_market,
    "openrca_market_cb2": basic_prompt_market,
}


def get_basic_prompt(dataset_key):
    """Get the basic prompt module (with .cand and .schema) for a dataset."""
    prompt = BASIC_PROMPTS.get(dataset_key)
    if prompt is None:
        raise ValueError(f"No basic prompt for dataset '{dataset_key}'. Available: {list(BASIC_PROMPTS.keys())}")
    return prompt
