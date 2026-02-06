# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Prompt templates for OpenRCA tasks."""

# Standard OpenRCA documentation template
OPENRCA_DOCS = """{prob_desc}

You are provided with the following APIs to interact with the telemetry data:

{telemetry_apis}

Finally, you will submit your solution for this task using the following API:

{submit_api}

## POSSIBLE ROOT CAUSE COMPONENTS:
{candidate_components}

## POSSIBLE ROOT CAUSE REASONS:
{candidate_reasons}

At each turn think step-by-step and respond with:
Thought: <your thought>
Action: <your action>

IMPORTANT: Use triple backticks WITHOUT language identifiers for code blocks.
Correct:
```
get_metric_container(start_time="2021-03-12 12:00:00", end_time="2021-03-12 12:30:00")
```

Incorrect (DO NOT use language identifiers like 'python'):
```python
get_metric_container(...)
```
"""

# Response instructions for OpenRCA
OPENRCA_RESP_INSTR = """DO NOT REPEAT ACTIONS! Respond with:
Thought: <your thought on the previous output>
Action: <your action towards identifying the root cause>
"""

# Instructions template for OpenRCA tasks
OPENRCA_INSTRUCTIONS = """
Analyze the telemetry data using the provided APIs and identify the root cause.

Submit format:
```
submit({
    "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS",
    "root cause component": "<component_name>",
    "root cause reason": "<reason>"
})
```

Note: Depending on the task type, not all fields may be required:
- task_1: only "root cause occurrence datetime"
- task_2: only "root cause reason"
- task_3: only "root cause component"
- task_4: "root cause occurrence datetime" + "root cause reason"
- task_5: "root cause occurrence datetime" + "root cause component"
- task_6: "root cause component" + "root cause reason"
- task_7: all three fields

IMPORTANT: All API calls must be written inside a markdown code block.
"""
