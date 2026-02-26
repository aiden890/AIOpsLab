"""Verifier Agent for backward RCA — given ground truth, finds evidence and extracts rules.

Three-phase architecture:
  Phase 1 — Evidence: Controller + Executor loop finds telemetry signals proving ground truth.
  Phase 2 — Rule:     LLM call extracts an abstract, reusable rule from the evidence.
  Phase 3 — Snippet:  LLM call generalizes the most critical executor code into a
                       parameterized function.

Not integrated with the orchestrator loop — runs its own loop directly.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from IPython.terminal.embed import InteractiveShellEmbed

from clients.openrca_rca.api_router import load_config, get_chat_completion
from clients.openrca_rca.executor import execute_act
from clients.openrca_rca.telemetry_helper import TelemetryHelper
from clients.openrca_rca.prompts import get_basic_prompt
from clients.openrca_rca.prompts.verifier_prompt import (
    PHASE1_SYSTEM,
    PHASE1_RESPONSE_FORMAT,
    PHASE2_INJECTION,
    PHASE3_INJECTION,
    parse_phase2_rule,
    parse_phase3_snippet,
)

logger = logging.getLogger("verifier_agent")


class VerifierAgent:
    """Backward RCA agent: given ground truth, produces evidence chains, rules, and snippets."""

    def __init__(self, api_config_path=None):
        if api_config_path is None:
            api_config_path = str(Path(__file__).parent / "api_config.yaml")
        self.configs = load_config(api_config_path)

        # Results populated during run()
        self.evidence_chain: list = []
        self.rule: dict | None = None
        self.snippet_code: str | None = None
        self.snippet_metadata: dict = {}
        self.snippet_id: str | None = None
        self._executor_trajectory: list = []
        self._step = 0
        self._controller_history: list = []
        self._phase2_history: list = []

    def run(
        self,
        problem,
        ground_truth: str,
        dataset_key: str,
        max_steps: int = 15,
        condition: str = "all",
    ):
        """Run all three phases for a single problem.

        Args:
            problem:      StaticProblem instance (has _actions, namespace, get_task_description()).
            ground_truth: Ground truth string from dataset labels (scoring_points).
            dataset_key:  Dataset identifier for basic prompt (e.g., "openrca_bank").
            max_steps:    Maximum evidence-collection steps (phase 1).
            condition:    Telemetry condition for basic prompt.
        """
        actions_obj = problem._actions
        namespace = problem.namespace
        task_description = problem.get_task_description()
        basic_prompt = get_basic_prompt(dataset_key)

        # Initialize IPython kernel
        kernel = InteractiveShellEmbed()
        helper = TelemetryHelper(actions_obj, namespace)
        kernel.push({"telemetry": helper})
        kernel.run_cell(
            "import pandas as pd\n"
            "pd.set_option('display.width', 427)\n"
            "pd.set_option('display.max_columns', 10)\n"
        )

        # Inject executor and direct runner into actions
        executor_history = []

        def _run_executor(instruction):
            code, result, status, new_history = execute_act(
                instruction,
                basic_prompt.build_schema(condition),
                executor_history,
                kernel,
                self.configs,
                logger,
            )
            executor_history[:] = new_history
            self._step += 1
            self._executor_trajectory.append({
                "step": self._step,
                "instruction": instruction,
                "code": code,
                "result": result,
                "success": status,
            })
            return result

        def _direct_run(code: str) -> str:
            result = kernel.run_cell(code)
            if result.success:
                return str(result.result or "").strip()
            err = result.error_in_exec
            return f"Error: {type(err).__name__}: {err}" if err else "Error: execution failed"

        actions_obj.set_executor(_run_executor)
        if hasattr(actions_obj, "set_direct_runner"):
            actions_obj.set_direct_runner(_direct_run)

        # ------------------------------------------------------------------
        # Phase 1: Evidence collection
        # ------------------------------------------------------------------
        logger.info("Phase 1: Evidence collection")
        self._phase1_loop(
            task_description=task_description,
            ground_truth=ground_truth,
            basic_prompt=basic_prompt,
            condition=condition,
            actions_obj=actions_obj,
            max_steps=max_steps,
        )

        # ------------------------------------------------------------------
        # Phase 2: Rule extraction
        # ------------------------------------------------------------------
        logger.info("Phase 2: Rule extraction")
        self._phase2_extract()

        # ------------------------------------------------------------------
        # Phase 3: Snippet generalization
        # ------------------------------------------------------------------
        logger.info("Phase 3: Snippet generalization")
        self._phase3_extract()

        # Cleanup kernel
        kernel.reset()

    def save_results(self, rule_store, case_id: str, dataset: str, task_type: str, ground_truth: str):
        """Persist rule, snippet, and evidence to the rule library.

        Args:
            rule_store:   RuleStore instance.
            case_id:      Problem ID string (e.g., "openrca_bank-task_6-0").
            dataset:      Dataset name (e.g., "openrca_bank").
            task_type:    Task type (e.g., "task_6").
            ground_truth: Ground truth string.

        Returns:
            dict with rule_id and snippet_id (may be None if extraction failed).
        """
        saved_snippet_id = None

        # Save snippet
        if self.snippet_code and self.snippet_metadata:
            snippet_id = self.snippet_metadata.get("snippet_id") or f"{case_id}_snippet_v1"
            # Ensure snippet_id doesn't include bad chars
            snippet_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", snippet_id)
            self.snippet_id = snippet_id
            meta = dict(self.snippet_metadata)
            meta["source_case"] = case_id
            ok = rule_store.save_snippet(snippet_id, self.snippet_code, meta)
            if ok:
                saved_snippet_id = snippet_id
                logger.info(f"Snippet saved: {snippet_id}")
            else:
                logger.warning(f"Snippet validation failed for case {case_id}")

        # Save rule
        saved_rule_id = None
        if self.rule:
            rule = dict(self.rule)
            rule.setdefault("source_cases", [case_id])
            rule.setdefault("applicable_datasets", [dataset])
            rule.setdefault("confidence", "low")
            if saved_snippet_id:
                existing = rule.get("linked_snippets", [])
                if saved_snippet_id not in existing:
                    rule["linked_snippets"] = existing + [saved_snippet_id]
            rule_store.save_rule(rule)
            saved_rule_id = rule.get("rule_id")
            logger.info(f"Rule saved: {saved_rule_id}")
        else:
            logger.warning(f"No rule extracted for case {case_id}")

        # Save evidence
        evidence = {
            "case_id": case_id,
            "dataset": dataset,
            "task_type": task_type,
            "ground_truth": ground_truth,
            "evidence_chain": self.evidence_chain,
            "rule_id": saved_rule_id,
            "snippet_id": saved_snippet_id,
            "timestamp": datetime.now().isoformat(),
        }
        rule_store.save_evidence(case_id, evidence)

        return {"rule_id": saved_rule_id, "snippet_id": saved_snippet_id}

    def get_phases_completed(self) -> int:
        """Return number of phases successfully completed (0-3)."""
        if self.snippet_code:
            return 3
        if self.rule:
            return 2
        if self.evidence_chain:
            return 1
        return 0

    # -------------------------------------------------------------------------
    # Phase implementations
    # -------------------------------------------------------------------------

    def _phase1_loop(
        self,
        task_description: str,
        ground_truth: str,
        basic_prompt,
        condition: str,
        actions_obj,
        max_steps: int,
    ):
        """Run evidence collection loop. Populates self.evidence_chain and
        self._controller_history for use in phases 2 and 3."""
        system_content = PHASE1_SYSTEM.format(
            ground_truth=ground_truth,
            task_description=task_description,
            response_format=PHASE1_RESPONSE_FORMAT,
        )

        self._controller_history = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "Let's begin evidence collection."},
        ]

        step = 0
        last_result = ""

        while step < max_steps:
            step += 1

            # Add executor result to history (skip on first step)
            if step > 1 and last_result:
                self._controller_history.append({"role": "user", "content": last_result})

            # Controller LLM call
            try:
                response_raw = get_chat_completion(self._controller_history, self.configs)
            except Exception as e:
                logger.error(f"Phase 1 controller error at step {step}: {e}")
                break

            if not response_raw:
                logger.warning(f"Phase 1 step {step}: empty LLM response")
                continue

            # Strip ```json wrapper if present
            if "```json" in response_raw:
                m = re.search(r"```json\s*(.*?)\s*```", response_raw, re.DOTALL)
                if m:
                    response_raw = m.group(1).strip()

            logger.debug(f"Phase 1 step {step} controller: {response_raw[:200]}")

            try:
                response = json.loads(response_raw)
            except json.JSONDecodeError:
                logger.warning(f"Phase 1 step {step}: invalid JSON response, continuing")
                self._controller_history.append({"role": "assistant", "content": response_raw})
                continue

            self._controller_history.append({"role": "assistant", "content": response_raw})

            analysis = response.get("analysis", "")
            instruction = response.get("instruction", "")
            completed = response.get("completed", "False")

            logger.info(f"Phase 1 step {step} | completed={completed} | instruction={instruction[:80]}")

            if completed == "True" or step >= max_steps:
                # Record final evidence entry
                if analysis:
                    self.evidence_chain.append({
                        "step": step,
                        "action": "conclusion",
                        "finding": analysis,
                        "signal_type": "summary",
                        "relevance": "confirms ground truth",
                    })
                logger.info(f"Phase 1 complete after {step} steps")
                break

            # Execute instruction via executor
            if instruction:
                last_result = actions_obj._executor_fn(instruction)

                # Build evidence entry from executor trajectory
                if self._executor_trajectory:
                    traj = self._executor_trajectory[-1]
                    self.evidence_chain.append({
                        "step": step,
                        "action": "execute",
                        "instruction": instruction,
                        "code": traj.get("code", ""),
                        "finding": traj.get("result", ""),
                        "signal_type": "telemetry_analysis",
                        "relevance": analysis,
                    })

    def _phase2_extract(self):
        """Inject phase 2 prompt and parse the rule from LLM response."""
        history = list(self._controller_history)
        history.append({"role": "user", "content": PHASE2_INJECTION})

        try:
            response = get_chat_completion(history, self.configs)
        except Exception as e:
            logger.error(f"Phase 2 LLM call failed: {e}")
            return

        response = response or ""
        logger.debug(f"Phase 2 response: {response[:500]}")
        self.rule = parse_phase2_rule(response)
        if self.rule:
            logger.info(f"Phase 2: extracted rule '{self.rule.get('rule_id')}'")
        else:
            logger.warning("Phase 2: failed to parse rule from response")

        # Keep history for phase 3
        history.append({"role": "assistant", "content": response})
        self._phase2_history = history

    def _phase3_extract(self):
        """Inject phase 3 prompt and parse the snippet from LLM response."""
        history = list(self._phase2_history)
        history.append({"role": "user", "content": PHASE3_INJECTION})

        try:
            response = get_chat_completion(history, self.configs)
        except Exception as e:
            logger.error(f"Phase 3 LLM call failed: {e}")
            return

        response = response or ""
        logger.debug(f"Phase 3 response: {response[:500]}")
        code, metadata = parse_phase3_snippet(response)

        if code:
            self.snippet_code = code
            self.snippet_metadata = metadata
            logger.info(f"Phase 3: extracted snippet '{metadata.get('snippet_id', '?')}'")
        else:
            logger.warning("Phase 3: no Python code block found in response")
