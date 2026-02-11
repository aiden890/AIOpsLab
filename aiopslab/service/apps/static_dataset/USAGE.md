# Static Telemetry Replayer - Usage Guide

## Quick Start

### Using the Client (Recommended)

```bash
# List available datasets
python clients/static_replayer.py --list

# Deploy single dataset
python clients/static_replayer.py --deploy openrca_bank

# Deploy all OpenRCA datasets
python clients/static_replayer.py --deploy-all-openrca

# Cleanup
python clients/static_replayer.py --cleanup
```

### Using Python API

```python
from aiopslab.service.apps.static_replayer import StaticReplayer

# Create replayer
replayer = StaticReplayer("openrca_bank")

# Deploy (starts infrastructure + bulk load + realtime replay)
replayer.deploy()

# Data is now streaming to:
# - Elasticsearch: http://localhost:9200
# - Prometheus: http://localhost:9090
# - Jaeger: http://localhost:16686

# Cleanup when done
replayer.cleanup()
```

---

## Client Options

### List Configurations

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
   Type: openrca
   Namespace: static-telecom
   Telemetry: trace, log, metric
...
```

### Deploy Single Dataset

```bash
python clients/static_replayer.py --deploy openrca_bank
```

This will:
1. Start Docker infrastructure (Elasticsearch, Prometheus, Jaeger)
2. Parse query.csv and record.csv
3. Calculate time remapping
4. Bulk load 30min history (~30 seconds)
5. Start realtime replay

### Deploy Multiple Datasets

```bash
python clients/static_replayer.py --deploy openrca_bank openrca_telecom
```

Runs sequentially with cleanup between each.

### Deploy All OpenRCA Datasets

```bash
python clients/static_replayer.py --deploy-all-openrca
```

Deploys all 4 OpenRCA datasets:
- openrca_bank
- openrca_telecom
- openrca_market_cloudbed1
- openrca_market_cloudbed2

### Cleanup

```bash
# Cleanup all
python clients/static_replayer.py --cleanup

# Cleanup specific dataset
python clients/static_replayer.py --cleanup openrca_bank
```

---

## Python API Usage

### Basic Usage

```python
from aiopslab.service.apps.static_replayer import StaticReplayer

replayer = StaticReplayer("openrca_bank")
replayer.deploy()

# Your analysis code here
# ...

replayer.cleanup()
```

### Access Deployed Services

```python
replayer = StaticReplayer("openrca_bank")
replayer.deploy()

# Access Elasticsearch
from elasticsearch import Elasticsearch
es = Elasticsearch(["http://localhost:9200"])
logs = es.search(index="logstash-static-bank-*")

# Access Prometheus
import requests
resp = requests.get("http://localhost:9090/api/v1/query", params={'query': 'up'})

# Access Jaeger
# Open browser: http://localhost:16686
```

### Get Time Mapping Info

```python
replayer = StaticReplayer("openrca_bank")
replayer.deploy()

# Print time mapping summary
if replayer.time_remapper:
    print(replayer.time_remapper.get_summary())

    # Get mapping dict
    mapping = replayer.time_remapper.mapping
    print(f"Original fault time: {mapping['anchor_original']}")
    print(f"Simulation fault time: {mapping['anchor_simulation']}")
```

### Get Query Info

```python
replayer = StaticReplayer("openrca_bank")

if replayer.query_info:
    print(f"Task ID: {replayer.query_info.task_id}")
    print(f"Time range: {replayer.query_info.time_range}")
    print(f"Faults: {replayer.query_info.faults}")
```

---

## Advanced Usage

### Custom Speed Factor

Edit config file:
```json
{
  "replay_config": {
    "speed_factor": 10.0
  }
}
```

Then:
```bash
python clients/static_replayer.py --deploy openrca_bank
# 30 minutes of data replays in 3 minutes
```

### Manual Time Control

Edit config file:
```json
{
  "time_mapping": {
    "mode": "manual",
    "simulation_start_time": "2026-02-10T10:00:00Z"
  }
}
```

### Disable Telemetry Types

Edit config file:
```json
{
  "telemetry": {
    "enable_trace": false,
    "enable_log": true,
    "enable_metric": true
  }
}
```

### Disable Bulk History

Edit config file:
```json
{
  "time_mapping": {
    "enable_bulk_history": false
  }
}
```

---

## Integration with AIOpsLab Observer

```python
from aiopslab.service.apps.static_replayer import StaticReplayer
from aiopslab.observer.observe import collect_traces, collect_logs, collect_metrics

# Deploy replayer
replayer = StaticReplayer("openrca_bank")
replayer.deploy()

# Wait for some data to be replayed
import time
time.sleep(60)

# Collect data using AIOpsLab observer
if replayer.time_remapper:
    start_time = replayer.time_remapper.mapping['anchor_simulation']
    end_time = start_time + 1800  # 30 minutes

    collect_traces(start_time, end_time)
    collect_logs(start_time, end_time)
    collect_metrics(start_time, end_time)

# Cleanup
replayer.cleanup()
```

---

## Troubleshooting

### Services Not Starting

```bash
# Check Docker
docker ps

# Start manually
cd aiopslab/service/apps/static_replayer/docker
docker-compose up -d

# Check logs
docker-compose logs -f
```

### Check Service Health

```bash
# Elasticsearch
curl http://localhost:9200

# Prometheus
curl http://localhost:9090/-/ready

# Jaeger
curl http://localhost:16686
```

### View Replayed Data

```bash
# Elasticsearch indices
curl http://localhost:9200/_cat/indices?v

# Prometheus metrics
curl http://localhost:9090/api/v1/label/__name__/values

# Jaeger services
curl http://localhost:16686/api/services
```

### Port Conflicts

If ports are already in use, modify `docker-compose.yml`:

```yaml
elasticsearch:
  ports:
    - "9201:9200"  # Changed from 9200

prometheus:
  ports:
    - "9091:9090"  # Changed from 9090
```

---

## Results and Logs

### Results Directory

By default, results are saved to:
```
results/static_replayer/
├── openrca_bank_1707512345.json
├── openrca_telecom_1707512456.json
└── batch_1707512567.json
```

### Result Format

```json
{
  "config_name": "openrca_bank",
  "dataset_name": "OpenRCA Bank",
  "namespace": "static-bank",
  "status": "deployed",
  "deploy_time_seconds": 45.2,
  "deployment_timestamp": "2026-02-09T18:00:00",
  "telemetry": {
    "enable_trace": true,
    "enable_log": true,
    "enable_metric": true
  },
  "time_mapping": {
    "anchor_original": 1614841800,
    "anchor_simulation": 1707512345,
    "time_offset": 92670545
  },
  "query_info": {
    "task_id": "task_1",
    "time_range": {...},
    "faults": [...]
  }
}
```

---

## Examples

See `clients/examples/static_replayer_example.py` for more examples.

```bash
python clients/examples/static_replayer_example.py
```
