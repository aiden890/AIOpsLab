# Tree Traversal Deep Dive / Expand Status

## 1. Current Pipeline Structure

The pipeline currently runs in this order:

```text
Localization
  -> Iteration 1: Deep Dive
  -> Iteration 1: Expand
  -> Iteration 2: Deep Dive
  -> Iteration 2: Expand
  -> ...
  -> Global Judge
```

Implementation:
- `clients/tree_traversal/staged_rca_pipeline.py`

Main entry points:
- `_run_iterative_deep_dive_expand()`
- `run()`

## 2. Localization

Localization is kept as-is.

Current status:
- Existing outlier extraction flow is preserved.
- Initial search tree candidate creation is preserved.
- Precomputation of trace anomaly edges is preserved.

Current additional behavior:
- `value_is_problematic == false` is not currently used as a hard localization rejection gate.

Implementation:
- `clients/tree_traversal/staged_rca_pipeline.py`

## 3. Deep Dive Module

Deep Dive is separated into its own file.

Implementation:
- `clients/tree_traversal/deep_dive_stage.py`

Contents:
- `DeepDiveOutcome` dataclass
- CSV preview/head builder
- column meaning descriptions
- KPI catalog builder by component level
- controller loop runner

Current Deep Dive input:
- component
- component level
- localized time
- localized reason hint
- localized evidence
- available KPI catalog
- executor telemetry preview
- allowed reason list

Current Deep Dive output:
- `verdict`
  - `confirmed`
  - `needs_expand`
  - `check_edges`
  - `symptom`
  - `noise`
- `reason`
- `time`
- `explanation`
- `checked_reasons`
- `next_kpis`
- `edge_targets`

## 4. Executor Telemetry Context in Deep Dive

The executor prompt is given local telemetry previews automatically.

CSV sources:
- `metric_node.csv`
- `metric_container.csv`
- `metric_service.csv`
- `metric_app.csv`
- `metric_mesh.csv`
- `trace_span.csv`
- first available `logs/*.csv`

For each CSV, the prompt includes:
- column names
- column meanings
- head preview

Implementation:
- `build_executor_telemetry_context()` in `clients/tree_traversal/deep_dive_stage.py`

## 5. Current Deep Dive Behavior

Current intended behavior:
- `execute()` retrieves minute-bucket raw tables only.
- comparison and interpretation are done by the controller after reading execute output.
- the controller can iterate multiple times before it submits a verdict.

The controller is expected to compare:
- target component vs peers or siblings
- pre-incident baseline vs incident window
- trace edge or path context when necessary

## 6. Controller Stage Changes

Common controller-loop behavior has been tightened.

Implementation:
- `clients/tree_traversal/controller_stage.py`

Current behavior:
- only `execute` and `submit` are allowed in `deep_dive` and `expand`
- at least one `execute()` must happen before `submit`
- `execute` without `instruction` is rejected
- top-level `instruction`, `analysis`, `query`, `task`, and `prompt` can be normalized into `args["instruction"]`

## 7. Expand Module

Expand is also separated into its own file.

Implementation:
- `clients/tree_traversal/expand_stage.py`

Contents:
- `ExpandOutcome` dataclass
- expand controller prompt
- grouped candidate-batch execution wrapper

Current intended behavior:
- `execute()` retrieves minute-bucket raw tables
- controller compares the candidates using those raw results
- only candidates worth another Deep Dive are returned

## 8. Expand Candidate Sources

Expand currently merges candidates from:
- trace relation candidates
- deployment relation candidates
- topology relation candidates
- `deep_dive.edge_targets`

Implementation:
- `_get_trace_relation_candidates()`
- `_get_deployment_relation_candidates()`
- `_get_topology_relation_candidates()`
- `_build_deep_dive_target_relation_map()`

Effect:
- if Deep Dive says "inspect this edge/component next", Expand can actually include it in the next candidate batch

## 9. Current Expand Filtering

Current behavior:
- trace-edge anomalies are no longer auto-promoted into expand candidates
- the controller must inspect and return candidates explicitly
- edge-related candidates can still move forward even when they are not strong self-anomalies, depending on relation family

Relation families that currently help forward edge-related candidates:
- `trace_call`
- `graph_call`
- `deep_dive_target`

Current intent:
- keep components with their own anomaly signal
- keep plausible path or predecessor candidates
- drop clear symptom or noise candidates

## 10. Search Tree Extensions

Search tree nodes now store more deep-dive and relation context.

Implementation:
- `clients/tree_traversal/rca_search_tree.py`

Added node fields:
- `deep_dive_verdict`
- `deep_dive_next_kpis`
- `deep_dive_edge_targets`
- `related_parent_ids`
- `related_parent_components`

Relation edge storage includes:
- source node
- destination node
- relation family
- relation type
- anomaly type
- time
- metadata

Containment group storage includes:
- container component
- member node ids
- relation metadata

## 11. Tree Summary Passed into Expand

Expand receives a tree summary that currently includes:
- search tree nodes
- stage, status, time, confidence
- parent
- related parents
- deep dive verdict
- deep dive reason
- relation edges
- containment groups

This means Expand sees:
- current hypothesis
- prior search tree history
- structural relation context
- multi-parent context

## 12. Market Name Normalization

A Market-specific normalization path was added for names like `os_node-6`.

Current behavior:
- Market `os_node-*` can be normalized to `node-*` when needed for expand/dependency lookup

Implementation:
- `clients/tree_traversal/staged_rca_pipeline.py`

## 13. Current Limitations

Known limitations at the current stage:
- prompt intent is updated, but real LLM behavior still needs rerun validation
- `checked_reasons` is still model-generated reasoning trace and may vary in quality
- expand filtering policy is improved but not fully finalized
- end-to-end behavior should still be verified on actual tasks and session logs

## 14. One-Line Summary

The current implementation already has:
- Localization preserved
- Deep Dive as an iterative module
- Expand as an iterative module
- Deep Dive and Expand alternating in a loop
- Deep Dive-produced edge targets feeding into Expand
- Search tree storage for relation and multi-parent context
