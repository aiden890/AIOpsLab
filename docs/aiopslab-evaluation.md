# AIOpsLab Evaluation Documentation

This document explains the evaluation system in AIOpsLab, including quantitative metrics and LLM-as-Judge qualitative evaluation.

---

## Overview

AIOpsLab provides two types of evaluation:

| Type | Description | Always Enabled |
|------|-------------|----------------|
| **Quantitative** | Objective metrics (time, steps, tokens, accuracy) | Yes |
| **Qualitative** | LLM-as-Judge reasoning quality score | Configurable |

### Evaluation Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    Agent Session Complete                        │
│                         submit()                                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Problem.eval()                               │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │              Task-Specific Evaluation                      │  │
│  │  - Detection: Check if "Yes"/"No" is correct               │  │
│  │  - Localization: Check if services match                   │  │
│  │  - Analysis: Check system_level and fault_type             │  │
│  │  - Mitigation: Check if pods are healthy                   │  │
│  └───────────────────────────────────────────────────────────┘  │
│                           │                                      │
│                           ▼                                      │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │              common_eval() (Base Task)                     │  │
│  │  ┌─────────────────┐    ┌─────────────────────────────┐   │  │
│  │  │  Quantitative   │    │  Qualitative (Optional)     │   │  │
│  │  │  - steps        │    │  - LLMJudge                 │   │  │
│  │  │  - in_tokens    │    │  - reasoning_score (1-10)   │   │  │
│  │  │  - out_tokens   │    │  - reasoning_judgement      │   │  │
│  │  └─────────────────┘    └─────────────────────────────┘   │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │   Results   │
                    │   (dict)    │
                    └─────────────┘
```

---

## File Structure

```
aiopslab/orchestrator/evaluators/
├── __init__.py
├── quantitative.py    # Quantitative metrics (steps, tokens, accuracy)
├── qualitative.py     # LLMJudge class (LLM-as-Judge)
└── prompts.py         # Prompts for LLM judge
```

---

## Quantitative Evaluation

**File:** `aiopslab/orchestrator/evaluators/quantitative.py`

### Metrics

| Metric | Function | Description |
|--------|----------|-------------|
| `steps` | `num_steps_taken(trace)` | Number of agent actions |
| `in_tokens` | `in_tokens(trace)` | Input tokens consumed |
| `out_tokens` | `out_tokens(trace)` | Output tokens generated |

### Helper Functions

| Function | Description |
|----------|-------------|
| `is_exact_match(pred, target)` | Check if prediction exactly matches target |
| `is_exact_match_lower(pred, target)` | Case-insensitive exact match |
| `is_in_range(pred, target, tolerance)` | Check if prediction is within range |
| `is_subset(pred, target)` | Check if prediction is subset of target |
| `is_superset(pred, target)` | Check if prediction is superset of target |

### Code

```python
from aiopslab.orchestrator.evaluators.quantitative import *

# Count steps
steps = num_steps_taken(trace)  # Number of assistant messages

# Count tokens (using tiktoken)
input_tokens = in_tokens(trace)   # Tokens from env/user
output_tokens = out_tokens(trace)  # Tokens from agent

# Accuracy checks
is_exact_match(["geo"], ["geo"])  # True
is_exact_match(["geo"], "geo")    # True (normalized)
is_subset(["geo"], ["geo", "rate", "profile"])  # True
```

---

## Task-Specific Evaluation

Each task type has its own evaluation logic based on the expected solution.

### Detection Evaluation

```python
def eval(self, soln, trace, duration):
    expected_solution = "Yes"  # or "No" depending on problem

    if soln.strip().lower() == expected_solution.lower():
        self.add_result("Detection Accuracy", "Correct")
    else:
        self.add_result("Detection Accuracy", "Incorrect")

    self.add_result("TTD", duration)  # Time To Detect
    self.common_eval(trace)
