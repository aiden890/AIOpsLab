"""Unified actions for OpenRCA static dataset tasks.

All 7 task types use the same submit format (JSON dict).
"""

import re as _re
import pandas as _pd
from datetime import datetime, timezone as _tz

from aiopslab.orchestrator.static_actions.base import StaticTaskActions
from aiopslab.utils.actions import action, executor_action, metric_action, trace_action
from aiopslab.utils.status import SubmissionStatus


# ---------------------------------------------------------------------------
# Trace call graph helpers
# ---------------------------------------------------------------------------

def _ctype(name: str) -> str:
    """Extract component type prefix, e.g. 'docker' from 'docker_003'."""
    m = _re.match(r'^([a-zA-Z]+)', str(name))
    return m.group(1) if m else str(name)


def _to_unix_ts(t) -> float | None:
    """Convert to Unix timestamp. Handles int, float, or datetime string."""
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            return _pd.Timestamp(t).timestamp()
        except Exception:
            return None
    return float(t)


def _normalize_trace_schema(df: _pd.DataFrame) -> _pd.DataFrame:
    """Normalize Bank/Telecom trace schema to (startTime, cmdb_id, dsName, elapsedTime, success).

    Telecom schema: already has startTime, dsName, success, elapsedTime, cmdb_id — returned as-is.
    Bank schema: has timestamp, span_id, parent_id, trace_id, duration, cmdb_id.
      - Reconstructs caller→callee edges from parent_id relationships.
      - Maps timestamp→startTime, duration→elapsedTime.
      - Sets success=True (no failure info in Bank traces).
    """
    if 'dsName' in df.columns and 'startTime' in df.columns:
        return df  # Telecom schema — already correct

    if 'span_id' not in df.columns or 'parent_id' not in df.columns:
        return df  # Unknown schema, return as-is

    df = df.copy()
    if 'startTime' not in df.columns and 'timestamp' in df.columns:
        df['startTime'] = df['timestamp']
    if 'elapsedTime' not in df.columns and 'duration' in df.columns:
        df['elapsedTime'] = df['duration']
    if 'success' not in df.columns:
        df['success'] = True

    # Reconstruct caller→callee edges from parent_id
    span_cmdb = df.set_index('span_id')['cmdb_id'].to_dict()
    df['_parent_cmdb'] = df['parent_id'].map(span_cmdb)
    edges = df[df['_parent_cmdb'].notna()].copy()

    if edges.empty:
        df['dsName'] = df['cmdb_id']
        return df.drop(columns=['_parent_cmdb'], errors='ignore')

    # Edge: caller = _parent_cmdb, callee = cmdb_id (as dsName)
    edges['dsName'] = edges['cmdb_id']
    edges['cmdb_id'] = edges['_parent_cmdb']
    return edges.drop(columns=['_parent_cmdb'])


def _compute_network_gap(raw_df: _pd.DataFrame) -> dict:
    """Signal C: parent-child span latency gap — network fault indicator.

    For each parent span, gap = parent.duration - sum(child.duration).
    A component whose spans consistently show a large gap is consuming network/transport
    time there, indicating network latency or packet loss.

    Requires raw span rows with span_id, parent_id, duration columns (Bank schema).
    Returns dict: cmdb_id -> avg_gap_ratio (avg_gap / avg_parent_duration), empty if
    schema is unsupported.
    """
    if 'span_id' not in raw_df.columns or 'parent_id' not in raw_df.columns:
        return {}
    dur_col = 'duration' if 'duration' in raw_df.columns else (
        'elapsedTime' if 'elapsedTime' in raw_df.columns else None
    )
    if dur_col is None or 'cmdb_id' not in raw_df.columns:
        return {}

    span_dur = raw_df.set_index('span_id')[dur_col].to_dict()
    span_cmdb = raw_df.set_index('span_id')['cmdb_id'].to_dict()

    # Sum child durations per parent span
    children = raw_df[raw_df['parent_id'].isin(span_dur)].copy()
    if children.empty:
        return {}

    child_sum = children.groupby('parent_id')[dur_col].sum()

    gaps = []
    for span_id, child_total in child_sum.items():
        parent_dur = span_dur.get(span_id, 0)
        parent_cmdb = span_cmdb.get(span_id)
        if parent_cmdb is None or parent_dur <= 0:
            continue
        gap = max(0.0, float(parent_dur) - float(child_total))
        gaps.append({'cmdb_id': parent_cmdb, 'gap': gap, 'parent_dur': float(parent_dur)})

    if not gaps:
        return {}

    gap_df = _pd.DataFrame(gaps)
    agg = gap_df.groupby('cmdb_id').agg(
        avg_gap=('gap', 'mean'),
        avg_parent_dur=('parent_dur', 'mean'),
        span_count=('gap', 'count'),
    ).reset_index()
    agg['gap_ratio'] = agg['avg_gap'] / (agg['avg_parent_dur'] + 1e-6)

    return {
        row['cmdb_id']: {
            'gap_ratio': row['gap_ratio'],
            'avg_gap_ms': row['avg_gap'],
            'avg_parent_dur_ms': row['avg_parent_dur'],
            'span_count': int(row['span_count']),
        }
        for _, row in agg.iterrows()
    }


