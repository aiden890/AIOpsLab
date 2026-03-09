"""Benchmark LLM response speed across reasoning effort levels.

Sends the same prompt with different reasoning_effort settings and compares
latency, token counts, and response length.

Usage:
    python experiments/benchmark_reasoning.py
    python experiments/benchmark_reasoning.py --efforts low,medium,high,xhigh --rounds 3
    python experiments/benchmark_reasoning.py --prompt "Explain quicksort step by step"
"""

import argparse
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_PROMPT = (
    "You are a DevOps engineer analyzing a microservice system failure. "
    "Given that the frontend service latency spiked at 14:30 UTC and "
    "node-1 CPU reached 95% at 14:28 UTC, while node-3 disk I/O "
    "increased 10x at 14:25 UTC, determine the most likely root cause "
    "chain and explain your reasoning step by step."
)


def call_llm(client, model, prompt, effort):
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
    )
    if effort:
        kwargs["reasoning_effort"] = effort

    start = time.time()
    resp = client.chat.completions.create(**kwargs)
    elapsed = time.time() - start

    choice = resp.choices[0]
    usage = resp.usage

    return {
        "effort": effort or "default",
        "elapsed": elapsed,
        "content": choice.message.content,
        "content_len": len(choice.message.content),
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "total_tokens": usage.total_tokens if usage else 0,
        "reasoning_tokens": getattr(usage, "completion_tokens_details", None)
            and getattr(usage.completion_tokens_details, "reasoning_tokens", 0) or 0,
    }


def run_benchmark(client, model, prompt, efforts, rounds):
    results = {e: [] for e in efforts}

    for round_num in range(1, rounds + 1):
        for effort in efforts:
            print(f"  Round {round_num}/{rounds} | effort={effort:6s} ...", end="", flush=True)
            try:
                r = call_llm(client, model, prompt, effort)
                results[effort].append(r)
                print(f" {r['elapsed']:.2f}s | {r['total_tokens']} tok | {r['content_len']} chars")
            except Exception as e:
                print(f" ERROR: {e}")

    return results


def print_summary(results):
    print(f"\n{'=' * 80}")
    print(f"{'Effort':>8s} {'Avg Time':>10s} {'Min':>8s} {'Max':>8s} "
          f"{'Avg Tok':>8s} {'Avg Reason':>11s} {'Avg Chars':>10s} {'Rounds':>7s}")
    print(f"{'-' * 80}")

    for effort, runs in results.items():
        if not runs:
            print(f"{effort:>8s}  (no successful runs)")
            continue
        times = [r["elapsed"] for r in runs]
        tokens = [r["total_tokens"] for r in runs]
        reason = [r["reasoning_tokens"] for r in runs]
        chars = [r["content_len"] for r in runs]
        print(f"{effort:>8s} {sum(times)/len(times):>9.2f}s {min(times):>7.2f}s {max(times):>7.2f}s "
              f"{sum(tokens)/len(tokens):>8.0f} {sum(reason)/len(reason):>11.0f} "
              f"{sum(chars)/len(chars):>10.0f} {len(runs):>7d}")

    # Speed comparison relative to lowest effort
    efforts_list = list(results.keys())
    if len(efforts_list) >= 2:
        base = efforts_list[0]
        base_avg = sum(r["elapsed"] for r in results[base]) / len(results[base]) if results[base] else 0
        if base_avg > 0:
            print(f"\nRelative to {base}:")
            for effort in efforts_list[1:]:
                if results[effort]:
                    avg = sum(r["elapsed"] for r in results[effort]) / len(results[effort])
                    print(f"  {effort}: {avg/base_avg:.2f}x slower ({avg - base_avg:+.2f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark reasoning effort speed")
    parser.add_argument("--prompt", type=str, default=DEFAULT_PROMPT,
                        help="Prompt to send")
    parser.add_argument("--efforts", type=str, default="low,xhigh",
                        help="Comma-separated effort levels (default: low,xhigh)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Number of rounds per effort level (default: 3)")
    parser.add_argument("--model", type=str, default=None,
                        help="Model name (default: from api_config.yaml)")
    parser.add_argument("--api-config", type=str,
                        default="clients/openrca_rca/api_config.yaml",
                        help="API config file for base URL and key")
    args = parser.parse_args()

    # Load config
    with open(args.api_config) as f:
        cfg = yaml.safe_load(f) or {}

    api_key = cfg.get("API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY", "")
    api_base = cfg.get("API_BASE") or os.environ.get("AZURE_OPENAI_BASE_URL", "")
    model = args.model or cfg.get("MODEL", "gpt-5")

    client = OpenAI(api_key=api_key, base_url=api_base)
    efforts = [e.strip() for e in args.efforts.split(",")]

    print(f"Model:   {model}")
    print(f"Base:    {api_base}")
    print(f"Efforts: {efforts}")
    print(f"Rounds:  {args.rounds}")
    print(f"Prompt:  {args.prompt[:80]}...")
    print(f"\nRunning benchmark...\n")

    results = run_benchmark(client, model, args.prompt, efforts, args.rounds)
    print_summary(results)