```

### Localization Evaluation

```python
def eval(self, soln, trace, duration):
    # Check exact match
    is_exact = is_exact_match(soln, self.faulty_service)

    # Check if solution is superset (contains the faulty service)
    is_sub = is_subset([self.faulty_service], soln)

    if is_exact:
        accuracy = 100.0
    elif is_sub:
        accuracy = (len([self.faulty_service]) / len(soln)) * 100.0
    else:
        accuracy = 0.0

    self.add_result("Localization Accuracy", accuracy)
    self.add_result("TTL", duration)  # Time To Localize
    self.results["success"] = is_exact or (is_sub and len(soln) == 1)
```

### Analysis Evaluation

```python
def eval(self, soln, trace, duration):
    expected_sys_level = "Application"
    expected_fault_type = "Authentication Issue"

    is_sys_level_correct = soln.get("system_level") == expected_sys_level
    is_fault_type_correct = soln.get("fault_type") == expected_fault_type

    self.add_result("TTA", duration)  # Time To Analyze
    self.results["success"] = is_sys_level_correct and is_fault_type_correct
```

### Mitigation Evaluation

```python
def eval(self, soln, trace, duration):
    # Check if all pods are healthy
    pod_list = self.kubectl.list_pods(self.namespace)
    all_normal = True

    for pod in pod_list.items:
        for container_status in pod.status.container_statuses:
            if container_status.state.waiting and \
               container_status.state.waiting.reason == "CrashLoopBackOff":
                all_normal = False
            elif not container_status.ready:
                all_normal = False

    self.add_result("TTM", duration)  # Time To Mitigate
    self.results["success"] = all_normal
```

---

## Qualitative Evaluation (LLM-as-Judge)

**File:** `aiopslab/orchestrator/evaluators/qualitative.py`

### LLMJudge Class

```python
class LLMJudge:
    """A LLM as a judge that evaluates the quality of a solution."""

    def __init__(self, trace: list[SessionItem]):
        self.trace = trace
        self.llm = GPT4Turbo()

    def reasoning_score(self) -> tuple[int, str]:
        """Generate a 1-10 score based on the agent's response."""
        # Returns (score, judgement_text)
```

### How It Works

1. **Format Trace**: Convert session items to readable format
2. **Send to GPT-4**: Ask GPT-4 Turbo to evaluate the agent's reasoning
3. **Parse Score**: Extract the [[rating]] from the response
4. **Return**: Score (1-10) and judgement text

### LLM Judge Prompt

**File:** `aiopslab/orchestrator/evaluators/prompts.py`

```python
SCORE_SYSTEM = """Please act as an impartial judge and evaluate the quality
of the response provided by an AI assistant towards a Service Operations task
displayed below.

Your evaluation should consider factors such as the helpfulness, relevance,
accuracy, depth, creativity, and level of detail of the response.

Begin your evaluation by providing a short explanation. Be as objective as possible.

After providing your explanation, you must rate the response on a scale of 1 to 10
by strictly following this format: "[[rating]]", for example: "Rating: [[5]]".
"""

SCORE_TASK = """<|The Start of Assistant A's Interaction with Service|>

{trace}

<|The End of Assistant A's Interaction with Service|>"
"""
```

### Evaluation Criteria

The LLM judge evaluates:

| Criteria | Description |
|----------|-------------|
| **Helpfulness** | Did the agent make progress toward solving the problem? |
| **Relevance** | Were the actions relevant to the task? |
| **Accuracy** | Were the agent's observations and conclusions correct? |
| **Depth** | Did the agent investigate thoroughly? |
| **Creativity** | Did the agent try alternative approaches when stuck? |
| **Detail** | Were the agent's responses well-reasoned? |

### Score Scale

| Score | Meaning |
|-------|---------|
| 1-2 | Very poor - No useful actions taken |
| 3-4 | Poor - Some attempts but mostly wrong |
| 5-6 | Average - Partial progress, some errors |
| 7-8 | Good - Correct approach, minor issues |
| 9-10 | Excellent - Efficient and accurate solution |

---

## Enabling/Disabling Qualitative Evaluation

**File:** `config.yml`

```yaml
# Flag to enable/disable qualitative evaluation (makes LLM calls)
qualitative_eval: false  # Set to true to enable LLM-as-Judge
```

### Why Disable?

- **Cost**: Each evaluation requires GPT-4 API calls
- **Speed**: LLM calls add latency
- **Reproducibility**: LLM outputs can vary slightly

### When to Enable

- Final benchmark evaluation
- Comparing agent reasoning quality
- Detailed analysis of agent behavior

---

## Using Evaluation in Code

### In Task Base Class

```python
# aiopslab/orchestrator/tasks/base.py

