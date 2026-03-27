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
    metric_action, trace_action,
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
        self._executor_trajectory = []  # [{step, instruction, code, result, success}]
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
        # Anomaly queue: built by finalize_anomaly_queue(), consumed by get_next_anomaly()
        self._anomaly_queue: list[dict] = []
        self._anomaly_queue_index: int = 0
        self._anomaly_evidence: dict[str, dict] = {}
        # RAG injector: Callable[[str], str] that enriches instructions with metric context
        self._rag_injector = None
        # Result enricher: Callable[[str], str] that appends peer comparison to executor results
        self._result_enricher = None
        # Summary injector: Callable[[str], str] that adds fault context before executor summary
        self._summary_injector = None
        # Metrics enricher: Callable[[str], str] that appends anomaly summary to get_metrics result
        self._metrics_enricher = None
        # Traces enricher: Callable[[str], str] that appends trace anomaly summary to get_traces result
        self._traces_enricher = None

    def setup_executor(
        self,
        background: str,
        api_config_path: str,
        namespace: str,
        logger=None,
        notebook_save_path: str | None = None,
        query_time_range: dict | None = None,
        llm_configs: dict | None = None,
    ):
        """Initialize IPython kernel, TelemetryHelper, and LLM config.

        Args:
            background: Domain schema string for the Executor LLM prompt.
            api_config_path: Path to api_config.yaml for LLM credentials.
            namespace: AIOpsLab namespace (e.g. "static-bank").
            logger: Optional logger instance.
            notebook_save_path: Path to save generated code as a .ipynb notebook.
                Each execute() call appends a new cell. Created on first call.
            query_time_range: Optional dict with start/end for query window.
            llm_configs: Optional shared LLM config dict. When provided, this dict
                is used for executor LLM calls so that token usage (_in_tokens,
                _out_tokens) accumulates in the same place as controller and vision
                critic. If None, config is loaded from api_config_path.
        """
        self._background = background
        if llm_configs is not None:
            self._configs = llm_configs
        else:
            self._configs = load_config(api_config_path)
        self._namespace = namespace
        self._executor_history = []  # persists across all execute() calls
        self._hypotheses = []  # reset each problem
        self._relationships_analyzed = False
        self._causal_graph_called = False
        self._anomaly_queue = []
        self._anomaly_queue_index = 0
        self._anomaly_evidence = {}
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
            # Write notebook with executor system prompt as first cell
            import copy
            from aiopslab.orchestrator.static_actions.executor.prompts.executor_prompt import (
                system_template as _sys_tmpl, rule as _rule, code_format as _code_fmt,
                get_rule_for_namespace as _get_rule_for_namespace,
            )
            nb = copy.deepcopy(_NOTEBOOK_TEMPLATE)
            active_rule = _get_rule_for_namespace(namespace) if namespace else _rule
            executor_system_prompt = _sys_tmpl.format(
                rule=active_rule, background=background, format=_code_fmt,
            )
            nb["cells"].append({
                "cell_type": "markdown",
                "metadata": {},
                "source": f"## Executor System Prompt\n\n```\n{executor_system_prompt}\n```",
            })
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

    def set_rag_injector(self, injector):
        """Set a callable that enriches executor instructions with RAG context.

        Args:
            injector: A ``Callable[[str], str]`` that takes an instruction and
                returns an enriched instruction (or the original if no context).
        """
        self._rag_injector = injector

    def set_result_enricher(self, enricher):
        """Set a callable that appends peer comparison to executor results.

        Args:
            enricher: A ``Callable[[str], str]`` that takes an executor result
                and returns the result with peer comparison appended.
        """
        self._result_enricher = enricher

    def set_summary_injector(self, injector):
        """Set a callable that adds fault context before executor summary.

        Args:
            injector: A ``Callable[[str], str]`` that takes an executor raw
                result and returns it with fault context appended, so the
                executor LLM includes key findings in its summary.
        """
        self._summary_injector = injector

    def set_metrics_enricher(self, enricher):
        """Set a callable that appends anomaly summary to get_metrics results.

        Args:
            enricher: A ``Callable[[str], str]`` that takes a get_metrics
                result (file path) and returns it with anomaly summary appended.
        """
        self._metrics_enricher = enricher

    def get_metrics(self, namespace: str, start_time=None, end_time=None) -> str:
        """Fetches metrics data, saves to CSV, returns file path with anomaly summary. start_time/end_time: Unix timestamps (s)."""
        result = super().get_metrics(namespace, start_time=start_time, end_time=end_time)
        if self._metrics_enricher is not None and not result.startswith("No metrics"):
            result = self._metrics_enricher(result)
        return result

    def set_traces_enricher(self, enricher):
        """Set a callable that appends trace anomaly summary to get_traces results.

        Args:
            enricher: A ``Callable[[str], str]`` that takes a get_traces
                result (file path) and returns it with trace summary appended.
        """
        self._traces_enricher = enricher

    def get_traces(self, namespace: str, start_time=None, end_time=None) -> str:
        """Fetches trace data, saves to CSV, returns file path with trace summary. start_time/end_time: Unix timestamps (s)."""
        result = super().get_traces(namespace, start_time=start_time, end_time=end_time)
        if self._traces_enricher is not None and not result.startswith("No traces"):
            result = self._traces_enricher(result)
        return result

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

            executor_mode = (
                self.enabled_telemetry_types is not None
                and "executor" in self.enabled_telemetry_types
            )
            if executor_mode and not self._relationships_analyzed:
                saved = "\n".join(
                    f"  #{h['id']} [{str(h['confidence']).upper()}] "
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
                               if str(h.get("confidence", "")).lower() == "high"]
            if not high_confidence:
                saved = "\n".join(
                    f"  #{h['id']} [{str(h['confidence']).upper()}] "
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
        """Analyze telemetry data using a natural language instruction.

        instruction: Natural language describing what analysis to run on
        **telemetry CSV data**. The executor will
        generate and run Python code that loads CSV.
        Do NOT pass file paths to images (.png) or ask to "analyze image at ...".
        """
        if self._kernel is None:
            return "Error: Executor not initialized. Call setup_executor() first."

        _stall_phrases = (
            "no further", "no more", "no action", "nothing to do",
            "not required", "not needed", "complete", "done", "finished",
        )
        if any(p in instruction.lower() for p in _stall_phrases):
            return (
                "execute() requires a concrete analysis instruction. "
                "If your investigation is complete, call submit() with your findings:\n"
                '  submit({"1": {"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", '
                '"root cause component": "ts-<service>", '
                '"root cause reason": "<fault type>"}})'
            )

        if self._rag_injector is not None:
            instruction = self._rag_injector(instruction)

        code, result, success, self._executor_history = execute_act(
            instruction=instruction,
            background=self._background,
            history=self._executor_history,
            kernel=self._kernel,
            configs=self._configs,
            logger=self._logger,
            max_retries=3,
            summary_injector=self._summary_injector,
            namespace=self._namespace,
        )

        if not success:
            self._logger.warning("Executor self-correction exhausted all retries.")

        step = len(self._executor_trajectory) + 1
        self._executor_trajectory.append({
            "step": step,
            "instruction": instruction,
            "code": code,
            "result": result,
            "success": success,
        })

        if self._notebook_path is not None:
            self._append_notebook_cell(step, instruction, code, result)

        if self._result_enricher is not None and success:
            result = self._result_enricher(result)

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

    # @action
    def finalize_anomaly_queue(self) -> str:
        """Consolidate multi-pass anomaly detection results into a ranked service queue.

        Reads DataFrames saved by previous execute() calls directly from the IPython
        kernel namespace — no text parsing. Each execute() call must save its results
        to one of these named variables:
          anomaly_cpu  — CPU anomalies (cpu columns)
          anomaly_mem  — memory anomalies (mem columns)
          anomaly_lat  — trace latency anomalies
          anomaly_err  — trace error rate anomalies

        Each DataFrame must have columns: service (str), score (float), earliest_time.
        Optional: dominant_signal (str).
        """
        if self._kernel is None:
            return "Error: Executor not initialized. Call setup_executor() first."

        import pandas as _pd
        from collections import defaultdict as _dd

        # Mapping: kernel variable name → default dominant_signal label
        _VAR_SIGNAL = {
            "anomaly_cpu": "cpu",
            "anomaly_mem": "memory",
            "anomaly_lat": "trace_latency",
            "anomaly_err": "trace_error_rate",
        }

        import re as _re

        # Extract canonical service name — strips template/template-id suffixes like
        # "_1", "_172", "_logcount" that the executor may include in log DataFrames.
        _svc_pat = _re.compile(r"ts-[a-z0-9-]+-(?:service|mongo|mysql|dashboard)")

        def _canonical(raw: str) -> str:
            m = _svc_pat.search(raw)
            return m.group(0) if m else raw

        def _norm_time(raw: str) -> str:
            t = raw.replace("+00:00", "").replace(" UTC", "").strip()
            return t[:19] if len(t) > 19 else t

        ns = self._kernel.user_ns
        # all_entries: one entry per (variable, canonical_service) after aggregation
        all_entries = []

        for var, default_signal in _VAR_SIGNAL.items():
            df = ns.get(var)
            if df is None or not isinstance(df, _pd.DataFrame) or df.empty:
                continue

            # Locate required columns flexibly
            svc_col = next((c for c in df.columns if c == "service"), None)
            score_col = next(
                (c for c in ["score", "anomaly_score", "max_latency_spike_ms",
                              "max_error_rate_spike"] if c in df.columns),
                None,
            )
            time_col = next(
                (c for c in ["earliest_time", "earliest_anomaly_time",
                              "earliest_spike_time", "fault_start_time"] if c in df.columns),
                None,
            )
            signal_col    = next((c for c in ["dominant_signal", "signal"] if c in df.columns), None)
            metric_col    = next((c for c in ["metric", "column", "metric_name"] if c in df.columns), None)
            duration_col  = next((c for c in ["duration_s", "duration", "anomaly_duration_s"] if c in df.columns), None)

            if svc_col is None or score_col is None:
                continue

            # Aggregate by canonical service within this variable:
            #   score → SUM across all metrics per service
            #   earliest_time → MIN
            #   dominant_signal / top_metric → from the single highest-scoring row
            #   duration filter: skip rows with duration_s < 45 (< 3 timestamps × 15s)
            MIN_DURATION_S = 45
            agg: dict[str, dict] = {}
            for _, row in df.iterrows():
                svc = _canonical(str(row[svc_col]))
                try:
                    score = float(row[score_col])
                except (ValueError, TypeError):
                    continue

                # Duration filter — skip brief spikes
                if duration_col is not None:
                    try:
                        dur = float(row[duration_col])
                        if dur < MIN_DURATION_S:
                            continue
                    except (ValueError, TypeError):
                        pass

                raw_time    = str(row[time_col]) if time_col else "unknown"
                fault_start = _norm_time(raw_time) if raw_time not in ("None", "nan", "") else "unknown"
                signal      = str(row[signal_col]) if signal_col else default_signal
                metric      = str(row[metric_col]) if metric_col else f"{svc}_{default_signal}"

                if svc not in agg:
                    agg[svc] = {
                        "score":            0.0,
                        "dominant_signal":  signal,
                        "top_metric":       metric,
                        "fault_start_time": fault_start,
                        "_top_row_score":   0.0,
                        "metrics":          [],   # all (metric, score, time) for this service
                    }

                # Sum all row scores for this service
                agg[svc]["score"] += score
                agg[svc]["metrics"].append((metric, score, fault_start))

                # Keep signal + metric from the single highest-scoring row
                if score > agg[svc]["_top_row_score"]:
                    agg[svc]["_top_row_score"]  = score
                    agg[svc]["dominant_signal"] = signal
                    agg[svc]["top_metric"]      = metric

                # Keep earliest non-unknown timestamp
                cur_t = agg[svc]["fault_start_time"]
                if fault_start != "unknown" and (cur_t == "unknown" or fault_start < cur_t):
                    agg[svc]["fault_start_time"] = fault_start

            for svc, data in agg.items():
                all_entries.append({
                    "service":          svc,
                    "score":            data["score"],
                    "dominant_signal":  data["dominant_signal"],
                    "top_metric":       data["top_metric"],
                    "fault_start_time": data["fault_start_time"],
                    "metrics":          data["metrics"],
                })

        if not all_entries:
            missing = [v for v in _VAR_SIGNAL if ns.get(v) is None]
            found   = [v for v in _VAR_SIGNAL if ns.get(v) is not None]
            return (
                f"No anomaly DataFrames found in the kernel namespace.\n"
                f"Found variables: {found or 'none'}\n"
                f"Missing variables: {missing}\n\n"
                f"Each execute() anomaly-detection call must save its results to one of:\n"
                f"  anomaly_cpu, anomaly_mem, anomaly_lat, anomaly_err\n"
                f"Example: anomaly_cpu = ranked_df[['service','score','earliest_time']].copy()"
            )

        # Merge across variables: per service keep max-score variable's signal/metric,
        # accumulate all metrics, and keep the earliest fault_start_time
        svc_best: dict[str, dict] = _dd(
            lambda: {"score": 0.0, "dominant_signal": "", "top_metric": "",
                     "fault_start_time": "", "metrics": []}
        )
        for e in all_entries:
            svc = e["service"]
            if e["score"] > svc_best[svc]["score"]:
                svc_best[svc]["score"]           = e["score"]
                svc_best[svc]["dominant_signal"]  = e["dominant_signal"]
                svc_best[svc]["top_metric"]       = e["top_metric"]
            svc_best[svc]["metrics"].extend(e["metrics"])
            # Keep the earliest non-unknown timestamp
            cur_t  = svc_best[svc]["fault_start_time"]
            new_t  = e["fault_start_time"]
            if new_t != "unknown" and (cur_t == "" or cur_t == "unknown" or new_t < cur_t):
                svc_best[svc]["fault_start_time"] = new_t

        queue = [
            {"service": svc, **data}
            for svc, data in svc_best.items()
            if data["score"] > 0
        ]
        queue.sort(key=lambda x: x["score"], reverse=True)

        self._anomaly_queue       = queue
        self._anomaly_queue_index = 0
        self._anomaly_evidence    = {}

        n   = len(queue)
        top = queue[0]
        lines = [
            f"Anomaly queue ready: {n} service(s) ranked by combined evidence.",
            f"Top anomaly: {top['service']} "
            f"(score={top['score']:.2f}, metric={top['top_metric']}, "
            f"start={top['fault_start_time']})",
            "",
            f"{'Rank':<4} {'Service':<35} {'Score':>10}  {'Top metric':<40} Fault start",
        ]
        for i, e in enumerate(queue, 1):
            lines.append(
                f"  {i:<3} {e['service']:<35} {e['score']:>10.2f}  "
                f"{e['top_metric']:<40} {e['fault_start_time']}"
            )
        lines.append("")
        lines.append("Call get_next_anomaly() to begin per-service investigation.")
        return "\n".join(lines)

    def _time_to_minute(self, t: str) -> str:
        """Truncate 'YYYY-MM-DD HH:MM:SS' to 'YYYY-MM-DD HH:MM' for matching."""
        t = t.strip().replace("+00:00", "").replace(" UTC", "")
        return t[:16] if len(t) >= 16 else t

    def _find_matching_prior(self, svc, dominant, start_time, all_findings):
        """Check if a prior finding matches (service, dominant_signal, fault_start_time[:16])."""
        target_min = self._time_to_minute(start_time)
        for f in all_findings:
            if (f.get("service") == svc
                    and f.get("dominant_signal") == dominant
                    and self._time_to_minute(f.get("fault_start_time", "")) == target_min):
                return f
        return None

    # @action
    def get_next_anomaly(self) -> str:
        """Get the next anomalous service from the queue for deep investigation.

        Auto-skips services that already have matching evidence (same service,
        dominant_signal, and fault_start_time up to the minute). Skipped services
        are reported but require no action.

        Call finalize_anomaly_queue() first to build the queue.
        """
        if not self._anomaly_queue:
            return (
                "Anomaly queue is empty. "
                "Run 3–4 execute() calls for different signal types first, "
                "then call finalize_anomaly_queue()."
            )

        if self._anomaly_queue_index >= len(self._anomaly_queue):
            return (
                "Queue exhausted. All services reviewed. "
                "Call build_causal_graph() to construct the dependency graph "
                "and get the root cause recommendation."
            )

        # Flatten all recorded findings for duplicate detection
        all_findings = [
            f for step_findings in self._anomaly_evidence.values()
            for f in step_findings
        ]

        # Auto-skip loop: skip services that already have matching evidence
        skipped = []
        while self._anomaly_queue_index < len(self._anomaly_queue):
            entry = self._anomaly_queue[self._anomaly_queue_index]
            svc = entry.get("service", "unknown")
            dominant = entry.get("dominant_signal", "unknown")
            start_time = entry.get("fault_start_time", "unknown")

            match = self._find_matching_prior(svc, dominant, start_time, all_findings)
            if match:
                # Auto-skip: copy prior evidence to this step's slot
                self._anomaly_evidence[self._anomaly_queue_index] = [{
                    "service":          svc,
                    "verdict":          match.get("verdict", ""),
                    "fault_start_time": match.get("fault_start_time", ""),
                    "dominant_signal":  match.get("dominant_signal", ""),
                    "evidence":         f"(auto-copied from prior step) {match.get('evidence', '')}",
                }]
                skipped.append(
                    f"  SKIPPED {self._anomaly_queue_index + 1}/{len(self._anomaly_queue)}: "
                    f"{svc} [{match.get('verdict')}] — already classified "
                    f"(signal={dominant}, time={start_time})"
                )
                self._anomaly_queue_index += 1
            else:
                break  # found a service that needs investigation

        # Check if queue is now exhausted after skipping
        if self._anomaly_queue_index >= len(self._anomaly_queue):
            lines = ["Auto-skipped services with matching prior evidence:"]
            lines.extend(skipped)
            lines += [
                "",
                "Queue exhausted. All services reviewed.",
                "Call build_causal_graph() to construct the dependency graph "
                "and get the root cause recommendation.",
            ]
            return "\n".join(lines)

        # Pop the next un-skipped entry
        entry = self._anomaly_queue[self._anomaly_queue_index]
        self._anomaly_queue_index += 1

        svc = entry.get("service", "unknown")
        score = entry.get("score", 0)
        dominant = entry.get("dominant_signal", "unknown")
        top_metric = entry.get("top_metric", dominant)
        start_time = entry.get("fault_start_time", "unknown")
        pos = self._anomaly_queue_index
        total = len(self._anomaly_queue)
        remaining = total - pos

        lines = []
        if skipped:
            lines += ["Auto-skipped services with matching prior evidence:"]
            lines.extend(skipped)
            lines += [""]

        lines += [
            f"Service:         {svc}",
            f"Anomaly score:   {score:.2f}",
            f"Dominant signal: {dominant}",
            f"Top metric:      {top_metric}",
            f"Fault start:     {start_time} UTC",
            f"Position:        {pos} / {total}  ({remaining} remaining)",
        ]

        # Show all contributing metrics sorted by score
        all_metrics = entry.get("metrics", [])
        if all_metrics:
            top_metrics = sorted(all_metrics, key=lambda x: x[1], reverse=True)[:5]
            lines += ["", "Top contributing metrics (metric, score, earliest_time):"]
            for m_name, m_score, m_time in top_metrics:
                lines.append(f"  {m_name:<45} score={m_score:.2f}  time={m_time}")

        # Redundancy check: same (fault_start_time, dominant_signal) for a DIFFERENT service?
        for f in all_findings:
            if (f.get("service") != svc
                    and self._time_to_minute(f.get("fault_start_time", "")) == self._time_to_minute(start_time)
                    and f.get("dominant_signal") == dominant):
                lines += [
                    "",
                    f"WARNING POSSIBLY REDUNDANT: same (time≈{start_time}, "
                    f"signal={dominant}) already recorded for {f.get('service')}.",
                    "  This service may be a VICTIM propagating the same fault.",
                    "  Prioritize trace call chain analysis over resource signal analysis.",
                ]
                break

        return "\n".join(lines)

    # @action
    def record_evidence(self, findings: list) -> str:
        """Record investigation findings for the current anomaly queue step.

        Stores ALL services found during this investigation step (the current queue
        service + any callers/callees discovered in trace analysis) under the current
        queue index. Each call to record_evidence() APPENDS findings to the current
        step's list, so you can call it multiple times as you discover more services.

        Args:
            findings: List of dicts, each with:
                - service (str): e.g. "ts-auth-service"
                - verdict (str): NOISE / TEMPORARY / VICTIM / ROOT_CAUSE
                - fault_start_time (str): "YYYY-MM-DD HH:MM:SS"
                - dominant_signal (str): cpu / memory / trace_latency / trace_error_rate /
                                         disk_io / socket / log_count
                - evidence (str): Detailed description including —
                    * exact signal values (baseline avg → peak value)
                    * trace call relationships (who calls it, what it calls)
                    * timing relative to neighbors (leads or lags)
                    * why this verdict, not alternatives

        Example:
            record_evidence([
                {"service": "ts-auth-service",
                 "verdict": "ROOT_CAUSE",
                 "fault_start_time": "2024-01-22 10:08:25",
                 "dominant_signal": "cpu",
                 "evidence": "cpu spiked from 8% baseline to 94% peak at 10:08:25; "
                             "no anomalous upstream callers; ts-order-service latency "
                             "rises 15s later consistent with propagation."},
                {"service": "ts-order-service",
                 "verdict": "VICTIM",
                 "fault_start_time": "2024-01-22 10:08:40",
                 "dominant_signal": "trace_latency",
                 "evidence": "latency spike 6ms→180ms at 10:08:40, 15s after ts-auth-service "
                             "cpu fault; directly calls ts-auth-service in traces."},
            ])
        """
        VALID_VERDICTS = {"NOISE", "TEMPORARY", "VICTIM", "ROOT_CAUSE"}

        if not isinstance(findings, list) or not findings:
            return (
                "record_evidence() requires a non-empty list of dicts. "
                "Each dict must have: service, verdict, fault_start_time, "
                "dominant_signal, evidence."
            )

        # Current investigation step = the last service returned by get_next_anomaly()
        current_idx = self._anomaly_queue_index - 1
        if current_idx < 0:
            return (
                "record_evidence() called before get_next_anomaly(). "
                "Call get_next_anomaly() first to select a service to investigate."
            )

        if current_idx not in self._anomaly_evidence:
            self._anomaly_evidence[current_idx] = []

        # Flatten all previously recorded findings for redundancy checks
        all_prior = [
            f for idx, step_findings in self._anomaly_evidence.items()
            for f in step_findings
            if idx != current_idx
        ]

        results = []
        for item in findings:
            if not isinstance(item, dict):
                results.append(f"Skipped non-dict entry: {item!r}")
                continue

            service        = str(item.get("service", "")).strip()
            verdict        = str(item.get("verdict", "")).strip().upper()
            fault_start    = str(item.get("fault_start_time", "unknown")).strip()
            dominant       = str(item.get("dominant_signal", "unknown")).strip()
            evidence       = str(item.get("evidence", "")).strip()

            if not service:
                results.append("Skipped entry with missing 'service'.")
                continue
            if verdict not in VALID_VERDICTS:
                results.append(
                    f"Skipped {service}: invalid verdict '{verdict}'. "
                    f"Must be one of {sorted(VALID_VERDICTS)}."
                )
                continue

            self._anomaly_evidence[current_idx].append({
                "service":          service,
                "verdict":          verdict,
                "fault_start_time": fault_start,
                "dominant_signal":  dominant,
                "evidence":         evidence,
            })
            entry_num = len(self._anomaly_evidence[current_idx])

            # Redundancy note: same (time, signal) recorded in a different step?
            matches = list({
                f.get("service") for f in all_prior
                if f.get("fault_start_time") == fault_start
                and f.get("dominant_signal") == dominant
                and f.get("service") != service
            })

            line = (
                f"[step {current_idx + 1}, entry #{entry_num}] {service} "
                f"[{verdict}] signal={dominant} time={fault_start}"
            )
            if matches:
                line += f"  ← shares (time, signal) with {', '.join(matches)}"
            results.append(line)

        saved = [r for r in results if r.startswith("[step")]
        total = len(saved)

        return (
            f"record_evidence: {total}/{len(findings)} entries saved.\n"
            + "\n".join(f"  {r}" for r in results)
        )

    # @action
    def build_causal_graph(self) -> str:
        """Build a causal dependency graph from all recorded evidence and return root cause.

        Call after the anomaly queue is exhausted (get_next_anomaly() says "Queue exhausted").
        The Executor loads trace data, constructs a caller→callee graph among anomalous
        services, cross-references recorded verdicts, and recommends the root cause.
        """
        if self._kernel is None:
            return "Error: Executor not initialized."

        if not self._anomaly_evidence:
            return (
                "No evidence recorded yet. "
                "Use get_next_anomaly() and record_evidence() to investigate "
                "all services before calling build_causal_graph()."
            )

        import json as _json

        # Flatten all findings across all queue steps for the executor
        all_findings = [
            f for step_findings in self._anomaly_evidence.values()
            for f in step_findings
        ]
        evidence_str = _json.dumps(
            {f"step_{idx + 1}": step_findings
             for idx, step_findings in sorted(self._anomaly_evidence.items())},
            indent=2,
        )

        root_cause_candidates = list({
            f.get("service") for f in all_findings
            if f.get("verdict") == "ROOT_CAUSE"
        })

        instruction = (
            f"Using the following recorded evidence from the anomaly investigation:\n\n"
            f"{evidence_str}\n\n"
            f"Build a causal dependency graph using TWO propagation mechanisms:\n\n"
            f"STEP 1 — Trace call graph:\n"
            f"  Load trace data and identify caller→callee relationships among "
            f"all services listed in the evidence above.\n\n"
            f"STEP 2 — Colocation inference:\n"
            f"  Use colocation_clusters (from Phase 1f) if available.\n"
            f"  Otherwise, identify services with correlated CPU/memory signals "
            f"(correlation > 0.85, anomaly start within 60s) — they share a node.\n"
            f"  The highest-scoring CPU/memory service in each cluster is the local root cause.\n"
            f"  Other cluster members are colocation victims (node resource contention).\n\n"
            f"STEP 3 — Merge both graphs:\n"
            f"  ROOT_CAUSE ──(node contention)──→ colocated VICTIM services\n"
            f"  colocated VICTIM ──(trace calls)──→ downstream VICTIM services\n"
            f"  Non-colocated VICTIMs that call a colocated VICTIM → call-chain VICTIM\n\n"
            f"STEP 4 — Select ROOT_CAUSE:\n"
            f"  The service with the highest sustained CPU/memory anomaly that:\n"
            f"   - Has a direct resource signal (cpu/memory/disk/socket) AND\n"
            f"   - Best explains all victims (via colocation + trace cascading).\n\n"
            f"Print the causal chain showing BOTH propagation types, e.g.:\n"
            f"  ts-auth-service (ROOT_CAUSE, cpu) ──node contention──→ ts-basic-service (VICTIM)\n"
            f"    └──trace call──→ ts-ticketinfo (VICTIM) └──→ ts-travel-service (VICTIM)\n\n"
            f"Print the final answer in this exact format:\n"
            f"   Root cause service: <service name>\n"
            f"   Fault type: <cpu stress / memory stress / network delay / "
            f"packet loss / disk I/O stress / socket exhaustion>\n"
            f"   Fault start time: <YYYY-MM-DD HH:MM:SS UTC>\n"
            f"   Confidence: <HIGH / MEDIUM / LOW>\n"
            + (
                f"\nCurrent ROOT_CAUSE candidates from evidence: "
                f"{', '.join(root_cause_candidates)}"
                if root_cause_candidates else ""
            )
        )

        return self.execute(instruction)

    # @action
    def get_baro_ranking(self) -> str:
        """Rank services by anomaly score using BARO and populate the anomaly queue.

        Uses RobustScaler (IQR-based) on the pre-injection baseline to score
        each metric column's post-injection deviation. Services are ranked by
        their highest-scoring column.

        Automatically populates the anomaly queue so that get_next_anomaly()
        can be called immediately — no finalize_anomaly_queue() needed.

        Returns the service ranking plus dominant signal type for each.
        """
        import re as _re
        import numpy as _np
        import pandas as _pd
        from collections import defaultdict as _defaultdict

        try:
            from sklearn.preprocessing import RobustScaler as _RobustScaler
        except ImportError:
            return "Error: scikit-learn is required for get_baro_ranking()."

        namespace = self._namespace

        # 1. Read inject_time from container meta
        try:
            raw = self.static_app._docker_exec(
                f"cat /agent/telemetry/{namespace}/meta/inject_time.txt"
            ).strip()
            inject_time = float(raw)
        except Exception as exc:
            return f"Error reading inject_time: {exc}"

        # 2. Load merged metrics.csv
        df = self.static_app.fetch_metrics_df(namespace)
        if df.empty:
            return "Error: no metrics data found for namespace."

        time_col = "time" if "time" in df.columns else "timestamp"
        if time_col not in df.columns:
            return "Error: no time column in metrics."

        normal_df = df[df[time_col] < inject_time]
        anomal_df = df[df[time_col] >= inject_time]

        if len(normal_df) < 2 or len(anomal_df) < 2:
            return (
                f"Insufficient data: pre-inject={len(normal_df)} rows, "
                f"post-inject={len(anomal_df)} rows. "
                f"inject_time={inject_time}"
            )

        # 3. Helpers: extract service name and signal type from column name
        _svc_pat = _re.compile(r"ts-[a-z0-9-]+-service")

        def _extract_service(col):
            stripped = col
            if col.startswith("lat_") or col.startswith("err_"):
                stripped = col[4:]
            if stripped.endswith("_logcount"):
                stripped = stripped[:-9]
            m = _svc_pat.search(stripped)
            return m.group(0) if m else None

        def _signal_type(col):
            # Trace-level columns (from tracets_lat / tracets_err merge)
            if col.startswith("lat_"):
                return "trace_latency"
            if col.startswith("err_"):
                return "trace_error_rate"
            if "_logcount" in col:
                return "log_count"
            # Resource metrics (from simple_metrics merge)
            if col.endswith("_cpu"):
                return "cpu"
            if col.endswith("_mem"):
                return "memory"
            if col.endswith("_diskio"):
                return "disk_io"
            if col.endswith("_socket"):
                return "socket"
            if "_latency-" in col:
                return "latency"
            if col.endswith("_error"):
                return "error_rate"
            if col.endswith("_workload"):
                return "workload"
            if "_network" in col or "_net_" in col:
                return "network"
            return "metric"

        # 4. Score each column and find earliest anomaly timestamp
        skip = {time_col, "timestamp"}
        col_scores = []

        for col in df.columns:
            if col in skip:
                continue
            svc = _extract_service(col)
            if svc is None:
                continue

            pre = (
                normal_df[col]
                .replace([_np.inf, -_np.inf], _np.nan)
                .ffill().fillna(0)
                .to_numpy()
            )
            post = (
                anomal_df[col]
                .replace([_np.inf, -_np.inf], _np.nan)
                .ffill().fillna(0)
                .to_numpy()
            )

            # Skip constant pre-inject baseline (scaler would divide by 0)
            if len(set(pre)) < 2:
                continue

            try:
                scaler = _RobustScaler().fit(pre.reshape(-1, 1))
                zscores = scaler.transform(post.reshape(-1, 1))[:, 0]
                score = float(_np.max(zscores))
            except Exception:
                continue

            # Find earliest anomaly timestamp (z-score > 2.0) in post-injection
            anomaly_mask = zscores > 2.0
            if anomaly_mask.any():
                first_idx = int(_np.argmax(anomaly_mask))
                earliest_ts = float(anomal_df.iloc[first_idx][time_col])
                earliest_str = _pd.Timestamp(earliest_ts, unit="s", tz="UTC").strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            else:
                earliest_str = "unknown"

            col_scores.append((col, score, svc, _signal_type(col), earliest_str))

        if not col_scores:
            return "Could not compute BARO scores (no valid columns after filtering)."

        col_scores.sort(key=lambda x: x[1], reverse=True)

        # 5. Aggregate by service: max score + dominant signal + all metrics
        svc_data = _defaultdict(lambda: {"entries": [], "metrics": []})
        for col, score, svc, sig, earliest in col_scores:
            svc_data[svc]["entries"].append((score, sig, col))
            svc_data[svc]["metrics"].append((col, score, earliest))

        ranked_svcs = []
        for svc, data in svc_data.items():
            data["entries"].sort(reverse=True)
            top_score, top_sig, top_col = data["entries"][0]
            # Find earliest non-unknown timestamp across all metrics for this service
            times = [m[2] for m in data["metrics"] if m[2] != "unknown"]
            fault_start = min(times) if times else "unknown"
            ranked_svcs.append((svc, top_score, top_sig, top_col, fault_start, data["metrics"]))
        ranked_svcs.sort(key=lambda x: x[1], reverse=True)

        # 6. Populate anomaly queue — top K only (rest shown as reference)
        MAX_QUEUE = 10
        queue = []
        for svc, score, sig, top_col, fault_start, metrics in ranked_svcs[:MAX_QUEUE]:
            if score <= 0:
                continue
            queue.append({
                "service":          svc,
                "score":            score,
                "dominant_signal":  sig,
                "top_metric":       top_col,
                "fault_start_time": fault_start,
                "metrics":          metrics,
            })

        self._anomaly_queue       = queue
        self._anomaly_queue_index = 0
        self._anomaly_evidence    = {}

        # 7. Format output — show all ranked services, mark top K as queued
        inject_dt = _pd.Timestamp(inject_time, unit="s", tz="UTC").strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        total_positive = sum(1 for s in ranked_svcs if s[1] > 0)
        n = len(queue)
        lines = [
            f"BARO anomaly ranking  (inject_time={inject_dt} UTC)",
            f"Pre-inject rows: {len(normal_df)}   Post-inject rows: {len(anomal_df)}",
            f"Top {n} service(s) queued for investigation (out of {total_positive} with score > 0).",
            "",
            f"{'Rank':<5} {'Service':<30} {'Score':>7}  {'Signal':<20} Fault start       Status",
            f"{'-'*5} {'-'*30} {'-'*7}  {'-'*20} {'-'*19} {'-'*10}",
        ]
        for i, (svc, score, sig, top_col, fault_start, metrics) in enumerate(ranked_svcs, 1):
            if score <= 0:
                break
            status = "QUEUED" if i <= MAX_QUEUE else "ref"
            lines.append(
                f"  {i:<3} {svc:<30} {score:7.2f}  "
                f"{sig:<20} {fault_start:<19} {status}"
            )
            if i >= 15:
                remaining = total_positive - 15
                if remaining > 0:
                    lines.append(f"  ... and {remaining} more services (ref)")
                break

        lines += ["", "Top scoring columns:"]
        seen_svc = set()
        for col, score, svc, sig, earliest in col_scores[:20]:
            if svc in seen_svc:
                continue
            seen_svc.add(svc)
            lines.append(f"  {score:7.2f}  {col}  ({sig})")

        # 8. Trace latency re-ranking for top metric candidates
        trace_lat_by_svc = _defaultdict(float)
        for col, score, svc, sig, earliest in col_scores:
            if sig == "trace_latency":
                trace_lat_by_svc[svc] = max(trace_lat_by_svc[svc], score)

        top_metric_svcs = [svc for svc, _, _, _, _, _ in ranked_svcs[:MAX_QUEUE]]
        trace_reranked = [(svc, trace_lat_by_svc.get(svc, 0.0)) for svc in top_metric_svcs]
        trace_reranked.sort(key=lambda x: x[1], reverse=True)

        if trace_reranked and trace_reranked[0][1] > 0:
            lines += [
                "",
                "Trace latency re-ranking (top services re-sorted by trace anomaly score):",
            ]
            for i, (svc, tscore) in enumerate(trace_reranked, 1):
                if tscore <= 0:
                    break
                lines.append(f"  {i}. {svc:<30} trace_latency_score={tscore:.2f}")
            lines.append(
                "  Note: For CPU stress and socket exhaustion, trace latency ranking"
            )
            lines.append(
                "        is often more accurate than metric ranking for identifying root cause."
            )

        # 9. Memory-dominant victim warning
        mem_dominant_svcs = []
        for svc, data in svc_data.items():
            entries = data["entries"]
            if entries and entries[0][1] == "memory":
                mem_dominant_svcs.append((svc, entries[0][0]))
        if len(mem_dominant_svcs) >= 2:
            lines += [
                "",
                "WARNING — Multiple services show memory as dominant signal.",
                "  Memory spikes often appear in VICTIM services that receive cascading load",
                "  from the actual root cause. Consider:",
                "  - Which service's anomaly started EARLIEST?",
                "  - Does a non-memory fault (CPU, disk, socket) in one service explain",
                "    the memory spikes in others?",
                "  - Check trace latency ranking above for the true root cause.",
            ]

        lines += [
            "",
            "Focus on the QUEUED services. Investigate the top 2-3 candidates,",
            "then submit when you have enough evidence for a root cause.",
        ]
        return "\n".join(lines)

    # @hypothesis_action
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
            f"Hypothesis #{idx} saved: [{str(confidence).upper()}] "
            f"{component} | {reason} | {datetime}\n"
            f"Evidence: {evidence}"
        )

    # @hypothesis_action
    def get_hypotheses(self) -> str:
        """Retrieve all saved root cause hypotheses, ranked by confidence.

        Use this to compare all candidates before deciding which to submit.
        Returns a ranked list of hypotheses saved via save_hypothesis().
        """
        if not self._hypotheses:
            return "No hypotheses saved yet. Use save_hypothesis() to record candidates as you find them."

        order = {"high": 0, "medium": 1, "low": 2}
        ranked = sorted(self._hypotheses,
                        key=lambda h: order.get(str(h["confidence"]).lower(), 3))

        lines = [f"All hypotheses ({len(ranked)} total, ranked by confidence):\n"]
        for h in ranked:
            lines.append(
                f"  #{h['id']} [{str(h['confidence']).upper()}]  "
                f"{h['component']} | {h['reason']} | {h['datetime']}\n"
                f"    Evidence: {h['evidence']}"
            )
        return "\n".join(lines)

    # @hypothesis_trace_action
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

    # @hypothesis_executor_action
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
                        key=lambda h: order.get(str(h["confidence"]).lower(), 3))
        hyp_lines = [f"Saved hypotheses ({len(ranked)} total):"]
        for h in ranked:
            hyp_lines.append(
                f"  #{h['id']} [{str(h['confidence']).upper()}] "
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
