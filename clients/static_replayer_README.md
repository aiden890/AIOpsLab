# Static Telemetry Replayer Client

Unified interface for static dataset replay with two modes:
1. **Standalone Mode**: Direct deployment of static replayers
2. **Problem Mode**: Orchestrator-integrated problem execution with agents

---

## 🚀 Quick Start

### Standalone Mode

```bash
# List datasets
python clients/static_replayer.py --list

# Deploy dataset
python clients/static_replayer.py --deploy openrca_bank
```

### Problem Mode (with Agent)

```bash
# List problems
python clients/static_replayer.py --list-problems

# Run problem with ReAct agent
python clients/static_replayer.py --run-problem openrca-bank-detection-0

# Run all problems
python clients/static_replayer.py --run-all-problems
```

---

## 📋 Standalone Mode

Deploy static datasets directly without agent integration.

### List Available Datasets

```bash
python clients/static_replayer.py --list
```

Output:
```
Available Dataset Configurations
==============================================================

1. openrca_bank
   Dataset: OpenRCA Bank
   Type: openrca
   Namespace: static-bank
   Telemetry: trace, log, metric

2. openrca_telecom
   Dataset: OpenRCA Telecom
   ...
```

### Deploy Single Dataset

```bash
python clients/static_replayer.py --deploy openrca_bank
```

This will:
- Start Docker infrastructure (ES, Prometheus, Jaeger)
- Parse query.csv and record.csv
- Calculate time remapping
- Bulk load 30min history
- Start realtime replay

### Deploy Multiple Datasets

```bash
python clients/static_replayer.py --deploy openrca_bank openrca_telecom
```

### Deploy All OpenRCA

```bash
python clients/static_replayer.py --deploy-all-openrca
```

---

## 🎯 Problem Mode (Orchestrator Integration)

Run static problems with agents through the Orchestrator.

### List Available Problems

```bash
python clients/static_replayer.py --list-problems
```

Output:
```
Available Static Problems
==============================================================

1. openrca-bank-detection-0
   Name: OpenRCA Bank Detection
   Dataset: OpenRCA Bank
   Task: Detection

2. openrca-telecom-detection-0
   Name: OpenRCA Telecom Detection
   Dataset: OpenRCA Telecom
   Task: Detection
...
```

### Run Single Problem

```bash
python clients/static_replayer.py --run-problem openrca-bank-detection-0
```

This will:
1. Initialize OpenRCABankDetection problem
2. Deploy StaticReplayer (replays historical data)
3. Start ReAct agent
4. Agent analyzes telemetry and detects fault
5. Evaluate agent's answer against expected solution
6. Save results to `results/static_replayer/`

### Run with Custom Agent

```bash
python clients/static_replayer.py --run-problem openrca-bank-detection-0 \
    --agent react \
    --max-steps 30
```

### Run All Problems

```bash
python clients/static_replayer.py --run-all-problems
```

Runs all 4 OpenRCA detection problems sequentially.

---

## 📊 Results

### Standalone Mode Results

```
results/static_replayer/
├── openrca_bank_1707512345.json
└── batch_1707512567.json
```

Format:
```json
{
  "config_name": "openrca_bank",
  "dataset_name": "OpenRCA Bank",
  "status": "deployed",
  "deploy_time_seconds": 45.2,
  "time_mapping": {...},
  "query_info": {...}
}
```

### Problem Mode Results

```
results/static_replayer/
├── openrca-bank-detection-0_react_1707512456.json
└── all_problems_react_1707512567.json
```

Format:
```json
{
  "problem_id": "openrca-bank-detection-0",
  "problem_name": "OpenRCA Bank Detection",
  "agent": "react",
  "duration_seconds": 120.5,
  "results": {
    "success": true,
    "Detection Accuracy": "Correct",
    "Dataset": "OpenRCA Bank",
    "Task ID": "task_1"
  }
}
```

