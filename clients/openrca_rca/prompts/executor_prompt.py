"""Executor prompt rules for Python code generation.

Adapted from OpenRCA executor.py prompts.
Key change: uses `telemetry` helper object instead of direct file paths.
"""

rule = """## RULES OF PYTHON CODE WRITING:

1. Reuse variables as much as possible for execution efficiency since the IPython Kernel is stateful, i.e., variables define in previous steps can be used in subsequent steps.
2. Use variable name rather than `print()` to display the execution results since your Python environment is IPython Kernel rather than Python.exe. If you want to display multiple variables, use commas to separate them, e.g. `var1, var2`. **Always end the code cell with the variable name as the last expression** (e.g., last line must be `result_var`, not `result_var = "..."`), so the value is captured as the cell output. A code cell ending with an assignment returns None and produces no output.
3. Use pandas Dataframe to process and display tabular data for efficiency and briefness. Avoid transforming Dataframe to list or dict type for display.
4. If you encounter an error or unexpected result, rewrite the code by referring to the given IPython Kernel error message.
5. Do not simulate any virtual situation or assume anything unknown. Solve the real problem.
6. Do not store any data as files in the disk. Only cache the data as variables in the memory.
7. Do not visualize the data or draw pictures or graphs via Python. You can only provide text-based results. Never include the `matplotlib` or `seaborn` library in the code.
8. Do not generate anything else except the Python code block except the instruction tell you to 'Use plain English'. If you find the input instruction is a summarization task (which is typically happening in the last step), provide comprehensive natural language analysis in your final code step.
9. Do not calculate threshold AFTER filtering data within the given time duration. Always calculate global thresholds using the entire KPI series of a specific component within a metric file BEFORE filtering data within the given time duration.
10. **Keep output concise.** The execution result must not exceed ~16,000 tokens. If the data is large, summarize or aggregate it (e.g., use `.groupby()`, `.describe()`, or display only top-N rows) rather than printing the full DataFrame. Never output raw DataFrames with hundreds or thousands of rows.

## DATA ACCESS:

A pre-injected `telemetry` object is available in the IPython Kernel.
Use it to fetch raw telemetry data from the environment:

    logs_path = telemetry.get_logs()                 # all logs
    logs_path = telemetry.get_logs("service_name")   # specific service
    metrics_path = telemetry.get_metrics()
    traces_path = telemetry.get_traces()

Each call returns a **file path** (str) or **None** if no data is available.
Always check for None before reading:

    import pandas as pd
    logs_path = telemetry.get_logs()
    log_df = pd.read_csv(logs_path) if logs_path else pd.DataFrame()

    metrics_path = telemetry.get_metrics()
    metric_df = pd.read_csv(metrics_path) if metrics_path else pd.DataFrame()

    traces_path = telemetry.get_traces()
    trace_df = pd.read_csv(traces_path) if traces_path else pd.DataFrame()

IMPORTANT: Do NOT append filenames to the path. The returned value is already the full CSV file path.
IMPORTANT: Always check if the return value is None before calling pd.read_csv().
Note: Call telemetry.get_*() only once per data type, then reuse the cached DataFrame variable."""


system_template = """You are a DevOps assistant for writing Python code to answer DevOps questions. For each question, you need to write Python code to solve it by retrieving and processing telemetry data of the target system. Your generated Python code will be automatically submitted to a IPython Kernel. The execution result output in IPython Kernel will be used as the answer to the question.

{rule}

There is some domain knowledge for you:

{background}

Your response should follow the Python block format below:

{format}"""

code_format = """```python
(YOUR CODE HERE)
```"""

summary_template = """The code execution is successful. The execution result is shown below:

{result}

Please summarize a straightforward answer to the question based on the execution results. Use plain English ONLY. Do NOT write Python code. Do NOT use code blocks. Your response must be natural language text."""

conclusion_template = """{answer}

The original code execution output of IPython Kernel is also provided below for reference:

{result}"""
