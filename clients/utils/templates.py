# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""prompt templates to share API documentation and instructions with clients"""

# standard documentation and apis template

DOCS = """SERVICE MONITORING TASK

{prob_desc}

Available monitoring APIs:

{telemetry_apis}

Diagnostic interface:

{shell_api}

Submission API:

{submit_api}

At each turn, respond with:
Thought: <your analysis>
Action: <your next step>
"""


### custom templates

DOCS_WITH_POSSIBLE_ROOT_CAUSES = """SERVICE MONITORING TASK

{prob_desc}

Available monitoring APIs:

{telemetry_apis}

Diagnostic interface:

{shell_api}

Submission API:

{submit_api}

{possible_root_causes}

At each turn, you should respond with:
Thought: <your analysis>
Action: <your next step>
"""

DOCS_SHELL_ONLY = """{prob_desc}
You are provided with a diagnostic interface for the service:

{shell_api}

Finally, you will submit your solution for this task using the following API:

{submit_api}

IMPORTANT:
1. The submit() call must strictly follow its defined parameter signature for this task.
2. Provide the call in a markdown code block.

At each turn respond with:
Action: <your action>
"""

AUTOGEN_DOCS = """{prob_desc}
You are provided with the following APIs to interact with the service:

{telemetry_apis}

You also have access to a diagnostic interface for the service:

{shell_api}

Finally, you will submit your solution for this task using the following API:

{submit_api}

Collaborate with your team to analyze the problem and suggest appropriate API calls.
Suggest API calls in the specified format within markdown code blocks.
"""
