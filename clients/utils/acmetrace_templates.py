# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Prompt templates for AcmeTrace Kalos GPU cluster RCA tasks."""

# Standard AcmeTrace documentation template
ACMETRACE_DOCS = """You are an AI assistant helping with GPU cluster root cause analysis.

{prob_desc}

## Available APIs

The following APIs are available to query telemetry data:

{telemetry_apis}

To submit your solution, use:

{submit_api}

## Data Format Reference

### Job Trace Columns (returned by get_jobs, get_failed_jobs)
- job_id: Unique job identifier
- user: Hashed user ID
- node_num: Number of nodes requested
- gpu_num: Number of GPUs requested
- state: Job status — COMPLETED, FAILED, TIMEOUT, NODE_FAIL, CANCELLED
- submit_time: When job was submitted
- start_time: When job started executing
- end_time: When job terminated
- fail_time: When failure occurred (if failed)
- duration: Execution time in seconds (end_time - start_time)
- gpu_time: Total GPU-seconds (duration x gpu_num)

### GPU Metric Columns (returned by get_gpu_util, get_gpu_temp, get_gpu_memory, get_power_usage)
- Time: Timestamp at 15-second intervals
- {{IP}}-{{GPU_INDEX}}: Value per GPU (e.g., 172.31.15.112-6 = GPU #6 on node 172.31.15.112)
- Each node has 8 GPUs (index 0-7)
- get_gpu_util: GPU utilization 0-100%
- get_gpu_temp: Temperature in Celsius
- get_gpu_memory: Frame buffer memory used in MB
- get_power_usage: Power consumption in Watts

### Node Metric Columns (returned by get_node_cpu, get_node_memory)
- Time: Timestamp at 15-second intervals
- {{IP}}: Value per node (e.g., 172.31.0.64)
- Values are 0-100%

### XID Error Columns (returned by get_xid_errors, get_xid_errors_for_job)
- Time: Timestamp at 15-second intervals
- {{IP}}-{{GPU_INDEX}}: XID error code per GPU (0 = no error)
- XID 31: ECC Error (GPU memory page retirement)
- XID 43: GPU fallen off bus (NVLink/PCIe failure) — most common
- XID 45: Preemptive cleanup due to prior errors

## Error Categories
{candidate_categories}

## Possible Error Reasons
{candidate_reasons}

## Response Format
Please analyze the data step by step. Use code blocks (triple backticks) to call APIs.

CRITICAL: You must include EXACTLY ONE code block per response. Do NOT include multiple code blocks.
Call one API at a time, observe the result, then decide the next step.

Example API call:
```
get_gpu_util(start_time="2023-08-15 10:00:00", end_time="2023-08-15 11:00:00")
```
"""

# Response instructions for AcmeTrace
ACMETRACE_RESP_INSTR = """Based on the output above, please continue your analysis and take the next step. Remember: include EXACTLY ONE code block (one API call) in your response."""

# Instructions template for AcmeTrace detection task
ACMETRACE_DETECTION_INSTR = """
Analyze the job and cluster telemetry to determine if the job experienced a failure.

Submit format:
```
submit({"is_failure": True})  # or False
```

IMPORTANT: All API calls must be written inside a markdown code block. Only ONE code block per response.
"""

# Instructions template for AcmeTrace localization task
ACMETRACE_LOCALIZATION_INSTR = """
Analyze the job and cluster telemetry to identify which node/GPU experienced the failure.

Submit format:
```
submit({
    "affected_node": "172.31.15.112",
    "affected_gpu": "172.31.15.112-6"
})
```

Note: You may provide either or both fields depending on what you can determine.

IMPORTANT: All API calls must be written inside a markdown code block. Only ONE code block per response.
"""

# Instructions template for AcmeTrace analysis task
ACMETRACE_ANALYSIS_INSTR = """
Analyze the job and cluster telemetry to determine the root cause category and reason.

Submit format:
```
submit({
    "category": "Infrastructure",
    "reason": "NVLink Error"
})
```

Categories: Infrastructure, Framework, Script

IMPORTANT: All API calls must be written inside a markdown code block. Only ONE code block per response.
"""

# Candidate categories
CANDIDATE_CATEGORIES = "Infrastructure, Framework, Script"

# Candidate reasons by category
CANDIDATE_REASONS = """
### Infrastructure:
NVLink Error, CUDA Error, Node Failure, ECC Error, Network Error, Connection Error, S3 Storage Error, NCCL Timeout, NCCL Remote Error

### Framework:
Dataloader Killed, Attribute Error, Out of Memory, Runtime Error, Assertion Error, Value Error

### Script:
File Not Found, OS Error, Type Error, Name Error, Permission Error, Import Error, Key Error, Syntax Error, Index Error
"""
