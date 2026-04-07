"""LLM API router supporting multiple backends.

Adapted from OpenRCA's api_router.py. Supports OpenAI, Google, Anthropic,
and generic OpenAI-compatible endpoints.
"""

import logging
import math
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
    timeout = float(configs.get("TIMEOUT", 120))
    client = OpenAI(api_key=configs["API_KEY"], timeout=timeout)
    kwargs = dict(model=configs["MODEL"], messages=messages)
    if configs.get("REASONING_EFFORT"):
        kwargs["reasoning_effort"] = configs["REASONING_EFFORT"]
    else:
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    # Accumulate token usage on the configs dict so callers can read per-run totals.
    usage = getattr(resp, "usage", None)
    if usage is not None:
        inp = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
        out = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
        if inp is not None:
            configs["_in_tokens"] = configs.get("_in_tokens", 0) + int(inp)
        if out is not None:
            configs["_out_tokens"] = configs.get("_out_tokens", 0) + int(out)
    return resp.choices[0].message.content


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
    timeout = float(configs.get("TIMEOUT", 120))
    client = OpenAI(api_key=configs["API_KEY"], base_url=configs["API_BASE"], timeout=timeout)
    kwargs = dict(model=configs["MODEL"], messages=messages)
    if configs.get("REASONING_EFFORT"):
        kwargs["reasoning_effort"] = configs["REASONING_EFFORT"]
    else:
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        inp = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
        out = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
        if inp is not None:
            configs["_in_tokens"] = configs.get("_in_tokens", 0) + int(inp)
        if out is not None:
            configs["_out_tokens"] = configs.get("_out_tokens", 0) + int(out)
    return resp.choices[0].message.content


_BACKENDS = {
    "OpenAI": _openai_chat,
    "Google": _google_chat,
    "Anthropic": _anthropic_chat,
    "AI": _compatible_chat,
}


logger = logging.getLogger("openrca_rca")


def _json_safe(value):
    """Convert value to JSON-safe data (no NaN/Inf, no exotic objects)."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    # Fallback: stringify objects (e.g., numpy scalars, datetime-like custom objects).
    return str(value)


def _sanitize_messages(messages):
    """Normalize chat messages to stable JSON-safe list[dict]."""
    sanitized = _json_safe(messages)
    if not isinstance(sanitized, list):
        return [{"role": "user", "content": str(sanitized)}]
    out = []
    for m in sanitized:
        if isinstance(m, dict):
            role = str(m.get("role", "user"))
            content = m.get("content", "")
            out.append({"role": role, "content": content})
        else:
            out.append({"role": "user", "content": str(m)})
    return out


def _is_invalid_json_body_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return (
        "could not parse the json body of your request" in msg
        or ("invalid_request_error" in msg and "json body" in msg)
    )


def get_chat_completion(messages, configs, temperature=0.0):
    """Call LLM with unlimited retry on rate limit (429).

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

    attempt = 0
    invalid_json_retried = False
    safe_messages = _sanitize_messages(messages)
    while True:
        try:
            return backend(safe_messages, temperature, configs)
        except Exception as e:
            if _is_invalid_json_body_error(e) and not invalid_json_retried:
                invalid_json_retried = True
                safe_messages = _sanitize_messages(safe_messages)
                logger.warning(
                    "BadRequest invalid JSON body detected; retried once with JSON-safe sanitized messages."
                )
                continue
            if "429" in str(e) or "rate" in str(e).lower():
                wait = min(2 ** attempt, 60)
                logger.warning(f"Rate limited (attempt {attempt + 1}), retrying in {wait}s")
                time.sleep(wait)
                attempt += 1
                continue
            raise