def _analyze_traces(df: _pd.DataFrame, faulty_components=None,
                    raw_df: _pd.DataFrame | None = None) -> dict | None:
    """Three-signal trace call graph analysis.

    Signal A (callee): dsName has high fail_rate → db/service fault.
    Signal B (caller): cmdb_id has high elapsed_ratio vs peer group → CPU/network fault.
    Signal C (network gap): parent.duration - sum(child.duration) gap ratio vs peer group
    Combined score = A * 3 + B + C * 2.

    raw_df: original (pre-normalization) span rows needed for Signal C. If None or
            schema lacks span_id/parent_id, Signal C is skipped.

    Returns dict with best_component, signal, callee_stats, caller_stats,
    network_gap_stats, scores, or None if analysis fails.
    """
    if df.empty or 'cmdb_id' not in df.columns or 'dsName' not in df.columns:
        return None

    # Filter to faulty components if provided
    if faulty_components:
        mask = df['cmdb_id'].isin(faulty_components) | df['dsName'].isin(faulty_components)
        edge_rows = df[mask]
        if edge_rows.empty:
            edge_rows = df
    else:
        edge_rows = df

    # Signal A: callee (dsName) — fail_rate × 100
    callee_stats = edge_rows.groupby('dsName').agg(
        incoming_count=('startTime', 'count'),
        fail_count=('success', lambda x: (~x.astype(bool)).sum()),
        avg_elapsed=('elapsedTime', 'mean'),
    ).reset_index().rename(columns={'dsName': 'component'})
    callee_stats['fail_rate'] = (
        callee_stats['fail_count'] / callee_stats['incoming_count']
    )
    callee_stats['score_a'] = callee_stats['fail_rate'] * 100

    # Signal B: caller (cmdb_id) — elapsed_ratio vs peer-group median
    caller_stats = df.groupby('cmdb_id').agg(
        call_count=('startTime', 'count'),
        avg_elapsed=('elapsedTime', 'mean'),
        fail_count=('success', lambda x: (~x.astype(bool)).sum()),
    ).reset_index().rename(columns={'cmdb_id': 'component'})
    caller_stats['ctype'] = caller_stats['component'].apply(_ctype)
    peer_med = caller_stats.groupby('ctype')['avg_elapsed'].median()
    caller_stats['peer_median'] = caller_stats['ctype'].map(peer_med)
    caller_stats['elapsed_ratio'] = (
        caller_stats['avg_elapsed'] / (caller_stats['peer_median'] + 1e-6)
    )
    caller_stats['score_b'] = caller_stats['elapsed_ratio'].clip(lower=0)

    # Signal C: network gap — parent.duration - sum(child.duration) ratio vs peer group
    gap_info = _compute_network_gap(raw_df) if raw_df is not None else {}
    if gap_info:
        # Normalize gap_ratio vs peer-group median (by component type prefix)
        gap_series = _pd.Series({c: v['gap_ratio'] for c, v in gap_info.items()})
        gap_df_tmp = gap_series.reset_index()
        gap_df_tmp.columns = ['component', 'gap_ratio']
        gap_df_tmp['ctype'] = gap_df_tmp['component'].apply(_ctype)
        peer_gap_med = gap_df_tmp.groupby('ctype')['gap_ratio'].median()
        gap_df_tmp['peer_gap_median'] = gap_df_tmp['ctype'].map(peer_gap_med)
        gap_df_tmp['score_c'] = (
            gap_df_tmp['gap_ratio'] / (gap_df_tmp['peer_gap_median'] + 1e-6)
        ).clip(lower=0)
        score_c = dict(zip(gap_df_tmp['component'], gap_df_tmp['score_c']))
    else:
        score_c = {}

    # Merge scores across all candidates
    score_a = dict(zip(callee_stats['component'], callee_stats['score_a']))
    score_b = dict(zip(caller_stats['component'], caller_stats['score_b']))

    candidates = set(score_a) | set(score_b) | set(score_c)
    if faulty_components:
        focus = candidates & set(faulty_components)
        if focus:
            candidates = focus

    scores = {
        c: {
            'a': score_a.get(c, 0.0),
            'b': score_b.get(c, 0.0),
            'c': score_c.get(c, 0.0),
            'combined': score_a.get(c, 0.0) * 3 + score_b.get(c, 0.0) + score_c.get(c, 0.0) * 2,
        }
        for c in candidates
    }
    if not scores:
        return None

    best = max(scores, key=lambda c: scores[c]['combined'])
    sc = scores[best]
    if sc['a'] > 1.0:
        signal = 'callee'
    elif sc['c'] > sc['b']:
        signal = 'network_gap'
    else:
        signal = 'caller'

    return {
        'best_component': best,
        'signal': signal,
        'callee_stats': callee_stats,
        'caller_stats': caller_stats,
        'network_gap_stats': gap_info,
        'scores': scores,
    }


