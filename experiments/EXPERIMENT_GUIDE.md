# RCA Experiment Guide

## Quick Start

```bash
# Critic agent + gpt-5 low reasoning
python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent critic \
  --api-config clients/openrca_rca/api_config_low.yaml \
  --eval-id gpt5-low-critic

# Original agent + gpt-5 default
python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent original \
  --api-config clients/openrca_rca/api_config.yaml \
  --eval-id gpt5-original
```

## Command Options

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--problems-file` | Yes | - | Problem ID list file (txt, `#` comments supported) |
| `--agent` | No | `critic` | Agent type: `critic` or `original` |
| `--api-config` | No | `api_config.yaml` | LLM API config file path |
| `--eval-id` | Yes | - | Experiment identifier (results subdirectory name) |
| `--results-dir` | No | `results/experiments` | Base results directory |
| `--max-steps` | No | `40` | Max orchestrator steps per problem |
| `--start-index` | No | `0` | Skip first N problems (for resume) |

## Agent Types

| Agent | File | Description |
|-------|------|-------------|
| `original` | `agent.py` | OpenRCA original (Controller + internal Executor) |
| `react` | `react_rca_agent.py` | OpenRCA ReAct (Controller + external Executor) |
| `critic` | `react_rca_critic_agent.py` | OpenRCA ReAct + Critic validation layer |

### Critic Agent Flow
```
Controller -> Analysis -> Critic (validates) -> Instruction -> Executor -> (repeat)
```

### Original Agent Flow
```
Controller -> Instruction -> Executor -> (repeat)
```

## API Config Files

Located in `clients/openrca_rca/`:

| File | Model | Reasoning |
|------|-------|-----------|
| `api_config.yaml` | gpt-5 | default |
| `api_config_low.yaml` | gpt-5 | low |
| `api_config_xhigh.yaml` | gpt-5 | xhigh |

### Custom API Config

```yaml
SOURCE:           "AI"          # AI = Azure OpenAI compatible
MODEL:            "gpt-5"
REASONING_EFFORT: "low"         # optional: low, medium, high, xhigh
# API_KEY and API_BASE loaded from .env
```

## Problem Files

### Default: `experiments/problems_30.txt` (30 tasks)

- Telecom (10): indices 0, 2, 4, 5, 8, 9, 13, 19, 20, 27
- Market cloudbed-1 (10): indices 2, 8, 10, 12, 14, 24, 29, 30, 56, 59
- Bank (10): indices 0, 3, 6, 11, 45, 46, 51, 56, 62, 81

### Custom Problem File Format

```text
# Comments start with #
# Empty lines are ignored

# Dataset: openrca_telecom
openrca_telecom-task_7-0
openrca_telecom-task_7-2

# Dataset: openrca_bank
openrca_bank-task_1-0
```

### Problem ID Format

```
{dataset_key}-{task_index}-{row_index}
```

- `dataset_key`: `openrca_telecom`, `openrca_bank`, `openrca_market_cb1`, `openrca_market_cb2`
- `task_index`: from query.csv `task_index` column (e.g., `task_7`)
- `row_index`: 0-based row number in query.csv

### Finding Problem IDs

```python
# List all available problems for a dataset
python -c "
import sys; sys.path.insert(0, '.')
from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
reg = StaticProblemRegistry()
for pid in reg.get_problem_ids(dataset='openrca_telecom'):
    print(pid)
"
```

## Results Structure

```
results/experiments/{eval_id}/
    scores.csv              # All problems unified (with t/c/r info)
    summary.txt             # Statistics: per-dataset, t/c/r, overall
    openrca_telecom/
        react-rca-critic/
            gpt-5-low/
                {eval_id}/
                    {timestamp}_{task}.log       # Execution log
                    {timestamp}_{task}.json      # Full session data
                    {task}_executor.ipynb        # Executor code notebook
                    static_metrics_output/       # Telemetry cache
    openrca_bank/...
    openrca_market_cb1/...
```

### scores.csv Fields

| Field | Description |
|-------|-------------|
| timestamp | Execution time |
| eval_id | Experiment identifier |
| problem_id | Problem ID |
| dataset | Dataset name |
| task_type | Task type (task_1 ~ task_7) |
| difficulty | easy / middle / hard |
| score | Score (0.0 ~ 1.0) |
| success | True if score == 1.0 |
| steps | Number of steps taken |
| TTA | Time to answer (seconds) |
| in_tokens | Input tokens used |
| out_tokens | Output tokens used |
| passing_criteria | Matched scoring criteria (JSON) |
| failing_criteria | Unmatched scoring criteria (JSON) |
| ground_truth | Expected answer from query.csv |

## W&B Dashboard

### Runs Table Columns (recommended)

| Column | Source | Description |
|--------|--------|-------------|
| `config/agent` | Config | critic / original |
| `config/model` | Config | gpt-5-low etc. |
| `final/success_rate` | Summary | Overall accuracy (%) |
| `final/avg_score` | Summary | Average score |
| `final/t_rate` | Summary | Time accuracy (%) |
| `final/c_rate` | Summary | Component accuracy (%) |
| `final/r_rate` | Summary | Reason accuracy (%) |
| `final/completed` | Summary | Problems completed |

To configure: Click **Columns** icon in Runs Table -> check/uncheck columns.

### Real-time Charts

| Chart | Description |
|-------|-------------|
| `progress/pct` | Progress (%) |
| `accuracy/success_rate` | Running success rate |
| `accuracy/t_rate` | Running time accuracy |
| `accuracy/c_rate` | Running component accuracy |
| `accuracy/r_rate` | Running reason accuracy |

## Task Types & Scoring

| Task | Asks for | Difficulty |
|------|----------|------------|
| task_1 | Time only | easy |
| task_2 | Reason only | easy |
| task_3 | Component only | easy |
| task_4 | Time + Component | middle |
| task_5 | Component + Reason | middle |
| task_6 | Component + Reason | middle |
| task_7 | Time + Component + Reason | hard |

Score = (matched criteria) / (total criteria). Success = score == 1.0.

## Resume a Failed Experiment

If an experiment fails midway, use `--start-index` to skip completed problems:

```bash
# Check how many completed
wc -l results/experiments/gpt5-low-critic/scores.csv
# 16 lines = 15 completed (1 header) -> start from index 15

python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent critic \
  --api-config clients/openrca_rca/api_config_low.yaml \
  --eval-id gpt5-low-critic \
  --start-index 15
```

Note: `--start-index` skips N problems from the problems file, not by problem ID. The scores.csv will append new rows. W&B will continue logging to a new run (same eval_id in config).

## Example Experiment Configurations

```bash
# Comparison: critic vs original (same model)
python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent critic --eval-id gpt5-low-critic \
  --api-config clients/openrca_rca/api_config_low.yaml

python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent original --eval-id gpt5-low-original \
  --api-config clients/openrca_rca/api_config_low.yaml

# Comparison: low vs xhigh reasoning (same agent)
python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent critic --eval-id gpt5-low-critic \
  --api-config clients/openrca_rca/api_config_low.yaml

python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent critic --eval-id gpt5-xhigh-critic \
  --api-config clients/openrca_rca/api_config_xhigh.yaml
```
