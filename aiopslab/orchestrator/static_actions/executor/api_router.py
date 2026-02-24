"""LLM API router supporting multiple backends.

Adapted from OpenRCA's api_router.py. Supports OpenAI, Google, Anthropic,
and generic OpenAI-compatible endpoints.
"""

import os
import time
import yaml
from dotenv import load_dotenv

load_dotenv()


def load_config(config_path):
    configs = dict(os.environ)
    with open(config_path, "r") as file:
        yaml_data = yaml.safe_load(file) or {}
    # Only override with non-empty yaml values
    for k, v in yaml_data.items():
        if v not in (None, ""):
            configs[k] = v
    # Fall back to env vars if API_KEY not set
    if not configs.get("API_KEY"):
        configs["API_KEY"] = (
            os.environ.get("AZURE_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
    # Fall back to AZURE_OPENAI_BASE_URL for API_BASE
    if not configs.get("API_BASE"):
        configs["API_BASE"] = os.environ.get("AZURE_OPENAI_BASE_URL", "")
    return configs


def _openai_chat(messages, temperature, configs):
    from openai import OpenAI
    client = OpenAI(api_key=configs["API_KEY"])
    return client.chat.completions.create(
        model=configs["MODEL"],
        messages=messages,
        temperature=temperature,
    ).choices[0].message.content


def _google_chat(messages, temperature, configs):
    import google.generativeai as genai
    genai.configure(api_key=configs["API_KEY"])
    genai.GenerationConfig(temperature=temperature)
    system_instruction = messages[0]["content"] if messages[0]["role"] == "system" else None
    msgs = [item for item in messages if item["role"] != "system"]
    msgs = [{"role": "model" if item["role"] == "assistant" else item["role"], "parts": item["content"]} for item in msgs]
    history = msgs[:-1]
    message = msgs[-1]
    return genai.GenerativeModel(
        model_name=configs["MODEL"],
        system_instruction=system_instruction,
    ).start_chat(
        history=history if history else None,
    ).send_message(message).text


def _anthropic_chat(messages, temperature, configs):
    import anthropic
    client = anthropic.Anthropic(api_key=configs["API_KEY"])
    system_msg = None
    filtered = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            filtered.append(m)
    kwargs = dict(model=configs["MODEL"], messages=filtered, temperature=temperature, max_tokens=4096)
    if system_msg:
        kwargs["system"] = system_msg
    return client.messages.create(**kwargs).content[0].text


def _compatible_chat(messages, temperature, configs):
    """OpenAI-compatible endpoint (e.g., vLLM, Azure, third-party)."""
    from openai import OpenAI
    client = OpenAI(api_key=configs["API_KEY"], base_url=configs["API_BASE"])
    return client.chat.completions.create(
        model=configs["MODEL"],
        messages=messages,
        temperature=temperature,
    ).choices[0].message.content


_BACKENDS = {
    "OpenAI": _openai_chat,
    "Google": _google_chat,
    "Anthropic": _anthropic_chat,
    "AI": _compatible_chat,
}


def get_chat_completion(messages, configs, temperature=0.0):
    """Call LLM with retry logic.

    Args:
        messages: Chat messages list.
        configs: Dict with SOURCE, MODEL, API_KEY, etc.
        temperature: Sampling temperature.

    Returns:
        str: LLM response text.
    """
    backend = _BACKENDS.get(configs["SOURCE"])
    if backend is None:
        raise ValueError(f"Invalid SOURCE '{configs['SOURCE']}'. Choose from: {list(_BACKENDS.keys())}")

    for attempt in range(3):
        try:
            return backend(messages, temperature, configs)
        except Exception as e:
            if "429" in str(e):
                time.sleep(2 ** attempt)
                continue
            raise
