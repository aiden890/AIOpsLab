# Static Telemetry Replayer

Replay static datasets (OpenRCA, Alibaba, ACME) as real-time telemetry with query-based time remapping and history bulk loading.

## Quick Start

```python
from aiopslab.service.apps.static_replayer import StaticReplayer

# Deploy OpenRCA Bank dataset
replayer = StaticReplayer("openrca_bank")
replayer.deploy()

# Data is now streaming to Elasticsearch, Prometheus, and Jaeger
# Access via:
# - Elasticsearch: http://localhost:9200
# - Prometheus: http://localhost:9090
# - Jaeger UI: http://localhost:16686

# Cleanup when done
replayer.cleanup()
```

## Features

- ✅ **Multiple Dataset Support**: OpenRCA, Alibaba, ACME (extensible)
- ✅ **Query-Based Time Remapping**: Automatically map fault times to current time
- ✅ **History Bulk Loading**: Pre-load 30min history in ~30 seconds
- ✅ **Selective Telemetry**: Enable/disable traces, logs, metrics independently
- ✅ **Speed Control**: Replay at 1x, 10x, or any speed factor

## Available Datasets

### OpenRCA

- `openrca_bank` - Bank dataset with 4 days of data
- `openrca_telecom` - Telecom dataset with 13 days
- `openrca_market_cloudbed1` - Market cloudbed-1 with mesh metrics
- `openrca_market_cloudbed2` - Market cloudbed-2 with mesh metrics

## Configuration

Each dataset has a JSON config file in `config/`. Key settings:

```json
{
  "telemetry": {
    "enable_trace": true,
    "enable_log": true,
    "enable_metric": true
  },
  "time_mapping": {
    "mode": "realtime",  // or "manual"
    "anchor_strategy": "fault_start",
    "history_duration_seconds": 1800
  },
  "replay_config": {
    "speed_factor": 1.0  // 1.0 = realtime, 10.0 = 10x faster
  }
}
```

## Advanced Usage

### 10x Speed Replay

```python
# Edit config to set speed_factor: 10.0
replayer = StaticReplayer("openrca_bank")
replayer.deploy()
# 30 minutes of data replays in 3 minutes
```

### Manual Time Control

```json
{
  "time_mapping": {
    "mode": "manual",
    "simulation_start_time": "2026-02-10T10:00:00Z"
  }
}
```

### Metrics Only

```json
{
  "telemetry": {
    "enable_trace": false,
    "enable_log": false,
    "enable_metric": true
  }
}
```

## Architecture

See [PROPOSAL.md](PROPOSAL.md) for full design documentation.

## Troubleshooting

**Services not starting?**
```bash
cd docker/
docker-compose up -d
docker-compose ps
```

**Check service health:**
```bash
curl http://localhost:9200  # Elasticsearch
curl http://localhost:9090  # Prometheus
curl http://localhost:16686  # Jaeger
```

**View logs:**
```bash
docker-compose logs -f
```

## Adding New Datasets

1. Create adapter in `adapters/my_dataset.py`
2. Create query parser in `time_mapping/my_dataset_query_parser.py`
3. Create config in `config/my_dataset.json`
4. Use it: `StaticReplayer("my_dataset")`

See [PROPOSAL.md](PROPOSAL.md) for detailed instructions.
