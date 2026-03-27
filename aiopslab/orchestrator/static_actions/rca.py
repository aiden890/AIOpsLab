"""Unified actions for OpenRCA static dataset tasks.

All 7 task types use the same submit format (JSON dict).
"""

import os as _os
import re as _re
import pandas as _pd
from datetime import datetime, timezone as _tz

from aiopslab.orchestrator.static_actions.base import StaticTaskActions
from aiopslab.orchestrator.static_actions.trace_path_renderer import (
    get_trace_path_renderer,
)
from aiopslab.utils.actions import action, executor_action, metric_action, trace_action, visualization
from aiopslab.utils.status import SubmissionStatus


# ---------------------------------------------------------------------------
# Shared color palette for peer graphs (22 visually distinct colors)
# ---------------------------------------------------------------------------
_PEER_COLORS = [
    '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
    '#42d4f4', '#f032e6', '#000000', '#ffe119', '#469990',
    '#9A6324', '#800000', '#aaffc3', '#000075', '#a9a9a9',
    '#808000', '#ffd8b1', '#bfef45', '#fabed4', '#dcbeff',
    '#fffac8', '#ff6961',
]
_PEER_LINESTYLES = ['-', '--', '-.', ':']


def _safe_filename_fragment(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return "unknown"
    return _re.sub(r"[^A-Za-z0-9._-]+", "_", text)

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
    """Normalize Bank/Telecom/Market trace schema to (startTime, cmdb_id, dsName, elapsedTime, success).

    Telecom schema: already has startTime, dsName, success, elapsedTime, cmdb_id — returned as-is.
    Bank schema: has timestamp, span_id, parent_id, trace_id, duration, cmdb_id.
    Market schema: has timestamp, cmdb_id, span_id, trace_id, duration, type, status_code,
                   operation_name, parent_span.
      - Reconstructs caller→callee edges from parent_id/parent_span relationships.
      - Maps timestamp→startTime, duration→elapsedTime.
      - Maps status_code→success (0 = True) or sets success=True if absent.
    """
    if 'dsName' in df.columns and 'startTime' in df.columns:
        return df  # Telecom schema — already correct

    # Detect parent column: Bank uses parent_id, Market uses parent_span
    parent_col = None
    if 'parent_id' in df.columns:
        parent_col = 'parent_id'
    elif 'parent_span' in df.columns:
        parent_col = 'parent_span'

    if 'span_id' not in df.columns or parent_col is None:
        return df  # Unknown schema, return as-is

    df = df.copy()
    if 'startTime' not in df.columns and 'timestamp' in df.columns:
        df['startTime'] = df['timestamp']
    if 'elapsedTime' not in df.columns and 'duration' in df.columns:
        df['elapsedTime'] = df['duration']
    if 'success' not in df.columns:
        if 'status_code' in df.columns:
            df['success'] = df['status_code'].astype(str).str.strip().str.lower().isin(
                ['0', '200', 'ok']
            )
        else:
            df['success'] = True
    # Normalize parent_col to parent_id for downstream compatibility
    if parent_col != 'parent_id':
        df['parent_id'] = df[parent_col]

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


def _extract_edge_rows_id_pid(df: _pd.DataFrame) -> _pd.DataFrame:
    """Extract caller->callee edge rows from id/pid (RPC: parent cmdb_id -> child cmdb_id).

    child.pid = parent.id -> caller=parent.cmdb_id, callee=child.cmdb_id.
    Returns DataFrame with columns: caller, callee, bucket, elapsedTime, success_bool.
    """
    id_col = 'id' if 'id' in df.columns else ('span_id' if 'span_id' in df.columns else None)
    pid_col = (
        'pid'
        if 'pid' in df.columns
        else ('parent_id' if 'parent_id' in df.columns else ('parent_span' if 'parent_span' in df.columns else None))
    )
    if id_col is None or pid_col is None or 'cmdb_id' not in df.columns:
        return _pd.DataFrame()

    df = df.copy()
    df['_id'] = df[id_col].astype(str)
    df['_pid'] = df[pid_col].fillna('').astype(str)
    children = df[df['_pid'].str.strip() != ''].copy()
    if children.empty:
        return _pd.DataFrame()

    parent_map = df.set_index('_id')['cmdb_id'].to_dict()
    children['caller'] = children['_pid'].map(parent_map)
    children['callee'] = children['cmdb_id'].astype(str).str.strip()
    edges = children[children['caller'].notna() & (children['caller'] != children['callee'])].copy()
    if edges.empty:
        return _pd.DataFrame()

    out = edges[['caller', 'callee', 'bucket']].copy()
    dur_col = 'elapsedTime' if 'elapsedTime' in edges.columns else 'duration'
    out['elapsedTime'] = edges[dur_col].astype(float) if dur_col in edges.columns else 0.0
    out['success_bool'] = edges['success_bool'] if 'success_bool' in edges.columns else True
    return out


def _extract_edge_rows_dsname(df: _pd.DataFrame) -> _pd.DataFrame:
    """Extract caller->callee edge rows from cmdb_id + dsName (JDBC: docker->db).

    Telecom JDBC: cmdb_id=caller, dsName=callee.
    Returns DataFrame with columns: caller, callee, bucket, elapsedTime, success_bool.
    """
    if 'cmdb_id' not in df.columns or 'dsName' not in df.columns:
        return _pd.DataFrame()

    df = df.copy()
    df['caller'] = df['cmdb_id'].astype(str).str.strip()
    df['callee'] = df['dsName'].astype(str).str.strip()
    edges = df[(df['callee'] != '') & (df['caller'] != df['callee'])].copy()
    if edges.empty:
        return _pd.DataFrame()

    out = edges[['caller', 'callee', 'bucket']].copy()
    dur_col = 'elapsedTime' if 'elapsedTime' in edges.columns else 'duration'
    out['elapsedTime'] = edges[dur_col].astype(float) if dur_col in edges.columns else 0.0
    out['success_bool'] = edges['success_bool'] if 'success_bool' in edges.columns else True
    return out


def _compute_network_gap(raw_df: _pd.DataFrame) -> dict:
    """Signal C: parent-child span latency gap — network fault indicator.

    For each parent span, gap = parent.duration - sum(child.duration).
    A component whose spans consistently show a large gap is consuming network/transport
    time there, indicating network latency or packet loss.

    Requires raw span rows with span_id, parent_id/parent_span, duration columns.
    Returns dict: cmdb_id -> avg_gap_ratio (avg_gap / avg_parent_duration), empty if
    schema is unsupported.
    """
    parent_col = 'parent_id' if 'parent_id' in raw_df.columns else (
        'parent_span' if 'parent_span' in raw_df.columns else None
    )
    if 'span_id' not in raw_df.columns or parent_col is None:
        return {}
    if parent_col != 'parent_id':
        raw_df = raw_df.copy()
        raw_df['parent_id'] = raw_df[parent_col]
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
        self.problem_id: str = ""
        self.save_dir: str = ""
        self._query_start = None
        self._query_end = None
        prc = possible_root_causes or {}
        self.possible_components = prc.get("components", [])
        self.possible_reasons = prc.get("reasons", [])
        self.component_levels = prc.get("component_levels", {})

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
        self.enable_visualization_tool: bool = flags.get(
            "enable_visualization_tool", True
        )

    def set_executor(self, executor_fn):
        """Inject an executor callback from the RCA agent.

        Args:
            executor_fn: Callable(instruction: str) -> str
        """
        self._executor_fn = executor_fn

    # @metric_action
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

    # @metric_action
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

    # @metric_action
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

    # @metric_action
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

    # @trace_action
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

    @visualization
    @metric_action
    def get_success_rate_drop_graph(self, namespace: str) -> str:
        """Plot app-level success rate over time and highlight drop windows.
        Reads metric_app.csv only (not container/node metrics).
        Generates a PNG chart with per-service SR lines and red-shaded
        regions where SR falls below 99%.

        Args:
            namespace: e.g. "static-bank", "static-telecom"

        Returns:
            File path to the generated PNG image.
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        _SR_THRESHOLD = 99.0
        _BUCKET_MINUTES = 1

        df = self.static_app.fetch_metric_app_df(
            namespace,
            start_time=self._query_start,
            end_time=self._query_end,
        )
        if df.empty:
            return f"No metric_app data found for namespace '{namespace}'"

        df = df.copy()

        # --- Normalize columns ---
        # Timestamp → unix seconds
        if "startTime" in df.columns:
            ts_col = "startTime"
            if df[ts_col].max() > 1e12:
                df[ts_col] = df[ts_col] / 1000.0
        elif "timestamp" in df.columns:
            ts_col = "timestamp"
        else:
            return "No timestamp column found in metric_app data"

        # Service column
        svc_col = None
        for c in ["tc", "serviceName", "cmdb_id", "service"]:
            if c in df.columns:
                svc_col = c
                break
        if svc_col is None:
            return "No service column found in metric_app data"

        # Success rate → 0-100 scale
        if "sr" in df.columns:
            sr_col = "sr"
        elif "succee_rate" in df.columns:
            sr_col = "succee_rate"
            df[sr_col] = df[sr_col] * 100.0
        else:
            return "No success rate column (sr / succee_rate) in metric_app data"

        df["datetime"] = _pd.to_datetime(df[ts_col], unit="s", utc=True)

        bucket_sec = _BUCKET_MINUTES * 60
        df["_bucket"] = (df[ts_col] // bucket_sec) * bucket_sec
        df["_bucket_dt"] = _pd.to_datetime(df["_bucket"], unit="s", utc=True)

        services = sorted(df[svc_col].unique())

        # --- Compute per-service bucketed SR ---
        bucketed = (
            df.groupby([svc_col, "_bucket", "_bucket_dt"])[sr_col]
            .mean()
            .reset_index()
            .rename(columns={sr_col: "avg_sr"})
        )

        # --- Find drop buckets (any service below threshold) ---
        drop_buckets = bucketed[bucketed["avg_sr"] < _SR_THRESHOLD]["_bucket_dt"].unique()

        # --- Plot ---
        n_services = len(services)
        fig_height = max(6, 3 * min(n_services, 4))
        fig, ax = plt.subplots(figsize=(16, fig_height))

        for svc in services:
            svc_data = bucketed[bucketed[svc_col] == svc].sort_values("_bucket_dt")
            ax.plot(svc_data["_bucket_dt"], svc_data["avg_sr"],
                    label=svc, linewidth=1.0, alpha=0.7)

        # Highlight drop windows with red shading
        if len(drop_buckets) > 0:
            drop_sorted = sorted(drop_buckets)
            bucket_delta = _pd.Timedelta(seconds=bucket_sec)

            # Merge adjacent drop buckets into contiguous windows
            window_start = drop_sorted[0]
            window_end = drop_sorted[0] + bucket_delta
            for dt in drop_sorted[1:]:
                if dt <= window_end:
                    window_end = dt + bucket_delta
                else:
                    ax.axvspan(window_start, window_end,
                               color="red", alpha=0.15, zorder=0)
                    window_start = dt
                    window_end = dt + bucket_delta
            ax.axvspan(window_start, window_end,
                       color="red", alpha=0.15, zorder=0,
                       label=f"SR < {_SR_THRESHOLD}%")

        # Threshold line
        ax.axhline(y=_SR_THRESHOLD, color="red", linestyle="--",
                   linewidth=1.0, alpha=0.5, label=f"Threshold ({_SR_THRESHOLD}%)")

        ax.set_title(f"App Success Rate — {namespace}", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("Success Rate (%)")
        ax.set_ylim(
            max(0, bucketed["avg_sr"].min() - 5),
            min(105, bucketed["avg_sr"].max() + 2),
        )
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.legend(loc="lower left", fontsize=7, ncol=3)

        plt.tight_layout()

        out_dir = self.save_dir or _os.path.join(self.work_dir, "static_metric_output")
        _os.makedirs(out_dir, exist_ok=True)
        file_path = _os.path.join(out_dir, "sr_drop_windows.png")
        fig.savefig(file_path, dpi=150, bbox_inches="tight")
        plt.close(fig)

        # Build text summary of drop windows
        # file_path on its own line so _extract_image_paths() detects it for vision critic
        summary_lines = [file_path]
        if len(drop_buckets) > 0:
            drop_services = (
                bucketed[bucketed["avg_sr"] < _SR_THRESHOLD]
                .groupby(svc_col)["avg_sr"]
                .agg(min_sr="min", count="count")
                .sort_values("min_sr")
            )
            summary_lines.append(f"\nServices with SR drops below {_SR_THRESHOLD}%:")
            for svc, row in drop_services.iterrows():
                summary_lines.append(
                    f"  {svc}: min_sr={row['min_sr']:.1f}%, "
                    f"drop_buckets={int(row['count'])}"
                )
            drop_start = _pd.Timestamp(min(drop_buckets)).strftime("%H:%M:%S")
            drop_end = _pd.Timestamp(max(drop_buckets)).strftime("%H:%M:%S")
            summary_lines.append(f"  Drop window range: {drop_start} ~ {drop_end} (UTC)")
        else:
            summary_lines.append(
                f"No SR drops below {_SR_THRESHOLD}% detected in the time range."
            )

        return "\n".join(summary_lines)

    @visualization
    @metric_action
    def get_kpi_peer_graph(self, namespace: str, component_type: str,
                           kpi_name) -> str:
        """Plot time-series chart of KPI(s) for all peers of the given component type.

        Args:
            namespace: e.g. "static-telecom"
            component_type: "docker", "os", "db", "redis"
            kpi_name: a single KPI name (str) or a list of up to 2 KPI names

        Returns:
            File path(s) to the generated PNG image(s), comma-separated.
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        # Normalize to list (max 2)
        if isinstance(kpi_name, str):
            kpi_names = [kpi_name]
        else:
            kpi_names = list(kpi_name)[:2]

        df = self.static_app.fetch_metrics_df(
            namespace,
            start_time=self._query_start,
            end_time=self._query_end,
        )
        if df.empty:
            return f"No metrics found for namespace '{namespace}'"

        # Normalize column names
        if "name" in df.columns and "kpi_name" not in df.columns:
            df = df.rename(columns={"name": "kpi_name"})
        if "startTime" in df.columns and "timestamp" not in df.columns:
            df = df.rename(columns={"startTime": "timestamp"})

        # Filter by component_type:
        # 1) If component_levels has this type (e.g. "node", "pod", "service"), use it
        #    For "pod" level: metric cmdb_id may be "node-X.pod_name" — match suffix after "."
        # 2) Otherwise, prefix match with underscore then hyphen
        level_members = self.component_levels.get(component_type)
        if level_members:
            level_set = set(level_members)
            # Direct match first (node, service levels)
            peer_df = df[df["cmdb_id"].isin(level_set)]
            if peer_df.empty:
                # Suffix match for "node-X.pod_name" format (pod level)
                peer_df = df[df["cmdb_id"].apply(
                    lambda x: str(x).split(".", 1)[-1] if "." in str(x) else ""
                ).isin(level_set)]
        else:
            peer_df = df[df["cmdb_id"].str.startswith(component_type + "_")]
            if peer_df.empty:
                peer_df = df[df["cmdb_id"].str.startswith(component_type + "-")]
        if peer_df.empty:
            return f"No metrics found for component type '{component_type}'"

        avail_kpis = sorted(peer_df["kpi_name"].unique().tolist())
        # If any requested KPI is missing, return available list only (no file)
        missing = [k for k in kpi_names if k not in avail_kpis]
        if missing:
            return (
                f"KPI(s) not found: {missing}. "
                f"Available KPIs for component_type '{component_type}': {avail_kpis}"
            )

        # Pre-check: skip if no plottable values (all series flat)
        has_plottable = False
        for kpi in kpi_names:
            kpi_df = peer_df[peer_df["kpi_name"] == kpi].copy()
            components_all = sorted(kpi_df["cmdb_id"].unique())
            ranges = {}
            for comp in components_all:
                vals = kpi_df[kpi_df["cmdb_id"] == comp]["value"]
                comp_range = float(vals.max() - vals.min()) if not vals.empty else 0.0
                ranges[comp] = comp_range
            global_range = max(ranges.values()) if ranges else 0.0
            var_threshold = max(global_range * 0.05, 1e-3)
            components_var = [
                c for c in components_all if ranges.get(c, 0.0) >= var_threshold
            ]
            if components_var:
                has_plottable = True
                break
        if not has_plottable:
            return (
                f"No variable KPI data to plot for {component_type}/{', '.join(kpi_names)}. "
                "All series are flat over the window."
            )

        n = len(kpi_names)
        fig, axes = plt.subplots(n, 1, figsize=(14, 5 * n), squeeze=False)

        out_dir = self.save_dir or _os.path.join(self.work_dir, "static_metric_output")
        _os.makedirs(out_dir, exist_ok=True)

        for idx, kpi in enumerate(kpi_names):
            ax = axes[idx, 0]
            kpi_df = peer_df[peer_df["kpi_name"] == kpi].copy()
            ts = kpi_df["timestamp"]
            if ts.median() > 1e12:
                ts = ts / 1000.0
            kpi_df["datetime"] = _pd.to_datetime(ts, unit="s", utc=True)

            # Filter out components whose series is effectively flat over the window.
            # Define variability as range; drop series whose range is very small
            # relative to the global range (dead/idle KPIs).
            components_all = sorted(kpi_df["cmdb_id"].unique())
            ranges = {}
            for comp in components_all:
                vals = kpi_df[kpi_df["cmdb_id"] == comp]["value"]
                comp_range = float(vals.max() - vals.min()) if not vals.empty else 0.0
                ranges[comp] = comp_range
            global_range = max(ranges.values()) if ranges else 0.0
            # Threshold: at least 5% of global range or an absolute epsilon
            var_threshold = max(global_range * 0.05, 1e-3)
            components_var = [
                c for c in components_all if ranges.get(c, 0.0) >= var_threshold
            ]
            # If still too many series, keep only top-N by variability to simplify legend.
            MAX_SERIES = 12
            if len(components_var) > MAX_SERIES:
                components = sorted(
                    components_var,
                    key=lambda c: ranges.get(c, 0.0),
                    reverse=True,
                )[:MAX_SERIES]
            else:
                components = components_var

            for ci, comp in enumerate(components):
                comp_df = kpi_df[kpi_df["cmdb_id"] == comp].sort_values("datetime")
                if comp_df.empty:
                    continue
                color = _PEER_COLORS[ci % len(_PEER_COLORS)]
                ls = _PEER_LINESTYLES[ci // len(_PEER_COLORS)]
                ax.plot(
                    comp_df["datetime"],
                    comp_df["value"],
                    label=comp,
                    linewidth=0.8,
                    alpha=0.85,
                    color=color,
                    linestyle=ls,
                    marker="o",
                    markersize=2.5,
                )

            ax.set_title(f"{component_type} peers — {kpi}",
                         fontsize=13, fontweight="bold")
            ax.set_xlabel("Time (UTC)")
            ax.set_ylabel(kpi)
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.legend(loc="upper right", fontsize=7, ncol=2)

        plt.tight_layout()
        safe_component_type = _safe_filename_fragment(component_type)
        safe_kpis = [_safe_filename_fragment(kpi) for kpi in kpi_names]
        fname = f"{safe_component_type}_{'_'.join(safe_kpis)}.png"
        file_path = _os.path.join(out_dir, fname)
        fig.savefig(file_path, dpi=150, bbox_inches="tight")
        plt.close(fig)

        return file_path

    # -- Trace graph helpers (shared) ----------------------------------

    def _load_trace_window(self, namespace: str) -> _pd.DataFrame:
        """Load trace data filtered to query window, with parsed datetime and success."""
        df = self.static_app.fetch_traces_df(
            namespace,
            start_time=self._query_start,
            end_time=self._query_end,
        )
        if df.empty:
            return df

        # Normalize schema (adds startTime, elapsedTime, dsName, success for all datasets)
        df = _normalize_trace_schema(df)

        ts_col = 'startTime' if 'startTime' in df.columns else 'timestamp'
        ts = df[ts_col].copy()
        if ts.median() > 1e12:
            ts = ts / 1000.0
        df["datetime"] = _pd.to_datetime(ts, unit="s", utc=True)
        df["bucket"] = df["datetime"].dt.floor("1min")

        # Enforce query-time clipping on the trace window.
        # Some static_app implementations may ignore start_time/end_time for traces,
        # so we defensively restrict the dataframe here to [query_start, query_end]
        # to make the x-axis of trace graphs match the problem's query range.
        if self._query_start and self._query_end:
            try:
                start_dt = _pd.to_datetime(self._query_start, unit="s", utc=True)
                end_dt = _pd.to_datetime(self._query_end, unit="s", utc=True)
                df = df[(df["datetime"] >= start_dt) & (df["datetime"] <= end_dt)]
            except Exception:
                # If anything goes wrong, fall back to the unfiltered df.
                pass

        if "success" in df.columns:
            if df["success"].dtype == object:
                df["success_bool"] = df["success"].str.strip().str.lower().isin(
                    ["true", "0", "200", "ok"]
                )
            else:
                df["success_bool"] = df["success"].astype(bool)
        else:
            df["success_bool"] = True

        return df

    def _load_trace_window_minutes(
        self,
        namespace: str,
        window_minutes: int | None = None,
    ) -> _pd.DataFrame:
        """Load trace data and optionally trim to the last N minutes."""
        df = self._load_trace_window(namespace)
        if df.empty or not window_minutes or window_minutes <= 0:
            return df

        end_dt = df["datetime"].max()
        start_dt = end_dt - _pd.Timedelta(minutes=int(window_minutes))
        return df[df["datetime"] >= start_dt].copy()

    def _build_trace_edge_metric_frame(self, df: _pd.DataFrame) -> _pd.DataFrame:
        """Aggregate trace rows into per-minute caller->callee edge metrics.

        Uses both id/pid (RPC, docker-docker) and cmdb_id-dsName (JDBC, docker-db) for Telecom.
        For Bank/Market (normalized edge schema), uses only cmdb_id-dsName to avoid duplicates.
        """
        if df.empty:
            return df

        edge_rows: list[_pd.DataFrame] = []
        is_telecom = 'id' in df.columns and 'pid' in df.columns

        if is_telecom:
            id_pid_edges = _extract_edge_rows_id_pid(df)
            if not id_pid_edges.empty:
                edge_rows.append(id_pid_edges)
            dsname_edges = _extract_edge_rows_dsname(df)
            if not dsname_edges.empty:
                edge_rows.append(dsname_edges)
        else:
            dsname_edges = _extract_edge_rows_dsname(df)
            if not dsname_edges.empty:
                edge_rows.append(dsname_edges)

        if not edge_rows:
            edge_df = df.copy()
            edge_df['caller'] = edge_df['cmdb_id'].fillna('').astype(str).str.strip()
            edge_df['callee'] = edge_df['dsName'].fillna('').astype(str).str.strip()
            edge_df = edge_df[(edge_df['caller'] != '') & (edge_df['callee'] != '')]
            if edge_df.empty:
                return _pd.DataFrame()
            edge_rows = [edge_df]

        combined = _pd.concat(edge_rows, ignore_index=True)
        if 'elapsedTime' not in combined.columns:
            combined['elapsedTime'] = 0.0
        if 'success_bool' not in combined.columns:
            combined['success_bool'] = True

        return (
            combined
            .groupby(['caller', 'callee', 'bucket'], as_index=False)
            .agg(
                latency_p50=('elapsedTime', 'median'),
                error_rate=('success_bool', lambda s: (1 - s.mean()) * 100),
                volume=('success_bool', 'size'),
            )
            .sort_values(['caller', 'callee', 'bucket'])
        )

    def _build_trace_edge_gap_frame(self, df: _pd.DataFrame) -> _pd.DataFrame:
        """Build per-minute edge caller gap (p50) from parent-child elapsed differences.

        Only use RPC-style boundary spans to reduce noisy gap inflation:
        - child callType == REMOTEPROCESS
        - parent callType == CSF
        """
        if df.empty:
            return _pd.DataFrame()

        id_col = 'id' if 'id' in df.columns else ('span_id' if 'span_id' in df.columns else None)
        pid_col = (
            'pid'
            if 'pid' in df.columns
            else ('parent_id' if 'parent_id' in df.columns else ('parent_span' if 'parent_span' in df.columns else None))
        )
        dur_col = 'elapsedTime' if 'elapsedTime' in df.columns else ('duration' if 'duration' in df.columns else None)
        if id_col is None or pid_col is None or dur_col is None:
            return _pd.DataFrame()
        if 'cmdb_id' not in df.columns or 'bucket' not in df.columns:
            return _pd.DataFrame()

        work = df.copy()
        work['_id'] = work[id_col].astype(str)
        work['_pid'] = work[pid_col].fillna('').astype(str)
        if 'callType' not in work.columns:
            return _pd.DataFrame()
        work['_call_type'] = work['callType'].astype(str).str.strip().str.upper()
        children = work[work['_pid'].str.strip() != ''].copy()
        if children.empty:
            return _pd.DataFrame()

        parent_cmdb_map = work.set_index('_id')['cmdb_id'].to_dict()
        parent_calltype_map = work.set_index('_id')['_call_type'].to_dict()
        parent_elapsed_map = _pd.to_numeric(work.set_index('_id')[dur_col], errors='coerce').to_dict()
        children['caller'] = children['_pid'].map(parent_cmdb_map)
        children['callee'] = children['cmdb_id'].astype(str).str.strip()
        children['parent_call_type'] = children['_pid'].map(parent_calltype_map)
        # Keep only CSF -> RemoteProcess edges for gap signal.
        children = children[
            (children['_call_type'] == 'REMOTEPROCESS')
            & (children['parent_call_type'] == 'CSF')
        ].copy()
        if children.empty:
            return _pd.DataFrame()
        children['parent_elapsed'] = _pd.to_numeric(children['_pid'].map(parent_elapsed_map), errors='coerce')
        children['child_elapsed'] = _pd.to_numeric(children[dur_col], errors='coerce')
        children['gap_ms'] = (children['parent_elapsed'] - children['child_elapsed']).clip(lower=0.0)

        edges = children[
            children['caller'].notna()
            & children['callee'].notna()
            & (children['caller'] != '')
            & (children['callee'] != '')
            & (children['caller'] != children['callee'])
            & children['gap_ms'].notna()
        ].copy()
        if edges.empty:
            return _pd.DataFrame()

        return (
            edges
            .groupby(['caller', 'callee', 'bucket'], as_index=False)
            .agg(
                gap_p50=('gap_ms', 'median'),
                gap_volume=('gap_ms', 'size'),
            )
            .sort_values(['caller', 'callee', 'bucket'])
        )

    def _build_trace_edge_remote_frame(self, df: _pd.DataFrame) -> _pd.DataFrame:
        """Build per-minute edge RemoteProcess elapsed p50."""
        if df.empty or 'callType' not in df.columns:
            return _pd.DataFrame()

        id_col = 'id' if 'id' in df.columns else ('span_id' if 'span_id' in df.columns else None)
        pid_col = (
            'pid'
            if 'pid' in df.columns
            else ('parent_id' if 'parent_id' in df.columns else ('parent_span' if 'parent_span' in df.columns else None))
        )
        dur_col = 'elapsedTime' if 'elapsedTime' in df.columns else ('duration' if 'duration' in df.columns else None)
        if id_col is None or pid_col is None or dur_col is None:
            return _pd.DataFrame()
        if 'cmdb_id' not in df.columns or 'bucket' not in df.columns:
            return _pd.DataFrame()

        work = df.copy()
        work['_id'] = work[id_col].astype(str)
        work['_pid'] = work[pid_col].fillna('').astype(str)
        work['_call_type'] = work['callType'].astype(str).str.strip().str.upper()
        rp = work[(work['_call_type'] == 'REMOTEPROCESS') & (work['_pid'].str.strip() != '')].copy()
        if rp.empty:
            return _pd.DataFrame()

        parent_cmdb_map = work.set_index('_id')['cmdb_id'].to_dict()
        rp['caller'] = rp['_pid'].map(parent_cmdb_map)
        rp['callee'] = rp['cmdb_id'].astype(str).str.strip()
        rp['remote_elapsed'] = _pd.to_numeric(rp[dur_col], errors='coerce')
        rp = rp[
            rp['caller'].notna()
            & rp['callee'].notna()
            & (rp['caller'] != '')
            & (rp['callee'] != '')
            & (rp['caller'] != rp['callee'])
            & rp['remote_elapsed'].notna()
        ].copy()
        if rp.empty:
            return _pd.DataFrame()

        return (
            rp
            .groupby(['caller', 'callee', 'bucket'], as_index=False)
            .agg(
                remote_p50=('remote_elapsed', 'median'),
                remote_volume=('remote_elapsed', 'size'),
            )
            .sort_values(['caller', 'callee', 'bucket'])
        )

    def _detect_trace_metric_onset(
        self,
        edge_metric_df: _pd.DataFrame,
        value_col: str,
        metric_kind: str,
        *,
        baseline_buckets: int = 5,
        sustain_buckets: int = 2,
        min_edge_volume: int = 20,
    ) -> dict | None:
        """Detect sustained anomaly episodes and return the strongest one."""
        if edge_metric_df.empty:
            return None

        series_df = edge_metric_df.sort_values("bucket").reset_index(drop=True)
        n_rows = len(series_df)
        if n_rows < baseline_buckets + sustain_buckets:
            return None

        values = series_df[value_col].astype(float)
        volumes = series_df["volume"].fillna(0).astype(float)
        buckets = series_df["bucket"]

        episodes: list[dict] = []
        idx = baseline_buckets
        while idx <= n_rows - sustain_buckets:
            history = values.iloc[idx - baseline_buckets:idx]
            if history.empty or history.isna().any():
                idx += 1
                continue

            future_vals = values.iloc[idx:idx + sustain_buckets]
            if len(future_vals) < sustain_buckets:
                break

            baseline = float(history.median())
            threshold = 0.0

            def _is_anom(i: int) -> bool:
                v = float(values.iloc[i])
                vol = float(volumes.iloc[i])
                if metric_kind in {"latency", "error_rate"} and vol < min_edge_volume:
                    return False
                return v >= threshold

            if metric_kind == "latency":
                threshold = max(baseline * 1.5, baseline + 100.0)
                if baseline <= 1.0:
                    threshold = max(threshold, 40.0)
                if all(_is_anom(i) for i in range(idx, idx + sustain_buckets)):
                    end_idx = idx + sustain_buckets - 1
                    k = idx + sustain_buckets
                    while k < n_rows and _is_anom(k):
                        end_idx = k
                        k += 1
                    episodes.append({
                        "metric": metric_kind,
                        "onset": buckets.iloc[idx],
                        "end": buckets.iloc[end_idx],
                        "baseline": baseline,
                        "peak": float(values.iloc[idx:end_idx + 1].max()),
                        "threshold": threshold,
                        "duration_buckets": int(end_idx - idx + 1),
                        "duration_minutes": float(end_idx - idx + 1),
                    })
                    idx = end_idx + 1
                    continue
            elif metric_kind == "error_rate":
                threshold = max(baseline * 3.0, baseline + 10.0, 5.0)
                if all(_is_anom(i) for i in range(idx, idx + sustain_buckets)):
                    end_idx = idx + sustain_buckets - 1
                    k = idx + sustain_buckets
                    while k < n_rows and _is_anom(k):
                        end_idx = k
                        k += 1
                    episodes.append({
                        "metric": metric_kind,
                        "onset": buckets.iloc[idx],
                        "end": buckets.iloc[end_idx],
                        "baseline": baseline,
                        "peak": float(values.iloc[idx:end_idx + 1].max()),
                        "threshold": threshold,
                        "duration_buckets": int(end_idx - idx + 1),
                        "duration_minutes": float(end_idx - idx + 1),
                    })
                    idx = end_idx + 1
                    continue
            elif metric_kind == "volume_drop":
                if baseline < min_edge_volume:
                    idx += 1
                    continue
                threshold = min(baseline * 0.5, baseline - 10.0)
                threshold = max(threshold, 0.0)
                if all(float(v) <= threshold for v in future_vals):
                    end_idx = idx + sustain_buckets - 1
                    k = idx + sustain_buckets
                    while k < n_rows and float(values.iloc[k]) <= threshold:
                        end_idx = k
                        k += 1
                    episodes.append({
                        "metric": metric_kind,
                        "onset": buckets.iloc[idx],
                        "end": buckets.iloc[end_idx],
                        "baseline": baseline,
                        "trough": float(values.iloc[idx:end_idx + 1].min()),
                        "threshold": threshold,
                        "drop_pct": max(0.0, (baseline - float(values.iloc[idx:end_idx + 1].min())) / max(baseline, 1.0) * 100.0),
                        "duration_buckets": int(end_idx - idx + 1),
                        "duration_minutes": float(end_idx - idx + 1),
                    })
                    idx = end_idx + 1
                    continue
            idx += 1
        if not episodes:
            return None
        strongest_raw = max(
            episodes,
            key=lambda e: (
                float(e.get("peak", 0.0) if e.get("peak", None) is not None else e.get("drop_pct", 0.0)),
                float(e.get("duration_minutes", 0.0)),
            ),
        )
        # NOTE:
        # strongest_raw is one element of episodes. If we attach episodes directly
        # onto that same dict, it creates a self-reference (circular JSON object)
        # when strongest_raw is inside episodes. Return detached copies instead.
        strongest = dict(strongest_raw)
        strongest["episodes"] = [dict(ep) for ep in episodes]
        strongest["episode_count"] = len(episodes)
        return strongest

    def _detect_trace_edge_anomalies(
        self,
        edge_metric_df: _pd.DataFrame,
        *,
        gap_metric_df: _pd.DataFrame | None = None,
        remote_metric_df: _pd.DataFrame | None = None,
        baseline_buckets: int = 5,
        sustain_buckets: int = 2,
        min_edge_volume: int = 20,
    ) -> list[dict]:
        """Detect anomalous caller->callee edges from error, gap, and remote metrics."""
        if edge_metric_df.empty:
            return []

        merged_df = edge_metric_df.copy()
        if gap_metric_df is not None and not gap_metric_df.empty:
            merged_df = merged_df.merge(
                gap_metric_df[["caller", "callee", "bucket", "gap_p50"]],
                on=["caller", "callee", "bucket"],
                how="left",
            )
        if remote_metric_df is not None and not remote_metric_df.empty:
            merged_df = merged_df.merge(
                remote_metric_df[["caller", "callee", "bucket", "remote_p50"]],
                on=["caller", "callee", "bucket"],
                how="left",
            )

        anomalies: list[dict] = []
        grouped = merged_df.groupby(["caller", "callee"], sort=True)
        for (caller, callee), group in grouped:
            if not caller or not callee:
                continue
            latency_info = None
            error_info = self._detect_trace_metric_onset(
                group,
                "error_rate",
                "error_rate",
                baseline_buckets=baseline_buckets,
                sustain_buckets=sustain_buckets,
                min_edge_volume=min_edge_volume,
            )
            gap_info = None
            if "gap_p50" in group.columns and group["gap_p50"].notna().any():
                gap_info = self._detect_trace_metric_onset(
                    group,
                    "gap_p50",
                    "latency",
                    baseline_buckets=baseline_buckets,
                    sustain_buckets=sustain_buckets,
                    min_edge_volume=min_edge_volume,
                )
            remote_info = None
            if "remote_p50" in group.columns and group["remote_p50"].notna().any():
                remote_info = self._detect_trace_metric_onset(
                    group,
                    "remote_p50",
                    "latency",
                    baseline_buckets=baseline_buckets,
                    sustain_buckets=sustain_buckets,
                    min_edge_volume=min_edge_volume,
                )
            if not error_info and not gap_info and not remote_info:
                continue

            metric_infos = {
                "error_rate": error_info,
                "gap": gap_info,
                "remote": remote_info,
            }
            onset_candidates = [
                info["onset"] for info in metric_infos.values() if info is not None
            ]
            first_onset = min(onset_candidates)
            earliest_metrics = [
                name for name, info in {"error_rate": error_info, "gap": gap_info}.items()
                if info is not None
                and abs((info["onset"] - first_onset).total_seconds()) / 60.0 <= 1.5
            ]
            dominant_metric = "mixed"
            if "gap" in earliest_metrics:
                dominant_metric = "gap_edge"
            elif "error_rate" in earliest_metrics:
                dominant_metric = "error_edge"

            latency_score = 0.0
            if latency_info:
                latency_score = (
                    max(latency_info["peak"] - latency_info["baseline"], 0.0)
                    / max(latency_info["baseline"], 1.0)
                )
            error_score = 0.0
            if error_info:
                error_score = (
                    max(error_info["peak"] - error_info["baseline"], 0.0)
                    / max(error_info["baseline"], 1.0)
                )
            gap_score = 0.0
            if gap_info:
                gap_score = (
                    max(gap_info["peak"] - gap_info["baseline"], 0.0)
                    / max(gap_info["baseline"], 1.0)
                )
            remote_score = 0.0
            if remote_info:
                remote_score = (
                    max(remote_info["peak"] - remote_info["baseline"], 0.0)
                    / max(remote_info["baseline"], 1.0)
                )

            anomalies.append(
                {
                    "edge_id": f"{caller}->{callee}",
                    "caller": caller,
                    "callee": callee,
                    "first_onset": first_onset,
                    "dominant_metric": dominant_metric,
                    "latency_info": latency_info,
                    "error_info": error_info,
                    "gap_info": gap_info,
                    "remote_info": remote_info,
                    "series": group.sort_values("bucket").reset_index(drop=True),
                    "score": latency_score + error_score + gap_score + remote_score,
                }
            )

        return sorted(
            anomalies,
            key=lambda item: (item["first_onset"], -item["score"], item["edge_id"]),
        )

    def _select_trace_path_subgraph(
        self,
        anomalies: list[dict],
        *,
        top_k_paths: int = 5,
        onset_slack_minutes: int = 3,
        path_slack_minutes: int = 5,
    ) -> list[dict]:
        """Return all anomalous edges without onset/top-k filtering."""
        if not anomalies:
            return []
        return sorted(
            anomalies,
            key=lambda item: (item["first_onset"], -item["score"], item["edge_id"]),
        )

    def _compute_trace_path_layout(
        self,
        selected_edges: list[dict],
        extra_edges: list[tuple[str, str]] | None = None,
    ) -> dict[str, tuple[float, float]]:
        """Layout nodes left-to-right by causal depth. Uses both anomalous and normal edges."""
        nodes = sorted(
            {item["caller"] for item in selected_edges} | {item["callee"] for item in selected_edges}
        )
        outgoing_nodes: dict[str, list[str]] = {node: [] for node in nodes}
        for item in selected_edges:
            caller = item["caller"]
            callee = item["callee"]
            if callee not in outgoing_nodes[caller]:
                outgoing_nodes[caller].append(callee)
        if extra_edges:
            for caller, callee in extra_edges:
                if caller not in outgoing_nodes:
                    outgoing_nodes[caller] = []
                    nodes = sorted(set(nodes) | {caller, callee})
                if callee not in outgoing_nodes:
                    outgoing_nodes[callee] = []
                    nodes = sorted(set(nodes) | {caller, callee})
                if callee not in outgoing_nodes[caller]:
                    outgoing_nodes[caller].append(callee)
        if not nodes:
            return {}

        indegree = {node: 0 for node in nodes}
        for caller, callees in outgoing_nodes.items():
            for callee in callees:
                if callee in indegree:
                    indegree[callee] += 1

        depth = {node: 0 for node in nodes}
        queue = [node for node in nodes if indegree[node] == 0]
        visited = set(queue)
        while queue:
            node = queue.pop(0)
            for nxt in outgoing_nodes.get(node, []):
                depth[nxt] = max(depth[nxt], depth[node] + 1)
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
                    visited.add(nxt)

        for node in nodes:
            if node not in visited:
                depth[node] = max(depth.values(), default=0)

        layers: dict[int, list[str]] = {}
        for node in nodes:
            layers.setdefault(depth[node], []).append(node)

        positions: dict[str, tuple[float, float]] = {}
        for layer_idx, layer_nodes in sorted(layers.items()):
            layer_nodes = sorted(layer_nodes)
            count = len(layer_nodes)
            for row_idx, node in enumerate(layer_nodes):
                y = (count - 1) / 2.0 - row_idx
                positions[node] = (layer_idx * 3.4, y * 1.7)
        return positions

    def _edge_plot_color(self, dominant_metric: str) -> str:
        """Color-code anomalous edges by anomaly type."""
        return {
            "error_edge": "#d62728",
            "latency_edge": "#ff7f0e",
            "gap_edge": "#8c564b",
        }.get(dominant_metric, "#7f7f7f")

    def _edge_summary_label(self, item: dict) -> str:
        """Compact caller->callee edge label for legends and annotations."""
        return f"{item['caller']} -> {item['callee']}"

    def _edge_graph_label(self, item: dict) -> str:
        """Compact edge annotation for the path graph."""
        onset = item["first_onset"].strftime("%H:%M")
        parts = [onset]
        if item.get("latency_info"):
            d = int(float(item["latency_info"].get("duration_minutes", 0.0)))
            c = int(item["latency_info"].get("episode_count", 1))
            parts.append(f"L {item['latency_info']['peak']:.0f}ms D{d}m x{c}")
        if item.get("error_info"):
            d = int(float(item["error_info"].get("duration_minutes", 0.0)))
            c = int(item["error_info"].get("episode_count", 1))
            parts.append(f"E {item['error_info']['peak']:.0f}% D{d}m x{c}")
        if item.get("gap_info"):
            d = int(float(item["gap_info"].get("duration_minutes", 0.0)))
            c = int(item["gap_info"].get("episode_count", 1))
            parts.append(f"G {item['gap_info']['peak']:.0f}ms D{d}m x{c}")
        return "\n".join(parts)

    def _remote_node_peak_map(self, selected_edges: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for item in selected_edges:
            info = item.get("remote_info")
            callee = item.get("callee")
            if not info or not callee:
                continue
            try:
                peak = float(info.get("peak", 0.0))
            except Exception:
                peak = 0.0
            prev = out.get(callee)
            if prev is None or peak > prev:
                out[callee] = peak
        return out

    def _remote_node_duration_map(self, selected_edges: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in selected_edges:
            info = item.get("remote_info")
            callee = item.get("callee")
            if not info or not callee:
                continue
            try:
                dur = int(float(info.get("duration_minutes", 0.0)))
            except Exception:
                dur = 0
            prev = out.get(callee, 0)
            if dur > prev:
                out[callee] = dur
        return out

    def _plot_trace_anomalous_path_figure(
        self,
        selected_edges: list[dict],
        out_path: str,
        *,
        window_minutes: int,
        edge_metric_df: _pd.DataFrame | None = None,
    ) -> str:
        """Render path graph only (no latency/error/volume charts). Includes normal edges in gray."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D

        anomalous_edge_ids = {item["edge_id"] for item in selected_edges}
        nodes = {item["caller"] for item in selected_edges} | {item["callee"] for item in selected_edges}
        normal_edges: list[tuple[str, str]] = []
        if edge_metric_df is not None and not edge_metric_df.empty:
            for (caller, callee), _ in edge_metric_df.groupby(["caller", "callee"], sort=True):
                eid = f"{caller}->{callee}"
                if eid not in anomalous_edge_ids and (caller in nodes or callee in nodes):
                    nodes.add(caller)
                    nodes.add(callee)
                    normal_edges.append((caller, callee))

        fig = plt.figure(figsize=(14, 8))
        ax_graph = fig.add_subplot(111)

        positions = self._compute_trace_path_layout(selected_edges, extra_edges=normal_edges)
        ax_graph.set_axis_off()

        all_x = [pos[0] for pos in positions.values()] or [0.0]
        all_y = [pos[1] for pos in positions.values()] or [0.0]
        gray = "#9E9E9E"
        for idx, (caller, callee) in enumerate(normal_edges):
            if caller in positions and callee in positions:
                sx, sy = positions[caller]
                tx, ty = positions[callee]
                rad = 0.06 if idx % 2 == 0 else -0.06
                ax_graph.annotate(
                    "",
                    xy=(tx - 0.35, ty),
                    xytext=(sx + 0.35, sy),
                    arrowprops={
                        "arrowstyle": "->",
                        "lw": 1.2,
                        "color": gray,
                        "alpha": 0.6,
                        "connectionstyle": f"arc3,rad={rad}",
                    },
                    zorder=0,
                )
        for idx, item in enumerate(selected_edges):
            sx, sy = positions[item["caller"]]
            tx, ty = positions[item["callee"]]
            color = self._edge_plot_color(item["dominant_metric"])
            rad = 0.08 if idx % 2 == 0 else -0.08
            ax_graph.annotate(
                "",
                xy=(tx - 0.35, ty),
                xytext=(sx + 0.35, sy),
                arrowprops={
                    "arrowstyle": "->",
                    "lw": 2.0,
                    "color": color,
                    "alpha": 0.9,
                    "connectionstyle": f"arc3,rad={rad}",
                },
                zorder=1,
            )
            mx = (sx + tx) / 2.0
            my = (sy + ty) / 2.0 + (0.22 if idx % 2 == 0 else -0.22)
            ax_graph.text(
                mx,
                my,
                self._edge_graph_label(item),
                fontsize=8,
                ha="center",
                va="center",
                color=color,
                bbox={
                    "boxstyle": "round,pad=0.18",
                    "fc": "white",
                    "ec": "none",
                    "alpha": 0.85,
                },
                zorder=3,
            )

        remote_node_peak = self._remote_node_peak_map(selected_edges)
        remote_node_dur = self._remote_node_duration_map(selected_edges)
        for node, (x, y) in positions.items():
            is_remote_node = node in remote_node_peak
            node_text = node
            if is_remote_node:
                d = remote_node_dur.get(node, 0)
                node_text = f"{node}\nR {remote_node_peak[node]:.0f}ms D{d}m"
            ax_graph.text(
                x,
                y,
                node_text,
                ha="center",
                va="center",
                fontsize=10,
                bbox={
                    "boxstyle": "round,pad=0.35",
                    "fc": "#eef7d1" if is_remote_node else "#F7F8FA",
                    "ec": "#4F5B67",
                    "lw": 1.2,
                },
                zorder=4,
            )

        dominant_metrics = {str(item.get("dominant_metric", "")) for item in selected_edges}
        graph_legend = []
        if "latency_edge" in dominant_metrics:
            graph_legend.append(Line2D([0], [0], color="#ff7f0e", lw=2, label="latency edge"))
        if "error_edge" in dominant_metrics:
            graph_legend.append(Line2D([0], [0], color="#d62728", lw=2, label="error edge"))
        if "gap_edge" in dominant_metrics:
            graph_legend.append(Line2D([0], [0], color="#8c564b", lw=2, label="gap edge"))
        if remote_node_peak:
            graph_legend.append(Line2D([0], [0], marker="s", markersize=10, markerfacecolor="#eef7d1", color="none", label="remote node"))
        if normal_edges:
            graph_legend.append(Line2D([0], [0], color=gray, lw=1.5, label="normal"))
        if graph_legend:
            ax_graph.legend(handles=graph_legend, loc="upper right", frameon=False, fontsize=8)
        ax_graph.set_title(
            f"Anomalous caller-callee paths (strongest episode per edge, {window_minutes}-minute window)",
            fontsize=13,
            fontweight="bold",
            pad=12,
        )
        ax_graph.set_xlim(min(all_x) - 1.3, max(all_x) + 1.3)
        ax_graph.set_ylim(min(all_y) - 1.4, max(all_y) + 1.4)

        plt.tight_layout()
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return out_path

    @visualization
    @trace_action
    def get_trace_anomalous_path_graph(
        self,
        namespace: str,
        window_minutes: int = 30,
        top_k_paths: int = 5,
        min_edge_volume: int = 20,
        onset_slack_minutes: int = 3,
        sustain_buckets: int = 2,
    ) -> str:
        """Find anomalous caller->callee paths and visualize the strongest episode per edge.

        This action scans the trace window and detects sustained anomalies on:
        - error-rate edges (caller->callee)
        - network-gap edges (caller->callee)
        - remote-process-time nodes (callee service)
        Then it renders one directed graph with anomalous edges plus normal context edges.

        Args:
            namespace: e.g. "static-telecom"
            window_minutes: inspect only the last N minutes of the trace window
            top_k_paths: retained for backward compatibility (currently not used for filtering)
            min_edge_volume: minimum spans/min required to trust an edge signal
            onset_slack_minutes: retained for backward compatibility (currently not used for filtering)
            sustain_buckets: require anomaly to persist for this many 1-minute buckets

        Returns:
            PNG path plus a short textual summary of anomalous paths.
        """
        renderer = get_trace_path_renderer(self.problem_id)
        return renderer.render(
            self,
            namespace,
            window_minutes=window_minutes,
            top_k_paths=top_k_paths,
            min_edge_volume=min_edge_volume,
            onset_slack_minutes=onset_slack_minutes,
            sustain_buckets=sustain_buckets,
        )

    @visualization
    @trace_action
    def get_trace_volume_graph(self, namespace: str) -> str:
        """Plot total trace span volume per minute across the query window.

        Useful as a first screening step — network faults typically cause
        a sharp volume drop even when latency/error signals are absent.

        Args:
            namespace: e.g. "static-telecom"

        Returns:
            File path to the generated PNG image.
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        df = self._load_trace_window(namespace)
        if df.empty:
            return f"No trace data found for namespace '{namespace}'"

        fig, ax = plt.subplots(figsize=(14, 5))
        vol = df.groupby("bucket").size()
        ax.bar(vol.index, vol.values,
               width=_pd.Timedelta(seconds=50), alpha=0.7, color='steelblue')

        ax.set_title("Trace Volume per Minute", fontsize=13, fontweight="bold")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("Span Count")
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        plt.tight_layout()
        out_dir = self.save_dir or _os.path.join(self.work_dir, "static_metric_output")
        _os.makedirs(out_dir, exist_ok=True)
        fname = "trace_volume.png"
        file_path = _os.path.join(out_dir, fname)
        fig.savefig(file_path, dpi=150, bbox_inches="tight")
        plt.close(fig)

        # Add summary stats
        baseline = vol.median()
        min_val = vol.min()
        min_bucket = vol.idxmin().strftime("%H:%M")
        drop_pct = (1 - min_val / baseline) * 100 if baseline > 0 else 0

        summary = (
            f"{file_path}\n\n"
            f"Baseline volume (median): {int(baseline)} spans/min\n"
            f"Minimum volume: {int(min_val)} spans/min at {min_bucket} UTC "
            f"({drop_pct:.0f}% drop)"
        )
        return summary

    def _select_trace_peer_subset(self, namespace: str, component_type: str, role: str):
        """Load trace data and restrict it to peers matching the requested level."""
        df = self._load_trace_window(namespace)
        if df.empty:
            return df, f"No trace data found for namespace '{namespace}'", "cmdb_id"

        comp_col = "cmdb_id" if role == "caller" else "dsName"
        if component_type in {"all", "*", "any"}:
            sub = df[df[comp_col].fillna("").astype(str).str.strip() != ""]
            if sub.empty:
                return sub, f"No trace data for {component_type} ({role})", comp_col
            return sub, "", comp_col

        level_members = self.component_levels.get(component_type)
        if level_members:
            level_set = set(level_members)
            sub = df[df[comp_col].fillna("").isin(level_set)]
            if sub.empty:
                # Suffix match for "node-X.pod_name" format
                sub = df[df[comp_col].fillna("").apply(
                    lambda x: str(x).split(".", 1)[-1] if "." in str(x) else ""
                ).isin(level_set)]
        else:
            sub = df[df[comp_col].fillna("").str.startswith(component_type + "_")]
            if sub.empty:
                sub = df[df[comp_col].fillna("").str.startswith(component_type + "-")]

        if sub.empty:
            return sub, f"No trace data for {component_type} ({role})", comp_col
        return sub, "", comp_col

    def _plot_trace_peer_metric(
        self,
        namespace: str,
        component_type: str,
        role: str,
        metric: str,
        title_suffix: str,
        ylabel: str,
        fname_suffix: str,
    ) -> str:
        """Render a single trace metric per peer so analysts can inspect it alone."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        sub, error, comp_col = self._select_trace_peer_subset(
            namespace, component_type, role,
        )
        if error:
            return error

        components = sorted(sub[comp_col].unique())
        fig, ax = plt.subplots(figsize=(14, 5))
        label_points: list[tuple[object, float, str, str]] = []

        for ci, comp in enumerate(components):
            comp_df = sub[sub[comp_col] == comp]
            color = _PEER_COLORS[ci % len(_PEER_COLORS)]
            ls = _PEER_LINESTYLES[ci // len(_PEER_COLORS)]

            if metric == "latency":
                series = comp_df.groupby("bucket")["elapsedTime"].median()
            elif metric == "error_rate":
                series = comp_df.groupby("bucket")["success_bool"].apply(
                    lambda s: (1 - s.mean()) * 100
                )
            elif metric == "volume":
                series = comp_df.groupby("bucket").size()
            else:
                raise ValueError(f"Unsupported trace peer metric: {metric}")

            ax.plot(
                series.index,
                series.values,
                label=comp,
                linewidth=0.8,
                alpha=0.85,
                color=color,
                linestyle=ls,
                marker="o",
                markersize=2.5,
            )

            if metric == "latency" and len(series.index) > 0:
                try:
                    peak_idx = series.idxmax()
                    peak_val = float(series.max())
                    label_points.append((peak_idx, peak_val, str(comp), color))
                except Exception:
                    pass

        if metric == "latency" and label_points:
            y_min, y_max = ax.get_ylim()
            y_span = max(y_max - y_min, 1.0)
            x_thresh = 1.0 / (24.0 * 60.0)  # ~1 minute in matplotlib date units
            y_thresh = max(0.02 * y_span, 5.0)
            label_points.sort(
                key=lambda item: (mdates.date2num(item[0]), float(item[1]))
            )
            cluster_offsets: list[int] = []
            placed_xy: list[tuple[float, float]] = []
            for peak_idx, peak_val, _comp, _color in label_points:
                x_num = mdates.date2num(peak_idx)
                offset_level = 0
                for placed_x, placed_y in placed_xy:
                    if abs(x_num - placed_x) <= x_thresh and abs(peak_val - placed_y) <= y_thresh:
                        offset_level += 1
                cluster_offsets.append(offset_level)
                placed_xy.append((x_num, peak_val))

            for (peak_idx, peak_val, comp, color), offset_level in zip(label_points, cluster_offsets):
                ax.text(
                    peak_idx,
                    peak_val + offset_level * (0.03 * y_span),
                    comp,
                    fontsize=7,
                    color=color,
                    alpha=0.9,
                    ha="left",
                    va="bottom",
                    clip_on=True,
                )

        ax.set_title(
            f"{component_type} {role} — {title_suffix}",
            fontsize=13,
            fontweight="bold",
        )
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.legend(loc="upper right", fontsize=7, ncol=2)

        plt.tight_layout()
        out_dir = self.save_dir or _os.path.join(self.work_dir, "static_metric_output")
        _os.makedirs(out_dir, exist_ok=True)
        fname = f"trace_{component_type}_{role}_{fname_suffix}.png"
        file_path = _os.path.join(out_dir, fname)
        fig.savefig(file_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return file_path

    @visualization
    @trace_action
    def get_trace_latency_peer_graph(self, namespace: str, component_type: str,
                                     role: str = "caller") -> str:
        """Plot trace latency (p50) per component on its own graph.

        Use this when you want to inspect latency spikes separately from
        error-rate behavior.

        Args:
            namespace: e.g. "static-telecom"
            component_type: "docker", "os", "db", "redis"
            role: "caller" (cmdb_id) or "callee" (dsName)

        Returns:
            File path to the generated PNG image.
        """
        return self._plot_trace_peer_metric(
            namespace=namespace,
            component_type=component_type,
            role=role,
            metric="latency",
            title_suffix="Trace Latency (p50)",
            ylabel="Elapsed Time (ms)",
            fname_suffix="latency",
        )

    @visualization
    @trace_action
    def get_trace_error_peer_graph(self, namespace: str, component_type: str,
                                   role: str = "caller") -> str:
        """Plot trace error rate per component on its own graph.

        Use this when you want to inspect errors separately from latency
        so a large latency spike does not dominate the visual scan.

        Args:
            namespace: e.g. "static-telecom"
            component_type: "docker", "os", "db", "redis"
            role: "caller" (cmdb_id) or "callee" (dsName)

        Returns:
            File path to the generated PNG image.
        """
        return self._plot_trace_peer_metric(
            namespace=namespace,
            component_type=component_type,
            role=role,
            metric="error_rate",
            title_suffix="Trace Error Rate",
            ylabel="Error Rate (%)",
            fname_suffix="error_rate",
        )

    @visualization
    @trace_action
    def get_trace_volume_peer_graph(self, namespace: str, component_type: str,
                                    role: str = "caller") -> str:
        """Plot trace span volume per component for a specific peer family.

        Use this when you want trace volume split by peer type (for example
        docker callers or db callees) instead of a single global total-volume chart.

        Args:
            namespace: e.g. "static-telecom"
            component_type: "docker", "os", "db", "redis"
            role: "caller" (cmdb_id) or "callee" (dsName)

        Returns:
            File path to the generated PNG image.
        """
        return self._plot_trace_peer_metric(
            namespace=namespace,
            component_type=component_type,
            role=role,
            metric="volume",
            title_suffix="Trace Volume",
            ylabel="Span Count",
            fname_suffix="volume",
        )

    @visualization
    @trace_action
    def get_trace_peer_graph(self, namespace: str, component_type: str,
                             role: str = "caller") -> str:
        """Plot trace latency (p50) and error rate for peers of a component type.

        Args:
            namespace: e.g. "static-telecom"
            component_type: "docker", "os", "db", "redis"
            role: "caller" (cmdb_id) or "callee" (dsName)

        Returns:
            File path to the generated PNG image.
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        sub, error, comp_col = self._select_trace_peer_subset(
            namespace, component_type, role,
        )
        if error:
            return error

        components = sorted(sub[comp_col].unique())

        fig, (ax_lat, ax_err) = plt.subplots(2, 1, figsize=(14, 10), sharex=True)

        for ci, comp in enumerate(components):
            comp_df = sub[sub[comp_col] == comp]
            color = _PEER_COLORS[ci % len(_PEER_COLORS)]
            ls = _PEER_LINESTYLES[ci // len(_PEER_COLORS)]

            lat = comp_df.groupby("bucket")["elapsedTime"].median()
            ax_lat.plot(
                lat.index,
                lat.values,
                label=comp,
                linewidth=0.8,
                alpha=0.85,
                color=color,
                linestyle=ls,
                marker="o",
                markersize=2.5,
            )
            # 라벨을 시계열 끝이 아니라, latency가 최대인 지점 근처에 붙여서
            # 서로 겹치지 않고 어떤 선이 어떤 컴포넌트인지 더 쉽게 구분할 수 있게 한다.
            if len(lat.index) > 0:
                try:
                    peak_idx = lat.idxmax()
                    peak_val = float(lat.max())
                    ax_lat.text(
                        peak_idx,
                        peak_val,
                        str(comp),
                        fontsize=7,
                        color=color,
                        alpha=0.9,
                        ha="left",
                        va="bottom",
                        clip_on=True,
                    )
                except Exception:
                    # 라벨링에 실패해도 그래프 자체는 그려지도록 한다.
                    pass

            err = comp_df.groupby("bucket")["success_bool"].apply(
                lambda s: (1 - s.mean()) * 100
            )
            ax_err.plot(
                err.index,
                err.values,
                label=comp,
                linewidth=0.8,
                alpha=0.85,
                color=color,
                linestyle=ls,
                marker="o",
                markersize=2.5,
            )

        ax_lat.set_title(f"{component_type} {role} — Trace Latency (p50)",
                         fontsize=13, fontweight="bold")
        ax_lat.set_ylabel("Elapsed Time (ms)")
        ax_lat.grid(True, alpha=0.3)
        ax_lat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_lat.legend(loc="upper right", fontsize=7, ncol=2)

        ax_err.set_title(f"{component_type} {role} — Trace Error Rate",
                         fontsize=13, fontweight="bold")
        ax_err.set_xlabel("Time (UTC)")
        ax_err.set_ylabel("Error Rate (%)")
        ax_err.grid(True, alpha=0.3)
        ax_err.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_err.legend(loc="upper right", fontsize=7, ncol=2)

        plt.tight_layout()
        out_dir = self.save_dir or _os.path.join(self.work_dir, "static_metric_output")
        _os.makedirs(out_dir, exist_ok=True)
        fname = f"trace_{component_type}_{role}.png"
        file_path = _os.path.join(out_dir, fname)
        fig.savefig(file_path, dpi=150, bbox_inches="tight")
        plt.close(fig)

        return file_path

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
        EXPECTED_KEYS = {
            "root cause occurrence datetime",
            "root cause component",
            "root cause reason",
        }
        DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

        errors = []
        for key, entry in prediction.items():
            if not isinstance(entry, dict):
                errors.append(
                    f"  [{key}] Value must be a dict with keys: {EXPECTED_KEYS}"
                )
                continue

            # --- Check for wrong/missing field names ---
            entry_keys = set(entry.keys())
            unknown_keys = entry_keys - EXPECTED_KEYS
            missing_keys = EXPECTED_KEYS - entry_keys
            if unknown_keys:
                errors.append(
                    f"  [{key}] Unknown field(s): {unknown_keys}.\n"
                    f"       Expected exactly: {EXPECTED_KEYS}"
                )
            if missing_keys:
                errors.append(
                    f"  [{key}] Missing field(s): {missing_keys}.\n"
                    f"       Expected exactly: {EXPECTED_KEYS}"
                )

            # --- Validate datetime format ---
            dt_str = entry.get("root cause occurrence datetime", "")
            if dt_str:
                try:
                    datetime.strptime(dt_str.strip(), DATETIME_FMT)
                except ValueError:
                    errors.append(
                        f"  [{key}] Invalid datetime format: '{dt_str}'.\n"
                        f"       Must be '{DATETIME_FMT}' (e.g. '2020-05-23 16:10:00')"
                    )

            # --- Validate component ---
            component = entry.get("root cause component", "")
            if self.possible_components and component and component not in self.possible_components:
                errors.append(
                    f"  [{key}] Invalid 'root cause component': '{component}'.\n"
                    f"       Must be one of: {self.possible_components}"
                )

            # --- Validate reason ---
            reason = entry.get("root cause reason", "")
            if self.possible_reasons and reason and reason not in self.possible_reasons:
                errors.append(
                    f"  [{key}] Invalid 'root cause reason': '{reason}'.\n"
                    f"       Must be one of: {self.possible_reasons}"
                )

        if errors:
            return (
                "Submission rejected - invalid values:\n"
                + "\n".join(errors)
                + "\n\nPlease correct and resubmit with the exact format:\n"
                + 'submit({"1": {"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", '
                + '"root cause component": "<component>", "root cause reason": "<reason>"}})'
            )

        return SubmissionStatus.VALID_SUBMISSION
