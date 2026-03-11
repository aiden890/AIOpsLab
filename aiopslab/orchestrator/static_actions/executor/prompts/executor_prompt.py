"""Executor prompt specs keyed by action name."""

COMMON_RULE = """## RULES OF PYTHON CODE WRITING:

1. Reuse variables as much as possible for execution efficiency since the IPython Kernel is stateful, i.e., variables defined in previous steps can be used in subsequent steps.
2. Use variable name rather than `print()` to display the execution results since your Python environment is IPython Kernel rather than Python.exe. If you want to display multiple variables, use commas to separate them, e.g. `var1, var2`. IMPORTANT: IPython only captures the result of the **last top-level expression** — expressions inside `if/else` blocks are NOT captured. Always place the final display expression at the top level (outside any `if/else`), or use `print()` inside conditional blocks.
3. Use pandas DataFrame to process and display tabular data for efficiency and briefness. Avoid transforming DataFrame to list or dict type for display.
4. If you encounter an error or unexpected result, rewrite the code by referring to the given IPython Kernel error message.
5. Do not simulate any virtual situation or assume anything unknown. Solve the real problem.
6. Do not store any data as files in the disk. Only cache the data as variables in the memory.
7. Do not visualize the data or draw pictures or graphs via Python. You can only provide text-based results. Never include the `matplotlib` or `seaborn` library in the code.
8. Do not generate anything else except the Python code block except the instruction tells you to 'Use plain English'. If you find the input instruction is a summarization task (which is typically happening in the last step), you should comprehensively summarize the conclusion as a string in your code and display it directly.
9. Do not calculate threshold AFTER filtering data within the given time duration. Always calculate global thresholds using the entire KPI series of a specific component within a metric file BEFORE filtering data within the given time duration.
10. All issues use **UTC** time. However, the local machine's default timezone is unknown.

## DATA ACCESS:

A pre-injected `telemetry` object is available in the IPython Kernel.
Use it to fetch raw telemetry data from the environment:

    log_path = telemetry.get_logs()                 # all logs
    log_path = telemetry.get_logs("service_name")   # specific service
    metric_path = telemetry.get_metrics()
    trace_path = telemetry.get_traces()

Each call returns the **full file path** to a CSV. Read it directly:

    import pandas as pd
    log_df = pd.read_csv(log_path)
    metric_df = pd.read_csv(metric_path)
    trace_df = pd.read_csv(trace_path)

Note: Call telemetry.get_*() only once per data type, then reuse the cached DataFrame variable."""

ANOMALY_METHOD_RULE = """
## OUTLIER REPORT METHOD:

When the caller asks for outlier detection or uses the outlier-report API, use this default method unless the instruction explicitly overrides it:

1. Baseline scope:
   - Build the baseline from the full KPI series for that component and KPI BEFORE filtering to the target incident window.
   - Never compute thresholds only from the incident window itself.

2. Point outlier rule:
   - Primary rule: median/MAD.
   - Positive-direction KPIs: outlier if value > median + 3*MAD.
   - Negative-direction KPIs (for example thread_idle): outlier if value < median - 3*MAD.

3. Fallback when MAD == 0 or MAD is undefined:
   - Use IQR = Q3 - Q1 from the same baseline series.
   - Positive-direction KPIs: outlier if value > Q3 + 1.5*IQR.
   - Negative-direction KPIs: outlier if value < Q1 - 1.5*IQR.

4. Output requirements:
   - Set baseline_method to the actual method used, e.g. "global median/MAD with IQR fallback".
   - Set threshold_rule to the actual rule used, e.g. "positive KPI: value > median + 3*MAD; negative KPI: value < median - 3*MAD; fallback: IQR 1.5x".
   - Point outliers are sufficient evidence to report statistical deviation; do not require a sustained-run criterion unless the instruction explicitly asks for one.
   - If the instruction asks for sustained windows, report them separately and follow the user-specified minimum run length.
   - If a fallback was used for a KPI, mention it in data_quality.notes or summary.
"""

CODE_FORMAT = """```python
(YOUR CODE HERE)
```"""