def common_eval(self, trace: list[SessionItem]):
    """Common evaluation function across tasks."""

    # Always run quantitative evaluation
    self.add_result("steps", num_steps_taken(trace))
    self.add_result("in_tokens", in_tokens(trace))
    self.add_result("out_tokens", out_tokens(trace))

    # Optionally run qualitative evaluation
    if config.get("qualitative_eval"):
        judge = LLMJudge(trace)
        score, judgement = judge.reasoning_score()
        self.add_result("reasoning_judgement", judgement)
        self.add_result("reasoning_score", score)
```

### In Problem Definition

```python
class MyProblemMitigation(MitigationTask):
    def eval(self, soln, trace, duration):
        # Task-specific time metric
        self.add_result("TTM", duration)

        # Task-specific success check
        all_normal = self._check_pods_healthy()
        self.add_result("success", all_normal)

        # Call common evaluation (quantitative + optional qualitative)
        self.common_eval(trace)

        return self.results
```

---

## Result Dictionary

After evaluation, `self.results` contains:

```python
{
    # Time metrics (task-specific)
    "TTD": 45.2,  # or TTL, TTA, TTM

    # Task-specific metrics
    "success": True,
    "Detection Accuracy": "Correct",  # or Localization Accuracy, etc.

    # Quantitative metrics (always)
    "steps": 12,
    "in_tokens": 5432,
    "out_tokens": 1234,

    # Qualitative metrics (if enabled)
    "reasoning_score": 8,
    "reasoning_judgement": "The agent showed good systematic approach..."
}
```

---

## Example: Complete Evaluation Output

```json
{
    "agent": "react",
    "session_id": "abc123",
    "problem_id": "pod_failure_mitigation_1",
    "start_time": 1699000000.0,
    "end_time": 1699000045.2,
    "results": {
        "TTM": 45.2,
        "success": true,
        "steps": 8,
        "in_tokens": 4521,
        "out_tokens": 892,
        "reasoning_score": 7,
        "reasoning_judgement": "The agent correctly identified the pod failure through kubectl commands and successfully restarted the affected pod. The approach was systematic but could have been more efficient by checking pod status earlier. Rating: [[7]]"
    }
}
```

---

## Testing the LLM Judge

**File:** `tests/judge/test_judge.py`

```python
import unittest
from aiopslab.orchestrator.evaluators.qualitative import LLMJudge
from aiopslab.session import SessionItem

class TestLLMJudge(unittest.TestCase):
    def setUp(self):
        self.trace = [
            SessionItem(role="user", content="Hello"),
            SessionItem(role="assistant", content="Hi there!"),
        ]
        self.judge = LLMJudge(trace=self.trace)

    def test_reasoning_score(self, MockOpenAI):
        # Mock the OpenAI response
        mock_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="Rating: [[5]]"))
        ]
        result = self.judge.reasoning_score()
        self.assertEqual(result, (5, "Rating: [[5]]"))
```

Run tests:

```bash
python -m pytest tests/judge/test_judge.py
```

---

## Summary: Evaluation Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `TTD` | Quantitative | Time To Detect (seconds) |
| `TTL` | Quantitative | Time To Localize (seconds) |
| `TTA` | Quantitative | Time To Analyze (seconds) |
| `TTM` | Quantitative | Time To Mitigate (seconds) |
| `steps` | Quantitative | Number of agent actions |
| `in_tokens` | Quantitative | Input tokens consumed |
| `out_tokens` | Quantitative | Output tokens generated |
| `success` | Quantitative | Boolean - did agent succeed? |
| `Detection Accuracy` | Quantitative | Correct/Incorrect |
| `Localization Accuracy` | Quantitative | Percentage (0-100%) |
| `reasoning_score` | Qualitative | LLM judge score (1-10) |
| `reasoning_judgement` | Qualitative | LLM judge explanation |