class StaticRCAActions(StaticTaskActions):
    """Actions for OpenRCA root cause analysis tasks."""

    def __init__(self, *args, possible_root_causes=None, telemetry_flags=None,
                 use_executor=True, use_hypothesis=True, **kwargs):
        super().__init__(*args, **kwargs)
        self._executor_fn = None
        prc = possible_root_causes or {}
        self.possible_components = prc.get("components", [])
        self.possible_reasons = prc.get("reasons", [])

        flags = telemetry_flags or {}
        enabled = set()
        if flags.get("enable_log", True):
            enabled.add("log")
        if flags.get("enable_metric", True):
            enabled.add("metric")
        if flags.get("enable_trace", True):
            enabled.add("trace")
        if use_executor:
            enabled.add("executor")
        if use_hypothesis:
            enabled.add("hypothesis")
        # None means no filtering (backward compat when no flags provided)
        self.enabled_telemetry_types: frozenset | None = (
            frozenset(enabled) if telemetry_flags is not None else None
        )

    def set_executor(self, executor_fn):
        """Inject an executor callback from the RCA agent.

        Args:
            executor_fn: Callable(instruction: str) -> str
        """
        self._executor_fn = executor_fn

    @metric_action
    def get_kpi_high_deviation(self, namespace: str, start_time=None, end_time=None) -> str:
        """Components whose KPI values exceed the global P90 baseline in the fault window.
        """
        df = self.static_app.fetch_kpi_high_deviation(
            namespace,
            start_time=start_time,
            end_time=end_time,
            components=self.possible_components or None,
        )
        if df is None or df.empty:
            return (
                f"No HIGH (above P90) deviations found for namespace '{namespace}' "
                f"in the specified window. No CPU fault or network delay signals detected. "
                f"Try get_kpi_low_deviation() to check for drop-type faults."
            )
        lines = [
            f"HIGH KPI deviations (above P90) for '{namespace}'",
            f"  Components filtered to possible root causes only",
            "",
            df.to_string(index=False),
        ]
        return "\n".join(lines)

    @metric_action
    def get_kpi_low_deviation(self, namespace: str, start_time=None, end_time=None) -> str:
        """Components whose KPI values drop below the global P10 baseline in the fault window.
        """
        df = self.static_app.fetch_kpi_low_deviation(
            namespace,
            start_time=start_time,
            end_time=end_time,
            components=self.possible_components or None,
        )
        if df is None or df.empty:
            return (
                f"No LOW (below P10) deviations found for namespace '{namespace}' "
                f"Try get_kpi_high_deviation() to check for spike-type faults."
            )
        lines = [
            f"LOW KPI deviations (below P10) for '{namespace}'",
            f"  Components filtered to possible root causes only",
            "",
            df.to_string(index=False),
        ]
        return "\n".join(lines)

    @metric_action
    def get_kpi_deviation_table(self, namespace: str, start_time=None, end_time=None) -> str:
        """One-row-per-component summary of worst KPI deviations for all possible root cause components.
        """
        df = self.static_app.fetch_kpi_deviation_table(
            namespace,
            start_time=start_time,
            end_time=end_time,
            components=self.possible_components or None,
        )
        if df is None or df.empty:
            return f"No metric deviation data found for namespace '{namespace}' in the specified window."

        lines = [
            f"KPI deviation summary for '{namespace}' (all possible root cause components):",
            f"  max_high_dev: max value above P90 baseline  |  max_low_dev: max drop below P10 baseline",
            f"  peak_*_ts: exact timestamp of that extreme value — use as fault occurrence datetime",
            "",
            df.to_string(index=False),
        ]
        return "\n".join(lines)

    @metric_action
    def get_component_kpi_deviation(self, namespace: str, component: str,
                                     start_time=None, end_time=None) -> str:
        """All KPI deviations (HIGH above P90 and LOW below P10) for one specific component.
        """
        result = self.static_app.fetch_component_kpi_deviation(
            namespace, component, start_time=start_time, end_time=end_time,
        )
        high_df = result.get("high", _pd.DataFrame())
        low_df = result.get("low", _pd.DataFrame())

        if high_df.empty and low_df.empty:
            return (
                f"No KPI deviations found for '{component}' in namespace '{namespace}' "
                f"in the specified window. This component appears normal."
            )

        lines = [f"KPI deviation report for '{component}' in '{namespace}':"]

        if not high_df.empty:
            lines += ["", "HIGH (above P90):",
                      high_df.to_string(index=False)]

        if not low_df.empty:
            lines += ["", "LOW (below P10):",
                      low_df.to_string(index=False)]

        return "\n".join(lines)

    @trace_action
    def get_trace_call_graph(self, namespace: str, start_time=None, end_time=None,
                             faulty_components: list = None) -> str:
        """Identify the deepest faulty component via trace call graph analysis.

        Uses three signals derived from the trace data:
        - Callee signal (A): high fail_rate when called as dsName → db/service fault.
          Score = fail_rate × 100.
        - Caller signal (B): high avg_elapsed vs peer components of the same type →
          CPU/network fault. Score = elapsed_ratio vs peer median.
        - Network gap signal (C): parent.duration - sum(child.duration) gap ratio vs
          peer group → network latency / packet loss at that component.
          Score = gap_ratio / peer_median_gap_ratio. (Bank schema only)
        Combined score = A×3 + B + C×2. Signal type: 'callee' if A>1.0, else
        'network_gap' if C>B, else 'caller'.

        Call this after get_kpi_high_deviation() / get_kpi_low_deviation() to pinpoint
        the deepest faulty node before saving a hypothesis.

        Args:
            namespace: Namespace string (e.g., "static-telecom").
            start_time: Fault window start — Unix timestamp (s) or "YYYY-MM-DD HH:MM:SS".
            end_time:   Fault window end   — Unix timestamp (s) or "YYYY-MM-DD HH:MM:SS".
            faulty_components: Candidate components from metric deviation analysis.
                               Focuses scoring on these; if omitted, analyzes all.
        """
        raw_df = self.static_app.fetch_traces_df(namespace)
        if raw_df.empty:
            return f"No trace data found for namespace '{namespace}'."

        # Trace CSVs use 'startTime' (Unix seconds), not 'timestamp'
        ts_col = 'startTime' if 'startTime' in raw_df.columns else 'timestamp'
        start_ts = _to_unix_ts(start_time)
        end_ts = _to_unix_ts(end_time)
        if start_ts is not None:
            raw_df = raw_df[raw_df[ts_col] >= start_ts]
        if end_ts is not None:
            raw_df = raw_df[raw_df[ts_col] <= end_ts]

        if raw_df.empty:
            return f"No traces found in the specified time window for '{namespace}'."

        # Keep raw_df for Signal C (parent-child gap); normalize for A and B
        norm_df = _normalize_trace_schema(raw_df)

        # If no explicit faulty_components, default to possible_components so trace
        # analysis never returns components outside the allowed candidate set
        effective_components = faulty_components or (self.possible_components or None)
        result = _analyze_traces(norm_df, effective_components, raw_df=raw_df)
        if result is None:
            return "Trace analysis: could not identify a faulty component (insufficient data)."

        best = result['best_component']
        signal = result['signal']
        scores = result['scores']
        gap_info = result.get('network_gap_stats', {})

        # Trace data time range from actual data
        trace_start_ts = int(raw_df[ts_col].min())
        trace_end_ts = int(raw_df[ts_col].max())
        trace_start_dt = datetime.fromtimestamp(trace_start_ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        trace_end_dt = datetime.fromtimestamp(trace_end_ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M:%S")

        signal_desc = {
            'callee': 'callee fail_rate (db/service fault)',
            'network_gap': (
                'network gap: parent.duration - sum(child.duration) — '
                'network fault at this component. '
                'Resolve type: NET* drop in get_kpi_low_deviation = packet loss; '
                'NET* spike in get_kpi_high_deviation = network latency'
            ),
            'caller': 'caller elapsed_ratio vs peer group (CPU/network fault)',
        }.get(signal, signal)

        top = sorted(scores.items(), key=lambda x: x[1]['combined'], reverse=True)[:5]
        score_lines = [
            f"  {comp}: combined={sc['combined']:.2f}  "
            f"(callee={sc['a']:.2f}, caller={sc['b']:.2f}, net_gap={sc['c']:.2f})"
            for comp, sc in top
        ]

        callee_df = result['callee_stats']
        best_callee = callee_df[callee_df['component'] == best]

        lines = [
            f"Trace call graph analysis for '{namespace}':",
            f"  Best candidate: {best}",
            f"  Signal type:    {signal} — {signal_desc}",
            f"  Trace time range: {trace_start_dt} ~ {trace_end_dt} UTC",
            "",
            "Top candidates (combined = callee×3 + caller + net_gap×2):",
        ] + score_lines

        if not best_callee.empty:
            row = best_callee.iloc[0]
            lines += [
                "",
                f"Callee stats for {best}:",
                f"  calls={int(row['incoming_count'])}, "
                f"failures={int(row['fail_count'])}, "
                f"fail_rate={row['fail_rate']:.3f}, "
                f"avg_elapsed={row['avg_elapsed']:.0f}ms",
            ]

        if gap_info:
            top_gap = sorted(gap_info.items(), key=lambda x: x[1]['gap_ratio'], reverse=True)[:5]
            lines += ["", "Network gap stats (parent.duration - sum(child.duration)):"]
            for comp, g in top_gap:
                lines.append(
                    f"  {comp}: gap_ratio={g['gap_ratio']:.3f}  "
                    f"avg_gap={g['avg_gap_ms']:.0f}ms / "
                    f"avg_parent={g['avg_parent_dur_ms']:.0f}ms  "
                    f"(n={g['span_count']})"
                )

        return "\n".join(lines)

    @executor_action
    def execute(self, instruction: str) -> str:
        """Runs Python code in an IPython kernel. Use for pandas data analysis on fetched CSVs."""
        if self._executor_fn is None:
            return "Error: Executor not initialized. Call set_executor() first."
        return self._executor_fn(instruction)

    @action
    def submit(self, prediction: dict):
        """
        Submit root cause analysis prediction.

        Args:
            prediction (dict): JSON dict with numbered keys ("1", "2", ...).
                Each value is a dict with optional fields:
                - "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS"
                - "root cause component": "component_name"
                - "root cause reason": "fault_reason"

        Returns:
            SubmissionStatus or str: VALID_SUBMISSION if accepted, error message if invalid.
        """
        errors = []
        for key, entry in prediction.items():
            if not isinstance(entry, dict):
                continue
            component = entry.get("root cause component", "")
            reason = entry.get("root cause reason", "")

            if self.possible_components and component and component not in self.possible_components:
                errors.append(
                    f"  [{key}] Invalid 'root cause component': '{component}'.\n"
                    f"       Must be one of: {self.possible_components}"
                )
            if self.possible_reasons and reason and reason not in self.possible_reasons:
                errors.append(
                    f"  [{key}] Invalid 'root cause reason': '{reason}'.\n"
                    f"       Must be one of: {self.possible_reasons}"
                )

        if errors:
            return (
                "Submission rejected - invalid values:\n"
                + "\n".join(errors)
                + "\n\nPlease correct and resubmit."
            )

        return SubmissionStatus.VALID_SUBMISSION
