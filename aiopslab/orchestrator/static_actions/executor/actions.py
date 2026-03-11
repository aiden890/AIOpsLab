"""Self-contained executor action for OpenRCA static dataset tasks.

Extends StaticRCAActions so that execute() owns its own IPython kernel,
TelemetryHelper, executor conversation history, and LLM config.

No injected callback is needed — the runner calls setup_executor() once
and the action handles everything internally.
"""

import json
import logging
from pathlib import Path
from IPython.terminal.embed import InteractiveShellEmbed

# Minimal nbformat-compatible notebook structure (no nbformat dependency needed)
_NOTEBOOK_TEMPLATE = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3"},
    },
    "cells": [],
}

from aiopslab.orchestrator.static_actions.rca import (
    StaticRCAActions, _normalize_trace_schema, _compute_network_gap, _to_unix_ts,
)
from aiopslab.orchestrator.static_actions.executor.helper import TelemetryHelper
from aiopslab.utils.actions import (
    action, executor_action, hypothesis_action,
    hypothesis_executor_action, hypothesis_trace_action,
)
from aiopslab.orchestrator.static_actions.executor.api_router import load_config
from aiopslab.orchestrator.static_actions.executor.runner import execute_act


class StaticRCAActionsWithExecutor(StaticRCAActions):
    """StaticRCAActions where execute() is fully self-contained.

    The IPython kernel, TelemetryHelper, executor history, and LLM config
    live inside this object. Call setup_executor() once before running.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._kernel = None
        self._executor_history = []
        self._executor_trajectory = []  # [{step, action, instruction, code, result, success}]
        self._trajectory_path: Path | None = None
        self._notebook_path: Path | None = None
        self._background = ""
        self._configs = None
        self._logger = logging.getLogger("rca_executor")
        self._namespace = ""
        self._hypotheses: list[dict] = []
        self._relationships_analyzed: bool = False
        self._causal_graph_called: bool = False
        self._query_start: float | None = None
        self._query_end: float | None = None

    def setup_executor(
        self,
        background: str,
        api_config_path: str,
        namespace: str,
        logger=None,
        notebook_save_path: str | None = None,
        query_time_range: dict | None = None,
    ):
        """Initialize IPython kernel, TelemetryHelper, and LLM config.

        Args:
            background: Domain schema string for the Executor LLM prompt.
            api_config_path: Path to api_config.yaml for LLM credentials.
            namespace: AIOpsLab namespace (e.g. "static-bank").
            logger: Optional logger instance.
            notebook_save_path: Path to save generated code as a .ipynb notebook.
                Each execute() call appends a new cell. Created on first call.
        """
        self._background = background
        self._configs = load_config(api_config_path)
        self._namespace = namespace
        self._executor_history = []  # persists across all execute() calls
        self._hypotheses = []  # reset each problem
        self._relationships_analyzed = False
        self._causal_graph_called = False
        if query_time_range:
            self._query_start = float(query_time_range.get("start", 0) or 0)
            self._query_end = float(query_time_range.get("end", 0) or 0)
        else:
            self._query_start = None
            self._query_end = None
        if logger is not None:
            self._logger = logger

        if notebook_save_path is not None:
            self._notebook_path = Path(notebook_save_path)
            self._notebook_path.parent.mkdir(parents=True, exist_ok=True)
            # Write an empty notebook to start fresh
            import copy
            nb = copy.deepcopy(_NOTEBOOK_TEMPLATE)
            self._notebook_path.write_text(json.dumps(nb, indent=1))
        else:
            self._notebook_path = None

        enabled = self.enabled_telemetry_types  # frozenset or None
        all_enabled = enabled is None

        self._kernel = InteractiveShellEmbed()
        helper = TelemetryHelper(
            actions_obj=self,
            namespace=namespace,
            enable_log=all_enabled or "log" in enabled,
            enable_metric=all_enabled or "metric" in enabled,
            enable_trace=all_enabled or "trace" in enabled,
        )
        self._kernel.push({"telemetry": helper})
        self._kernel.run_cell(
            "import pandas as pd\n"
            "pd.set_option('display.width', 427)\n"
            "pd.set_option('display.max_columns', 10)\n"
        )

    def submit(self, prediction: dict):
        """Submit root cause analysis prediction.
        In hypothesis mode, requires at least 3 saved hypotheses before accepting.
        """
        hypothesis_mode = (
            self.enabled_telemetry_types is not None
            and "hypothesis" in self.enabled_telemetry_types
        )
        if hypothesis_mode:
            if len(self._hypotheses) < 3:
                current = len(self._hypotheses)
                needed = 3 - current
                return (
                    f"Submission rejected: only {current} hypothesis saved "
                    f"({needed} more needed). "
                    f"Use save_hypothesis() to record {needed} more candidate(s) "
                    f"before submitting. Investigate other component levels, "
                    f"trace call chains, or alternative reasons."
                )

            trace_mode = (
                self.enabled_telemetry_types is not None
                and "trace" in self.enabled_telemetry_types
            )
            if trace_mode and not self._causal_graph_called:
                saved = "\n".join(
                    f"  #{h['id']} [{h['confidence'].upper()}] "
                    f"{h['component']} | {h['reason']} | {h['datetime']}"
                    for h in self._hypotheses
                )
                return (
                    f"Submission rejected: you must call get_hypothesis_causal_graph() "
                    f"before submitting. This step shows the trace call chain and elapsed "
                    f"times connecting all hypothesis components.\n\n"
                    f"Current hypotheses ({len(self._hypotheses)} saved):\n{saved}\n\n"
                    f"Call get_hypothesis_causal_graph(namespace, start_time, end_time) to proceed."
                )

            executor_mode = (
                self.enabled_telemetry_types is not None
                and "executor" in self.enabled_telemetry_types
            )
            if executor_mode and not self._relationships_analyzed:
                saved = "\n".join(
                    f"  #{h['id']} [{h['confidence'].upper()}] "
                    f"{h['component']} | {h['reason']} | {h['datetime']}"
                    for h in self._hypotheses
                )
                return (
                    f"Submission rejected: you must call analyze_hypothesis_relationships() "
                    f"before submitting. This step reasons about which hypothesis is the "
                    f"root origin (not a downstream victim).\n\n"
                    f"Current hypotheses ({len(self._hypotheses)} saved):\n{saved}\n\n"
                    f"Call analyze_hypothesis_relationships(\"<your analysis instruction>\") to proceed."
                )

            high_confidence = [h for h in self._hypotheses
                               if h.get("confidence", "").lower() == "high"]
            if not high_confidence:
                saved = "\n".join(
                    f"  #{h['id']} [{h['confidence'].upper()}] "
                    f"{h['component']} | {h['reason']} | {h['datetime']}"
                    for h in self._hypotheses
                )
                return (
                    f"Submission rejected: none of the {len(self._hypotheses)} saved "
                    f"hypotheses has HIGH confidence. Current candidates:\n{saved}\n\n"
                    f"You must investigate further before submitting. Try a different approach:\n"
                )

        return super().submit(prediction)

    @executor_action
    def execute(self, instruction: str) -> str:
        """Generate and run Python code for custom telemetry analysis.

        The Executor LLM writes Python code from your instruction, runs it
        in a stateful IPython kernel, and returns a summarized result.
        Variables persist across calls — reuse them to avoid redundant fetches.
        Max 3 retries on execution error.

        Args:
            instruction: Detailed natural language description of what to analyze.
        """
        return self._run_executor_action(
            action_name="execute",
            instruction=instruction,
            output_mode="legacy",
        )

    @executor_action
    def execute_anomaly_report(self, instruction: str) -> str:
        """Generate and run Python code, returning a structured anomaly-report JSON.

        Use this API when you need machine-readable anomaly evidence instead of
        free-form summaries. Output is normalized JSON (no raw output section).
        Kernel state persists across calls, so cached DataFrames/variables can be reused.

        Expected top-level JSON fields in the response:
          - report_type: "anomaly_report"
          - component: target component
          - window_utc: {"start": "...", "end": "..."}
          - baseline_method: baseline strategy used
          - threshold_rule: anomaly threshold rule used
          - kpi_results: per-metric anomalies and sustained windows
          - target_timestamp_check: anomaly status at specific timestamp
          - data_quality: missing intervals and notes
          - summary: concise interpretation

        Args:
            instruction: Detailed natural language analysis request, including
                component(s), UTC window, baseline/threshold intent, and required outputs.
        """
        return self._run_executor_action(
            action_name="execute_anomaly_report",
            instruction=instruction,
            output_mode="anomaly_report",
        )

    def _run_executor_action(self, action_name: str, instruction: str, output_mode: str) -> str:
        if self._kernel is None:
            return "Error: Executor not initialized. Call setup_executor() first."

        code, result, success, self._executor_history = execute_act(
            instruction=instruction,
            background=self._background,
            history=self._executor_history,
            kernel=self._kernel,
            configs=self._configs,
            logger=self._logger,
            max_retries=3,
            output_mode=output_mode,
        )

        if not success:
            self._logger.warning("Executor self-correction exhausted all retries.")

        step = len(self._executor_trajectory) + 1
        self._executor_trajectory.append({
            "step": step,
            "action": action_name,
            "instruction": instruction,
            "code": code,
            "result": result,
            "success": success,
        })

        if self._notebook_path is not None:
            self._append_notebook_cell(step, instruction, code, result)

        return result

    def _append_notebook_cell(self, step: int, instruction: str, code: str, result: str):
        """Append a markdown header + code cell to the .ipynb notebook file."""
        try:
            nb = json.loads(self._notebook_path.read_text())

            # Markdown cell: step header and instruction
            nb["cells"].append({
                "cell_type": "markdown",
                "metadata": {},
                "source": f"## Step {step}\n\n**Instruction:** {instruction}",
            })

            # Code cell: generated Python code
            nb["cells"].append({
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": result,
                    }
                ],
                "source": code,
            })

            self._notebook_path.write_text(json.dumps(nb, indent=1))
        except Exception as exc:
            self._logger.warning(f"Failed to save notebook cell: {exc}")

    @hypothesis_action
    def save_hypothesis(self, component: str, reason: str, datetime: str,
                        confidence: str, evidence: str) -> str:
        """Save a root cause hypothesis candidate for later comparison.

        Call this whenever you find a plausible candidate but want to keep
        investigating before committing. Review all candidates with
        get_hypotheses() before submitting.

        Args:
            component: Candidate root cause component (must be from possible list).
            reason: Candidate root cause reason (must be from possible list).
            datetime: Fault occurrence datetime (YYYY-MM-DD HH:MM:SS).
                      MUST be an exact timestamp read from telemetry data — not estimated.
                      Source priority:
                        1. Metric: use peak_high_ts from get_kpi_high_deviation() or
                           peak_low_ts from get_kpi_low_deviation() — the exact timestamp
                           of the max/min KPI value in the fault window
                        2. Trace: startTime/timestamp of the first error or slow span
                        3. Log: timestamp of the first error/warning log entry
            confidence: "high", "medium", or "low".
            evidence: One-sentence summary including the telemetry source and exact timestamp,
                      e.g. "metric: os_012 CPU_util breached P95 at 2020-05-22 16:48:23"
                           "trace: db_003 span error at startTime=2020-05-22 16:47:55"
        """
        import re as _re
        unknown_markers = ("unknown", "unclear", "uncertain", "n/a", "none", "tbd", "")
        if reason.strip().lower() in unknown_markers or "unknown" in reason.lower():
            return (
                f"Hypothesis rejected: reason '{reason}' is too vague. "
                f"You must determine a specific reason from the possible reasons list "
                f"before saving. Investigate the component's metrics, traces, and logs further."
            )

        # Reject round-hour timestamps — they are almost certainly guessed, not read from data.
        # Real telemetry timestamps have non-zero seconds (e.g. "16:48:23"), not "16:00:00" or "16:30:00".
        import re as _re
        _round = _re.match(r"\d{4}-\d{2}-\d{2} \d{2}:(00|30):00$", datetime.strip())
        if _round:
            return (
                f"Hypothesis rejected: datetime '{datetime}' looks like a round estimate "
                f"(exact hour HH:00:00). Use the exact timestamp from telemetry: "
                f"peak_high_ts from get_kpi_high_deviation() or "
                f"peak_low_ts from get_kpi_low_deviation(). "
                f"If the table output was truncated, use execute() to extract the exact value."
            )

        # Validate datetime is within the query time range (if known)
        if self._query_start and self._query_end:
            import pandas as _pd2
            try:
                dt_ts = _pd2.Timestamp(datetime.strip()).timestamp()
                if dt_ts < self._query_start or dt_ts > self._query_end:
                    import datetime as _dt
                    start_str = _dt.datetime.utcfromtimestamp(self._query_start).strftime("%Y-%m-%d %H:%M:%S")
                    end_str = _dt.datetime.utcfromtimestamp(self._query_end).strftime("%Y-%m-%d %H:%M:%S")
                    return (
                        f"Hypothesis rejected: datetime '{datetime}' is outside the query "
                        f"time range [{start_str} ~ {end_str} UTC]. "
                        f"Use a timestamp from the fault window. "
                        f"Check peak_high_ts / peak_low_ts from KPI deviation actions."
                    )
            except Exception:
                pass  # unparseable datetime — let it through

        idx = len(self._hypotheses) + 1
        self._hypotheses.append({
            "id": idx,
            "component": component,
            "reason": reason,
            "datetime": datetime,
            "confidence": confidence,
            "evidence": evidence,
        })
        return (
            f"Hypothesis #{idx} saved: [{confidence.upper()}] "
            f"{component} | {reason} | {datetime}\n"
            f"Evidence: {evidence}"
        )

    @hypothesis_action
    def get_hypotheses(self) -> str:
        """Retrieve all saved root cause hypotheses, ranked by confidence.

        Use this to compare all candidates before deciding which to submit.
        Returns a ranked list of hypotheses saved via save_hypothesis().
        """
        if not self._hypotheses:
            return "No hypotheses saved yet. Use save_hypothesis() to record candidates as you find them."

        order = {"high": 0, "medium": 1, "low": 2}
        ranked = sorted(self._hypotheses,
                        key=lambda h: order.get(h["confidence"].lower(), 3))

        lines = [f"All hypotheses ({len(ranked)} total, ranked by confidence):\n"]
        for h in ranked:
            lines.append(
                f"  #{h['id']} [{h['confidence'].upper()}]  "
                f"{h['component']} | {h['reason']} | {h['datetime']}\n"
                f"    Evidence: {h['evidence']}"
            )
        return "\n".join(lines)

    @hypothesis_trace_action
    def get_hypothesis_causal_graph(self, namespace: str,
                                    start_time=None, end_time=None,
                                    components: list = None) -> str:
        """Build a causal trace graph focused on saved hypothesis components and extra components.

        Pass extra `components` to includ additional candidates alongside saved hypotheses.
        """
        import pandas as _pd
        if not self._hypotheses:
            return (
                "No hypotheses saved yet. Save candidates with save_hypothesis() first."
            )

        hyp_components = {h["component"] for h in self._hypotheses}
        # Merge in any extra components the caller wants to focus on
        if components:
            hyp_components = hyp_components | set(components)
        hyp_map = {h["component"]: h for h in self._hypotheses}

        raw_df = self.static_app.fetch_traces_df(namespace)
        if raw_df.empty:
            return f"No trace data found for namespace '{namespace}'."

        ts_col = "startTime" if "startTime" in raw_df.columns else "timestamp"
        start_ts = _to_unix_ts(start_time)
        end_ts = _to_unix_ts(end_time)
        if start_ts is not None:
            raw_df = raw_df[raw_df[ts_col] >= start_ts]
        if end_ts is not None:
            raw_df = raw_df[raw_df[ts_col] <= end_ts]

        if raw_df.empty:
            return "No traces found in the specified time window."

        norm_df = _normalize_trace_schema(raw_df)
        if norm_df.empty or "cmdb_id" not in norm_df.columns or "dsName" not in norm_df.columns:
            return "Could not build call graph from trace data (schema unsupported)."

        # Edge stats: (caller, callee) → call_count, avg_elapsed
        edge_stats = (
            norm_df.groupby(["cmdb_id", "dsName"])
            .agg(call_count=("startTime", "count"), avg_elapsed=("elapsedTime", "mean"))
            .reset_index()
        )

        # Network gap per component from raw spans
        gap_info = _compute_network_gap(raw_df)

        # Drop self-loops (intra-component spans — meaningless for call graph)
        edge_stats = edge_stats[edge_stats["cmdb_id"] != edge_stats["dsName"]]

        # Focus on edges where at least one endpoint is a hypothesis component
        mask = (
            edge_stats["cmdb_id"].isin(hyp_components)
            | edge_stats["dsName"].isin(hyp_components)
        )
        focused = edge_stats[mask]
        if focused.empty:
            focused = edge_stats  # fallback: show all if no direct match

        # Sort: hypothesis callees first (to show what's being called by others), then by elapsed
        focused = focused.copy()
        focused["_hyp_callee"] = focused["dsName"].isin(hyp_components).astype(int)
        focused = focused.sort_values(["_hyp_callee", "avg_elapsed"], ascending=[False, False])

        def _label(comp):
            if comp in hyp_map:
                h = hyp_map[comp]
                return f"{comp} [HYP#{h['id']}: {h['reason']}]"
            return comp


        lines = [
            f"Causal graph for '{namespace}':",
            f"Components: {', '.join(sorted(hyp_components))}",
            "",
            "Call chain (caller → callee)  avg_elapsed  net_gap (caller):",
        ]
        for _, row in focused.iterrows():
            caller, callee = row["cmdb_id"], row["dsName"]
            count = int(row["call_count"])
            elapsed = row["avg_elapsed"]
            g = gap_info.get(caller, {})
            gap_str = (
                f"  net_gap={g['avg_gap_ms']:.0f}ms/{g['avg_parent_dur_ms']:.0f}ms"
                f" ({g['gap_ratio']*100:.0f}%)"
                if g else ""
            )
            lines.append(
                f"  {_label(caller)}  →  {_label(callee)}"
                f"    calls={count}  avg_elapsed={elapsed:.0f}ms{gap_str}"
            )

        if gap_info:
            hyp_gaps = [(c, gap_info[c]) for c in sorted(hyp_components) if c in gap_info]
            if hyp_gaps:
                lines += ["", "Network gap per hypothesis component:"]
                for comp, g in hyp_gaps:
                    lines.append(
                        f"  {comp}: avg_gap={g['avg_gap_ms']:.0f}ms"
                        f" / avg_parent={g['avg_parent_dur_ms']:.0f}ms"
                        f"  gap_ratio={g['gap_ratio']*100:.0f}%"
                        f"  (n={g['span_count']} spans)"
                    )

        self._causal_graph_called = True
        return "\n".join(lines)

    @hypothesis_executor_action
    def analyze_hypothesis_relationships(self, instruction: str) -> str:
        """Analyze relationships between all saved hypotheses to identify the root origin.

            instruction: Describe what relationship analysis to perform.
        """
        if not self._hypotheses:
            return (
                "No hypotheses saved yet. Use save_hypothesis() to record candidates "
                "before analyzing relationships."
            )

        order = {"high": 0, "medium": 1, "low": 2}
        ranked = sorted(self._hypotheses,
                        key=lambda h: order.get(h["confidence"].lower(), 3))
        hyp_lines = [f"Saved hypotheses ({len(ranked)} total):"]
        for h in ranked:
            hyp_lines.append(
                f"  #{h['id']} [{h['confidence'].upper()}] "
                f"{h['component']} | {h['reason']} | {h['datetime']}\n"
                f"    Evidence: {h['evidence']}"
            )
        hyp_context = "\n".join(hyp_lines)

        enriched_instruction = (
            f"{hyp_context}\n\n"
            f"Relationship analysis task:\n{instruction}"
        )

        result = self.execute(enriched_instruction)
        self._relationships_analyzed = True
        return result

    def cleanup(self):
        """Release IPython kernel resources."""
        if self._kernel is not None:
            self._kernel.reset()
            self._kernel = None