---

## 🔄 Mode Comparison

| Feature | Standalone | Problem Mode |
|---------|-----------|--------------|
| **Use Case** | Direct data replay | Agent evaluation |
| **Orchestrator** | ❌ | ✅ |
| **Agent Integration** | ❌ | ✅ |
| **Automatic Evaluation** | ❌ | ✅ |
| **Session Tracking** | ❌ | ✅ |
| **Result Format** | Simple | Standard Task format |
| **Speed** | Faster | Slower (agent overhead) |

---

## 💡 Usage Examples

### Example 1: Quick Dataset Testing

```bash
# Deploy dataset and check Jaeger UI
python clients/static_replayer.py --deploy openrca_bank
open http://localhost:16686

# Cleanup
python clients/static_replayer.py --cleanup
```

### Example 2: Agent Benchmarking

```bash
# Run all problems with ReAct
python clients/static_replayer.py --run-all-problems --agent react

# Check results
cat results/static_replayer/all_problems_react_*.json
```

### Example 3: Mixed Usage

```python
from clients.static_replayer import StaticReplayerClient

client = StaticReplayerClient()

# Standalone: Deploy for manual analysis
client.deploy("openrca_bank")
# ... do your analysis
client.cleanup("openrca_bank")

# Problem: Run with agent
client.run_problem("openrca-bank-detection-0")
```

### Example 4: Custom Agent

```python
# Modify clients/react.py or create new agent
# Then use with problem mode

from clients.static_replayer import StaticReplayerClient
client = StaticReplayerClient()
client.run_problem("openrca-bank-detection-0", agent_name="my_custom_agent")
```

---

## 🛠️ Advanced Options

### Custom Results Directory

```bash
python clients/static_replayer.py --results-dir ./my_results --deploy openrca_bank
```

### Problem with More Steps

```bash
python clients/static_replayer.py --run-problem openrca-bank-detection-0 --max-steps 50
```

---

## 🔍 Troubleshooting

### Standalone Mode Issues

```bash
# Check Docker services
docker ps

# Manually start infrastructure
cd aiopslab/service/apps/static_replayer/docker
docker-compose up -d

# Check logs
docker-compose logs -f
```

### Problem Mode Issues

```bash
# Check if problem classes are importable
python -c "from aiopslab.orchestrator.static_problems import OpenRCABankDetection; print('OK')"

# Check agent
python -c "from clients.react import Agent; print('OK')"

# Check orchestrator
python -c "from aiopslab.orchestrator import Orchestrator; print('OK')"
```

---

## 📚 Related Files

- **Static Problems**: `aiopslab/orchestrator/static_problems/openrca_detection.py`
- **Replayer Core**: `aiopslab/service/apps/static_replayer/replayer.py`
- **React Agent**: `clients/react.py`
- **React Static**: `clients/react_static.py` (alternative problem runner)

---

## 🎯 When to Use Which Mode

### Use Standalone Mode When:
- Quick dataset exploration
- Manual data analysis
- Testing replayer functionality
- Developing new features
- No agent evaluation needed

### Use Problem Mode When:
- Benchmarking agents
- Reproducible evaluation
- Standard metrics needed
- Session tracking required
- Comparing multiple agents

---

## 📖 Full Command Reference

```bash
# Standalone Mode
--list                      List datasets
--deploy CONFIG [CONFIG ...]Deploy one or more datasets
--deploy-all-openrca       Deploy all OpenRCA datasets

# Problem Mode
--list-problems            List static problems
--run-problem PROBLEM_ID   Run specific problem
--run-all-problems         Run all problems
--agent AGENT              Agent to use (default: react)
--max-steps N              Max steps (default: 30)

# Common
--cleanup [CONFIG]         Cleanup (all or specific)
--results-dir DIR          Custom results directory
```

---

**Both modes are fully supported and can be used interchangeably based on your needs!** 🚀