BASE_SYSTEM_TEMPLATE = """You are a DevOps assistant for writing Python code to answer DevOps questions. For each question, you need to write Python code to solve it by retrieving and processing telemetry data of the target system. Your generated Python code will be automatically submitted to a IPython Kernel. The execution result output in IPython Kernel will be used as the answer to the question.

{rule}

There is some domain knowledge for you:

{background}

Your response should follow the Python block format below:

{format}"""

EXECUTE_SUMMARY_TEMPLATE = """The code execution is successful. The execution result is shown below:

{result}

Summarize a straightforward answer based on the execution results.
IMPORTANT:
- Use plain English only. Do NOT include any Python code, code blocks, or variable assignments.
- Include specific numbers, component names, and timestamps from the results.
- Keep the summary concise (under 500 words)."""

ANOMALY_REPORT_SUMMARY_TEMPLATE = """The code execution is successful. The execution result is shown below:

{result}

Convert this into a structured outlier report.
Apply this outlier filtering method when interpreting the result:

{anomaly_method_rule}

Return ONLY one valid JSON object (no markdown, no code fences, no extra text),
using this exact top-level schema:
{{
  "report_type": "outlier_report",
  "component": "<component or empty>",
  "window_utc": {{"start": "<YYYY-MM-DD HH:MM:SS or empty>", "end": "<YYYY-MM-DD HH:MM:SS or empty>"}},
  "baseline_method": "<method used or unknown>",
  "threshold_rule": "<rule used or unknown>",
  "kpi_results": [
    {{
      "metric": "<name>",
      "sample_interval_sec": <number or null>,
      "outlier_points": [
        {{"timestamp_utc": "<YYYY-MM-DD HH:MM:SS>", "value": <number or null>, "score": <number or null>, "flag": true}}
      ],
      "sustained_outlier_windows": [
        {{"start_utc": "<YYYY-MM-DD HH:MM:SS>", "end_utc": "<YYYY-MM-DD HH:MM:SS>", "max_score": <number or null>}}
      ]
    }}
  ],
  "target_timestamp_check": {{
    "timestamp_utc": "<YYYY-MM-DD HH:MM:SS or empty>",
    "outlier_metrics": ["<metric>", "..."]
  }},
  "data_quality": {{
    "missing_minutes_utc": ["<YYYY-MM-DD HH:MM:SS>", "..."],
    "notes": ["<short note>", "..."]
  }},
  "summary": "<concise plain-English summary>"
}}

Rules:
- Keep missing/unknown fields as empty string, empty list, or null (never omit keys).
- Use UTC timestamps exactly as shown in the result when possible.
- If no outliers are found, return empty outlier lists and explain in summary.
- Treat outlier detection as statistical evidence only; do not claim that an outlier alone proves a fault.
- Prefer the outlier method above over ad-hoc thresholds.
- Do not invent values not present in the execution result."""

LEGACY_CONCLUSION_TEMPLATE = """{answer}

--- Raw Output ---
{result}"""

STRUCTURED_CONCLUSION_TEMPLATE = """{answer}"""

PROMPT_SPECS = {
    "execute": {
        "rule": COMMON_RULE,
        "system_template": BASE_SYSTEM_TEMPLATE,
        "code_format": CODE_FORMAT,
        "followup_template": EXECUTE_SUMMARY_TEMPLATE,
        "followup_template_kwargs": (),
        "structured_output": False,
        "conclusion_template": LEGACY_CONCLUSION_TEMPLATE,
    },
    "execute_outlier_report": {
        "rule": "\n\n".join([COMMON_RULE, ANOMALY_METHOD_RULE.strip()]),
        "system_template": BASE_SYSTEM_TEMPLATE,
        "code_format": CODE_FORMAT,
        "followup_template": ANOMALY_REPORT_SUMMARY_TEMPLATE,
        "followup_template_kwargs": ("anomaly_method_rule",),
        "structured_output": True,
        "conclusion_template": STRUCTURED_CONCLUSION_TEMPLATE,
    },
}


def get_prompt_spec(action_name: str) -> dict:
    """Return executor prompt spec for a given action."""
    return PROMPT_SPECS.get(action_name, PROMPT_SPECS["execute"])
