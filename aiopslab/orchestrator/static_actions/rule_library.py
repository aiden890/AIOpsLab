"""RuleLibraryAction mixin — adds query_rule_library() and run_snippet() actions.

Mixed into StaticRCAActions so forward-RCA agents can consult the rule library
and run saved parameterized code snippets during investigation.
"""

import logging
import traceback

from aiopslab.utils.actions import action
from aiopslab.utils.rule_store import RuleStore

logger = logging.getLogger(__name__)


class RuleLibraryAction:
    """Mixin that adds rule library consultation actions to StaticRCAActions."""

    def __init__(self, rule_library_dir: str = "results/rule_library"):
        self._rule_store = RuleStore(rule_library_dir)
        self._rule_library_dir = rule_library_dir
        self._direct_runner = None  # set by agent via set_direct_runner()

    def set_direct_runner(self, fn):
        """Inject a direct kernel runner: fn(code: str) -> str.

        Unlike set_executor (which goes through LLM code generation),
        this runs pre-written code directly in the IPython kernel.
        Called by the agent after initializing the kernel.
        """
        self._direct_runner = fn

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    @action
    def query_rule_library(
        self,
        query: str,
        method: str = "hybrid",
        top_k: int = 3,
    ) -> str:
        """Search historical RCA rules for patterns similar to your current investigation.

        Call this early in your investigation when you have a hypothesis about
        the fault type or affected component. Returns matching rules with
        investigation strategies and available code snippets from past verified cases.

        Args:
            query:  What you are investigating. Natural language description, e.g.:
                    "high memory usage causing latency in docker service"
                    "network packet loss between containers"
                    "database connection timeouts"
            method: Search method — "keyword" (fast, literal match),
                    "embedding" (semantic similarity), "llm" (LLM ranking),
                    or "hybrid" (keyword pre-filter + embedding rerank, default).
            top_k:  Number of rules to return (default: 3).

        Returns:
            Formatted string with matching rules, investigation strategies,
            and linked snippet IDs ready to call with run_snippet().
        """
        rules = self._run_search(query, method, top_k)

        if not rules:
            snippets = self._rule_store.list_snippets()
            if snippets:
                snippet_list = ", ".join(s["snippet_id"] for s in snippets[:5])
                return (
                    f"No rules matched '{query}' (method={method}).\n"
                    f"Available snippets you can still run: {snippet_list}"
                )
            return (
                f"No rules found for '{query}'. "
                f"The rule library may be empty — run the verifier first."
            )

        lines = [f"Found {len(rules)} relevant rule(s) for '{query}':\n"]
        for i, rule in enumerate(rules, 1):
            lines.append(f"[{i}] {rule.get('rule_id', 'unnamed')} "
                         f"(confidence: {rule.get('confidence', '?')}, "
                         f"{len(rule.get('source_cases', []))} source case(s))")
            lines.append(f"    Root cause type: {rule.get('root_cause_type', '?')}")

            signals = rule.get("abstract_signals", [])
            if signals:
                lines.append(f"    Signals: {', '.join(signals)}")

            pattern = rule.get("temporal_pattern")
            if pattern:
                lines.append(f"    Temporal pattern: {pattern}")

            strategy = rule.get("investigation_strategy", "")
            if strategy:
                lines.append(f"    Investigation strategy:")
                for step in strategy.split(". "):
                    step = step.strip().rstrip(".")
                    if step:
                        lines.append(f"      - {step}.")

            snippets = rule.get("linked_snippets", [])
            if snippets:
                lines.append(f"    Available snippets: {', '.join(snippets)}")
                for sid in snippets:
                    meta = self._rule_store.load_snippet_metadata(sid)
                    if meta.get("description"):
                        lines.append(f"      {sid}: {meta['description']}")
                        params = {k: v for k, v in meta.items()
                                  if k not in ("snippet_id", "description", "when_to_use",
                                               "root_cause_types", "source_case")}
                        if params:
                            param_str = ", ".join(params.keys())
                            lines.append(f"        Parameters: {param_str}")

            sources = rule.get("source_cases", [])
            if sources:
                lines.append(f"    Source cases: {', '.join(sources[:3])}"
                             + (" ..." if len(sources) > 3 else ""))
            lines.append("")

        return "\n".join(lines)

    @action
    def run_snippet(self, snippet_id: str, **params) -> str:
        """Run a saved parameterized code snippet on the current telemetry data.

        Call this after query_rule_library() returns snippet IDs.
        The snippet runs in the current IPython kernel with access to telemetry data.

        Args:
            snippet_id: The snippet ID returned by query_rule_library()
                        (e.g., "memory_peak_finder_v1").
            **params:   Keyword arguments the snippet declares.
                        Check the snippet's "Parameters" in query_rule_library() output.

        Example:
            run_snippet("memory_peak_finder_v1",
                        namespace="static-bank",
                        metric_name="Memory_utilization",
                        start_time=1740000000,
                        end_time=1740003600)

        Returns:
            The snippet's output string, or an error message if execution fails.
        """
        if self._direct_runner is None:
            return (
                "Error: direct kernel runner not initialized. "
                "Ensure set_direct_runner() was called during agent setup."
            )

        code = self._rule_store.load_snippet_code(snippet_id)
        if code is None:
            available = self._rule_store.get_available_snippet_ids()
            if available:
                return (
                    f"Snippet '{snippet_id}' not found.\n"
                    f"Available snippets: {', '.join(available)}"
                )
            return (
                f"Snippet '{snippet_id}' not found and no snippets exist in the library. "
                f"Run the verifier first to build the library."
            )

        # Build execution: define the function, then call it
        params_repr = repr(params)
        exec_code = f"{code}\n\n_snippet_result = run(**{params_repr})\n_snippet_result"

        try:
            result = self._direct_runner(exec_code)
            return str(result) if result is not None else "(snippet returned no output)"
        except TypeError as e:
            meta = self._rule_store.load_snippet_metadata(snippet_id)
            param_info = {k: v for k, v in meta.items()
                          if k not in ("snippet_id", "description", "when_to_use",
                                       "root_cause_types", "source_case")}
            return (
                f"Snippet '{snippet_id}' parameter error: {e}\n"
                f"Expected parameters: {list(param_info.keys()) if param_info else 'see snippet file'}"
            )
        except Exception as e:
            return (
                f"Snippet '{snippet_id}' execution error:\n"
                f"{traceback.format_exc()}"
            )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _run_search(self, query: str, method: str, top_k: int) -> list:
        """Dispatch to the appropriate search backend."""
        store = self._rule_store
        if method == "keyword":
            return store.search_keyword(query, top_k)
        elif method == "embedding":
            return store.search_embedding(query, top_k)
        elif method == "llm":
            llm_fn = getattr(self, "_executor_fn", None)
            return store.search_llm(query, top_k, llm_fn=llm_fn)
        else:  # "hybrid" or anything else
            return store.search_hybrid(query, top_k)
