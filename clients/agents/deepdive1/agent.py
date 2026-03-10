"""Deep Dive Agent 1: 3-Stage RCA Agent with Diagnosis Tree and 3-Layer Context.

Architecture:
  - Stages: EXPLORATION → DEEP_DIVE ↔ EXPAND → SUBMIT
  - Localization candidates are pre-analyzed and fixed (3 candidates per task)
  - 3-Layer context: Persistent / Stage Summary / Working Memory
  - DiagnosisTree tracks investigation state as indented text tree

Response format per stage:
  EXPLORATION:
    {"thought": "...", "action": "...", "args": {...}, "stage_complete": true|false}

  DEEP_DIVE:
    {"thought": "...", "action": "...", "args": {...},
     "verdict": null | {"status": "CONFIRMED|REJECTED", "reason": "...",
                        "confidence": "high|medium|low", "evidence": "..."}}

  EXPAND:
    {"thought": "...", "action": "...", "args": {...},
     "expand_result": null | {"found": true|false, "component": "...", "time": "..."}}
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken

from clients.openrca_rca.api_router import load_config, get_chat_completion
_DIAGNOSIS_RULES = """\
1. Do NOT include any Python code in your response. Provide instructions in natural language for the Executor.
2. Do NOT convert timestamps manually — let the Executor handle conversions.
3. Do NOT visualize data or draw graphs. Only text-based results are supported.
4. Do NOT save anything to the file system. Cache intermediate results in the IPython Kernel.
5. Do NOT query a specific KPI without first checking which KPIs are available."""
from clients.agents.deepdive1.prompts import (
    EXPLORATION_PROMPT,
    DEEPDIVE_PROMPT_TEMPLATE,
    EXPAND_PROMPT_TEMPLATE,
    SYSTEM_TEMPLATE,
    ACTION_LIST_TEMPLATE,
    EXECUTE_SECTION,
    FORCE_SUBMIT_TEMPLATE,
)

logger = logging.getLogger("deepdive1")

MAX_CONTEXT_FEEDBACK_CHARS = 5000
MAX_DIGEST_LINES = 80
MAX_DIGEST_CHARS = 3200


# ===========================================================================
# DiagnosisTree
# ===========================================================================

@dataclass
class DiagnosisNode:
    """Single node in the diagnosis tree."""
    node_id: str           # "1", "1.1", "1.1.1"
    component: str
    time: str
    status: str = "PENDING"   # PENDING / CONFIRMED / REJECTED / SKIPPED
    reason: str = ""
    confidence: str = ""      # high / medium / low
    evidence: str = ""
    children: list["DiagnosisNode"] = field(default_factory=list)


class DiagnosisTree:
    """Tree structure tracking candidates and their deep dive / expand results."""

    def __init__(self):
        self.roots: list[DiagnosisNode] = []      # top-level candidates [1], [2], [3]
        self.nodes: dict[str, DiagnosisNode] = {}  # node_id → node
        self.root_cause_id: str | None = None      # confirmed ROOT node_id
        self.system_info: str = ""                  # dataset, window, topology

    def add_candidate(self, rank: int, component: str, time: str) -> str:
        """Add a top-level candidate. rank is 1-based. Returns node_id."""
        node_id = str(rank)
        node = DiagnosisNode(node_id=node_id, component=component, time=time)
        self.roots.append(node)
        self.nodes[node_id] = node
        return node_id

    def add_child(self, parent_id: str, component: str, time: str) -> str:
        """Add expand child under parent. Returns new node_id like '1.1'."""
        parent = self.nodes[parent_id]
        child_idx = len(parent.children) + 1
        child_id = f"{parent_id}.{child_idx}"
        child = DiagnosisNode(node_id=child_id, component=component, time=time)
        parent.children.append(child)
        self.nodes[child_id] = child
        return child_id

    def update_node(self, node_id: str, status: str,
                    reason: str = "", confidence: str = "",
                    evidence: str = ""):
        """Update a node after deep dive verdict."""
        node = self.nodes[node_id]
        node.status = status
        if reason:
            node.reason = reason
        if confidence:
            node.confidence = confidence
        if evidence:
            node.evidence = evidence

    def set_root(self, node_id: str):
        """Mark node as confirmed ROOT and SKIP remaining top-level candidates."""
        self.root_cause_id = node_id
        confirmed_root_num = node_id.split(".")[0]
        for root in self.roots:
            if root.node_id != confirmed_root_num and root.status == "PENDING":
                root.status = f"SKIPPED (root found at [{node_id}])"

    def get_next_pending(self) -> DiagnosisNode | None:
        """Get next PENDING node in DFS order (rank order for roots)."""
        for root in self.roots:
            node = self._dfs_pending(root)
            if node:
                return node
        return None

    def _dfs_pending(self, node: DiagnosisNode) -> DiagnosisNode | None:
        if node.status == "PENDING":
            return node
        for child in node.children:
            result = self._dfs_pending(child)
            if result:
                return result
        return None

    def get_best_fallback(self) -> DiagnosisNode | None:
        """If all rejected, return the node with highest confidence."""
        conf_rank = {"high": 3, "medium": 2, "low": 1, "": 0}
        best, best_score = None, -1
        for node in self.nodes.values():
            score = conf_rank.get(node.confidence, 0)
            if score > best_score:
                best, best_score = node, score
        return best

    def get_result(self) -> dict | None:
        """Return {component, time, reason} of ROOT node."""
        if self.root_cause_id and self.root_cause_id in self.nodes:
            node = self.nodes[self.root_cause_id]
            return {"component": node.component, "time": node.time, "reason": node.reason}
        fb = self.get_best_fallback()
        if fb:
            return {"component": fb.component, "time": fb.time, "reason": fb.reason}
        return None

    def render(self) -> str:
        """Render tree as indented text for LLM consumption."""
        lines = ["DIAGNOSIS TREE", "=" * 40]
        if self.system_info:
            lines.append(self.system_info)
            lines.append("")

        for root in self.roots:
            self._render_node(root, lines, indent=0)
            lines.append("")

        if self.root_cause_id and self.root_cause_id in self.nodes:
            n = self.nodes[self.root_cause_id]
            lines.append(f"RESULT: C={n.component} T={n.time} R={n.reason}")
        else:
            lines.append("RESULT: (pending)")

        return "\n".join(lines)

    def _render_node(self, node: DiagnosisNode, lines: list, indent: int):
        prefix = "    " * indent
        status_str = f"status={node.status}"
        reason_str = f"reason={node.reason}" if node.reason else "reason=-"
        conf_str = f"conf={node.confidence}" if node.confidence else "conf=-"
        lines.append(f"{prefix}[{node.node_id}] C={node.component} T={node.time} | {status_str} | {reason_str} | {conf_str}")
        if node.evidence:
            lines.append(f"{prefix}    evidence: \"{node.evidence}\"")
        for child in node.children:
            self._render_node(child, lines, indent + 1)


# ===========================================================================
# Utility functions
# ===========================================================================

def _extract_json(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        return match.group(1)
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _build_action_string(action: str, args: dict) -> str:
    if action == "submit":
        prediction = args.get("prediction", args)
        return f'```\nsubmit({json.dumps(prediction)})\n```'
    if action == "execute":
        instruction = args.get("instruction", "")
        escaped = instruction.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        return f'```\nexecute("{escaped}")\n```'
    arg_strs = []
    for v in args.values():
        if isinstance(v, str):
            arg_strs.append(f'"{v}"')
        else:
            arg_strs.append(str(v))
    return f'```\n{action}({", ".join(arg_strs)})\n```'


def _stringify_apis(apis: dict) -> str:
    return "\n\n".join(f"{k}{v}" for k, v in apis.items())


def _format_possible_rca(prc: dict | None) -> str:
    if not prc:
        return ""
    lines = ["## POSSIBLE ROOT CAUSE CANDIDATES:\n"]
    components = prc.get("components", [])
    reasons = prc.get("reasons", [])
    lines.append("Components:")
    levels = prc.get("component_levels", {})
    if levels:
        for level, comps in levels.items():
            lines.append(f"  ({level} level)")
            lines.extend(f"  - {c}" for c in comps)
    else:
        lines.extend(f"  - {c}" for c in components)
    lines.append("\nReasons:")
    lines.extend(f"  - {r}" for r in reasons)
    return "\n".join(lines) + "\n\n"


# ===========================================================================
# StagedRCAAgent
# ===========================================================================

class DeepDiveAgent1:
    """Deep Dive Agent 1: 3-stage RCA with DiagnosisTree and 3-layer context."""

    # Stage constants
    EXPLORATION = "EXPLORATION"
    DEEP_DIVE = "DEEP_DIVE"
    EXPAND = "EXPAND"
    SUBMIT = "SUBMIT"

    def __init__(self, api_config_path: str | None = None):
        if api_config_path is None:
            api_config_path = str(
                Path(__file__).parent.parent.parent / "openrca_rca" / "api_config.yaml"
            )
        self.configs = load_config(api_config_path)

        # 3-Layer context
        self.system_understanding: str = "(not yet explored)"
        self.completed_summaries: list[str] = []  # Layer 2
        self.working_memory: list[dict] = []       # Layer 3

        # Diagnosis state
        self.tree = DiagnosisTree()
        self.current_stage: str = self.EXPLORATION
        self.current_node_id: str | None = None
        self._pending_transition: dict | None = None

        # Step tracking
        self.step: int = 0
        self.stage_step: int = 0

        # Budgets
        self.exploration_budget: int = 6
        self.deepdive_budget: int = 10
        self.expand_budget: int = 6

        # Prompt building blocks (set in init_context)
        self.problem_desc: str = ""
        self.action_content: str = ""
        self._possible_rca: dict | None = None
        self._possible_rca_cand: str = ""
        self._dataset_notes: str = ""
        self._reasons_list: list[str] = []

        # Internal trajectory for debugging/analysis
        self._agent_trajectory: list[dict] = []

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def init_context(
        self,
        problem_desc: str,
        instructions: str,
        apis: dict,
        possible_rca: dict | None = None,
        dataset_notes: str | None = None,
        candidates: list[dict] | None = None,
        prefilled_exploration: dict | None = None,
        dataset_key: str | None = None,
    ):
        """Initialize agent with problem context and pre-analyzed candidates."""
        self.problem_desc = problem_desc
        self._possible_rca = possible_rca
        self._dataset_notes = ("\n" + dataset_notes.strip() + "\n") if dataset_notes else ""

        if possible_rca:
            reasons = possible_rca.get("reasons", [])
            components = possible_rca.get("components", [])
            self._possible_rca_cand = f"Components: {components}\nReasons: {reasons}"
            self._reasons_list = reasons

        # Build action list
        execute_api = {k: v for k, v in apis.items() if k == "execute"}
        submit_api = {k: v for k, v in apis.items() if k == "submit"}
        prebuilt_apis = {k: v for k, v in apis.items()
                         if k not in ("execute", "exec_shell", "submit")}

        execute_section = (
            EXECUTE_SECTION.format(execute_api=_stringify_apis(execute_api))
            if execute_api else ""
        )
        self.action_content = ACTION_LIST_TEMPLATE.format(
            prebuilt_apis=_stringify_apis(prebuilt_apis),
            execute_section=execute_section,
            submit_api=_stringify_apis(submit_api),
        )

        # Populate tree with candidates
        if candidates:
            for i, cand in enumerate(candidates, 1):
                self.tree.add_candidate(
                    rank=i,
                    component=cand["component"],
                    time=cand.get("peak_time", cand.get("time", "")),
                )

        # Initial working memory with instructions
        self.working_memory = [
            {"role": "user", "content": instructions},
        ]

        if prefilled_exploration:
            self.bootstrap_exploration(dataset_key=dataset_key, exploration_snapshot=prefilled_exploration)

    def bootstrap_exploration(
        self,
        dataset_key: str | None,
        exploration_snapshot: dict,
    ) -> None:
        """Skip EXPLORATION stage by injecting a fixed exploration snapshot."""
        summary_payload = {
            "summary_type": "exploration_prefill",
            "stage_context": "Stage: EXPLORATION (pre-completed)",
            "dataset": dataset_key or "",
            "constraint": "Use ONLY this prefilled exploration context for topology/dependency assumptions.",
            "graph_definitions": exploration_snapshot.get("graph_definitions", {}),
            "call_graph": exploration_snapshot.get("CALL_GRAPH", {}),
            "deployment_graph": exploration_snapshot.get("DEPLOYMENT_GRAPH", {}),
            "shared_resource_graph": exploration_snapshot.get("SHARED_RESOURCE_GRAPH", {}),
        }
        summary = json.dumps(summary_payload, ensure_ascii=True, separators=(",", ":"))

        self.system_understanding = summary
        self.completed_summaries = [summary]
        self.current_stage = self.DEEP_DIVE
        self.stage_step = 0
        self._pending_transition = None

        next_node = self.tree.get_next_pending()
        if next_node:
            self.current_node_id = next_node.node_id
        else:
            self.current_node_id = None
            self.current_stage = self.SUBMIT

        self.working_memory.append({
            "role": "user",
            "content": (
                "Exploration is pre-completed. Do not run exploration tasks. "
                "Start Deep Dive immediately using only the prefilled graph context."
            ),
        })

    # ------------------------------------------------------------------
    # Main loop entry point
    # ------------------------------------------------------------------

    async def get_action(self, feedback: str) -> str:
        """Called by orchestrator each step. Returns action string."""
        self.step += 1
        self.stage_step += 1

        # 1. Process pending transition from previous step
        if self._pending_transition:
            self._execute_transition()

        # 2. Append compacted feedback to working memory (raw stays in session logs)
        resp_instr = self._get_response_instruction()
        feedback_digest = self._digest_feedback_for_context(feedback)
        self.working_memory.append({"role": "user", "content": feedback_digest})

        # 3. Check budget-based forced transition
        if self._budget_exceeded():
            self._force_stage_transition()

        # 4. Build messages and call LLM
        response = None
        max_retries = 3
        for _ in range(max_retries):
            try:
                messages = self._build_messages()
                messages.append({"role": "user", "content": resp_instr})
                response = get_chat_completion(messages, self.configs)

                if response is None:
                    logger.warning(f"Step[{self.step}] API returned None, retrying.")
                    continue

                self.working_memory.append({"role": "assistant", "content": response})

                raw_json = _extract_json(response)
                parsed = json.loads(raw_json)

                thought = parsed.get("thought", "")
                action = parsed.get("action", "")
                args = parsed.get("args", {})

                logger.debug(
                    f"Step[{self.step}] Stage={self.current_stage} "
                    f"Node={self.current_node_id} Action={action}"
                )

                # 5. Check for stage signals
                self._check_stage_signals(parsed)

                # 6. Record trajectory
                self._agent_trajectory.append({
                    "step": self.step,
                    "stage": self.current_stage,
                    "stage_step": self.stage_step,
                    "node_id": self.current_node_id,
                    "feedback_digest": feedback_digest,
                    "feedback_raw_chars": len(feedback or ""),
                    "feedback_raw_preview": (feedback or "")[:1200],
                    "thought": thought,
                    "action": action,
                    "args": args,
                    "signal": self._pending_transition,
                    "tree": self.tree.render(),
                    "system_prompt": self.get_system_prompt(),
                    "working_memory": [m.copy() for m in self.working_memory],
                    "completed_summaries": list(self.completed_summaries),
                })

                # 7. Return action string with stage/node header
                action_str = _build_action_string(action, args)
                node_info = self.tree.nodes.get(self.current_node_id) if self.current_node_id else None
                if node_info and self.current_stage != "EXPLORATION":
                    header = (f"[{self.current_stage}] Node={self.current_node_id} "
                              f"C={node_info.component} "
                              f"T={node_info.time} "
                              f"(stage_step={self.stage_step})")
                else:
                    header = f"[{self.current_stage}] (stage_step={self.stage_step})"
                parts = [header]
                if thought:
                    parts.append(f"Thought: {thought}")
                parts.append(action_str)
                return "\n".join(parts)

            except json.JSONDecodeError:
                logger.warning(f"Step[{self.step}] JSON parse failed, retrying.")
                self.working_memory.append({
                    "role": "user",
                    "content": "Your response was not valid JSON. Please respond with only a JSON object.",
                })
                continue

            except Exception as e:
                logger.error(f"Step[{self.step}] Error: {e}")
                self.working_memory.append({
                    "role": "user",
                    "content": f"Error: {e}. Please try again.",
                })
                continue

        if response is None:
            logger.warning(f"Step[{self.step}] API returned None after all retries.")
            return ""

        return self._force_submit()

    def _digest_feedback_for_context(self, feedback: str) -> str:
        """Compress long env feedback into a compact digest for model context."""
        text = (feedback or "").strip()
        if not text:
            return "(empty observation)"

        if len(text) <= MAX_CONTEXT_FEEDBACK_CHARS and text.count("\n") <= MAX_DIGEST_LINES:
            return text

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "(empty observation)"

        # Keep headers and lines with high diagnostic value (time, KPI, errors, confidence signals).
        pattern = re.compile(
            r"(UTC|timestamp|time window|\d{4}-\d{2}-\d{2}|"
            r"error|fail|anomal|deviation|baseline|"
            r"p50|p95|p99|mad|iqr|latency|timeout|"
            r"cpu|memory|network|i/o|spans?|trace|log|metric|component|reason|confidence)",
            re.IGNORECASE,
        )

        kept: list[str] = []
        seen: set[str] = set()

        for line in lines[:10]:
            if line not in seen:
                kept.append(line)
                seen.add(line)

        for line in lines[10:]:
            if not pattern.search(line):
                continue
            if line in seen:
                continue
            kept.append(line)
            seen.add(line)
            if len(kept) >= MAX_DIGEST_LINES:
                break
            if len("\n".join(kept)) >= MAX_DIGEST_CHARS:
                break

        if not kept:
            kept = lines[: min(20, len(lines))]

        omitted_lines = max(0, len(lines) - len(kept))
        digest_body = "\n".join(kept)
        return (
            "[Observation Digest]\n"
            f"{digest_body}\n"
            f"[Digest Meta] original_chars={len(text)}, omitted_lines={omitted_lines}. "
            "Full raw observation is preserved in session logs/notebooks."
        )

    # ------------------------------------------------------------------
    # Stage signal detection
    # ------------------------------------------------------------------

    def _check_stage_signals(self, parsed: dict):
        """Detect stage transition signals in LLM response. Queued for next step."""
        if self.current_stage == self.EXPLORATION:
            if parsed.get("stage_complete"):
                self._pending_transition = {"type": "exploration_complete"}

        elif self.current_stage == self.DEEP_DIVE:
            verdict = parsed.get("verdict")
            if verdict and isinstance(verdict, dict) and verdict.get("status"):
                self._pending_transition = {"type": "verdict", "verdict": verdict}

        elif self.current_stage == self.EXPAND:
            expand = parsed.get("expand_result")
            if expand and isinstance(expand, dict) and expand.get("found") is not None:
                self._pending_transition = {"type": "expand", "expand_result": expand}

    # ------------------------------------------------------------------
    # Stage transitions
    # ------------------------------------------------------------------

    def _execute_transition(self):
        """Execute a queued stage transition: summarize → archive → reset → update → next."""
        transition = self._pending_transition
        self._pending_transition = None

        # 1. Summarize working memory
        summary = self._summarize_working_memory()

        # 2. Archive to Layer 2
        self.completed_summaries.append(summary)

        # 3. Reset Layer 3
        self.working_memory = []
        self.stage_step = 0

        t_type = transition["type"]

        prev_stage = self.current_stage
        transition_desc = ""

        if t_type == "exploration_complete":
            self.system_understanding = summary
            self.current_stage = self.DEEP_DIVE
            next_node = self.tree.get_next_pending()
            self.current_node_id = next_node.node_id if next_node else None
            if not next_node:
                self.current_stage = self.SUBMIT
            transition_desc = f"EXPLORATION → DEEP_DIVE [{self.current_node_id}]"
            logger.info(f"Transition: {transition_desc}")

        elif t_type == "verdict":
            verdict = transition["verdict"]
            self.tree.update_node(
                self.current_node_id,
                status=verdict["status"].upper(),
                reason=verdict.get("reason", ""),
                confidence=verdict.get("confidence", ""),
                evidence=verdict.get("evidence", ""),
            )

            if verdict["status"].upper() == "CONFIRMED":
                self.current_stage = self.EXPAND
                transition_desc = f"DEEP_DIVE [{self.current_node_id}] CONFIRMED → EXPAND"
                logger.info(f"Transition: {transition_desc}")
            else:
                next_node = self.tree.get_next_pending()
                if next_node:
                    self.current_node_id = next_node.node_id
                    self.current_stage = self.DEEP_DIVE
                    transition_desc = f"DEEP_DIVE REJECTED → DEEP_DIVE [{self.current_node_id}]"
                    logger.info(f"Transition: {transition_desc}")
                else:
                    self.current_stage = self.SUBMIT
                    transition_desc = "All candidates REJECTED → SUBMIT (fallback)"
                    logger.info(f"Transition: {transition_desc}")

        elif t_type == "expand":
            expand = transition["expand_result"]
            if expand.get("found"):
                child_id = self.tree.add_child(
                    self.current_node_id,
                    component=expand["component"],
                    time=expand["time"],
                )
                self.current_node_id = child_id
                self.current_stage = self.DEEP_DIVE
                transition_desc = f"EXPAND found → DEEP_DIVE [{child_id}]"
                logger.info(f"Transition: {transition_desc}")
            else:
                self.tree.set_root(self.current_node_id)
                self.current_stage = self.SUBMIT
                transition_desc = f"EXPAND no deeper → ROOT = [{self.current_node_id}]"
                logger.info(f"Transition: {transition_desc}")

        # Record transition in trajectory
        self._agent_trajectory.append({
            "event": "transition",
            "type": t_type,
            "description": transition_desc,
            "from_stage": prev_stage,
            "to_stage": self.current_stage,
            "node_id": self.current_node_id,
            "summary": summary,
            "tree": self.tree.render(),
        })

    def _budget_exceeded(self) -> bool:
        if self.current_stage == self.EXPLORATION:
            return self.stage_step > self.exploration_budget
        if self.current_stage == self.DEEP_DIVE:
            return self.stage_step > self.deepdive_budget
        if self.current_stage == self.EXPAND:
            return self.stage_step > self.expand_budget
        return False

    def _force_stage_transition(self):
        """Force transition when budget exceeded."""
        logger.warning(f"Budget exceeded for {self.current_stage} (step {self.stage_step}). Forcing transition.")

        summary = self._summarize_working_memory()
        self.completed_summaries.append(summary)
        self.working_memory = []
        self.stage_step = 0

        if self.current_stage == self.EXPLORATION:
            self.system_understanding = summary
            self.current_stage = self.DEEP_DIVE
            next_node = self.tree.get_next_pending()
            self.current_node_id = next_node.node_id if next_node else None
            if not next_node:
                self.current_stage = self.SUBMIT

        elif self.current_stage == self.DEEP_DIVE:
            self.tree.update_node(
                self.current_node_id,
                status="REJECTED",
                reason="",
                confidence="low",
                evidence="budget exceeded without conclusive verdict",
            )
            next_node = self.tree.get_next_pending()
            if next_node:
                self.current_node_id = next_node.node_id
            else:
                self.current_stage = self.SUBMIT

        elif self.current_stage == self.EXPAND:
            self.tree.set_root(self.current_node_id)
            self.current_stage = self.SUBMIT

    # ------------------------------------------------------------------
    # 3-Layer message building
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        """Assemble 3-layer messages for LLM call."""
        # Layer 1: Persistent context (system prompt)
        stage_prompt = self._get_stage_prompt()
        system_content = SYSTEM_TEMPLATE.format(
            problem_desc=self.problem_desc,
            diagnosis_rules=_DIAGNOSIS_RULES,
            dataset_notes=self._dataset_notes,
            action_list=self.action_content,
            possible_root_causes=_format_possible_rca(self._possible_rca),
            system_understanding=self.system_understanding,
            tree=self.tree.render(),
            stage_prompt=stage_prompt,
        )

        messages = [{"role": "system", "content": system_content}]

        # Layer 2: Completed summaries
        if self.completed_summaries:
            summaries_text = "\n\n---\n\n".join(self.completed_summaries)
            messages.append({
                "role": "user",
                "content": f"## Completed Analysis\n\n{summaries_text}",
            })
            messages.append({
                "role": "assistant",
                "content": "Understood. I'll reference these completed analyses as I continue.",
            })

        # Layer 3: Working memory (current node raw conversation)
        messages.extend(self.working_memory)

        return messages

    def _get_stage_prompt(self) -> str:
        if self.current_stage == self.EXPLORATION:
            return EXPLORATION_PROMPT

        if self.current_stage == self.DEEP_DIVE and self.current_node_id:
            node = self.tree.nodes[self.current_node_id]
            reasons_str = ", ".join(self._reasons_list) if self._reasons_list else "(any)"
            return DEEPDIVE_PROMPT_TEMPLATE.format(
                node_id=node.node_id,
                component=node.component,
                time=node.time,
                reasons=reasons_str,
            )

        if self.current_stage == self.EXPAND and self.current_node_id:
            node = self.tree.nodes[self.current_node_id]
            return EXPAND_PROMPT_TEMPLATE.format(
                node_id=node.node_id,
                component=node.component,
                time=node.time,
            )

        return ""

    def _get_response_instruction(self) -> str:
        if self.current_stage == self.EXPLORATION:
            return (
                '\n\nRespond with a JSON object only:\n'
                '{"thought": "...", "action": "...", "args": {...}, "stage_complete": false}'
            )
        if self.current_stage == self.DEEP_DIVE:
            return (
                '\n\nRespond with a JSON object only:\n'
                '{"thought": "...", "action": "...", "args": {...}, "verdict": null}'
            )
        if self.current_stage == self.EXPAND:
            return (
                '\n\nRespond with a JSON object only:\n'
                '{"thought": "...", "action": "...", "args": {...}, "expand_result": null}'
            )
        return (
            '\n\nRespond with a JSON object only:\n'
            '{"thought": "...", "action": "submit", "args": {"prediction": {...}}}'
        )

    def get_system_prompt(self) -> str:
        """Return the current system prompt (Layer 1) for external logging."""
        stage_prompt = self._get_stage_prompt()
        return SYSTEM_TEMPLATE.format(
            problem_desc=self.problem_desc,
            diagnosis_rules=_DIAGNOSIS_RULES,
            dataset_notes=self._dataset_notes,
            action_list=self.action_content,
            possible_root_causes=_format_possible_rca(self._possible_rca),
            system_understanding=self.system_understanding,
            tree=self.tree.render(),
            stage_prompt=stage_prompt,
        )

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    @staticmethod
    def _clip(value, max_len: int = 240) -> str:
        text = str(value or "").strip()
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _build_structured_summary_prompt(self, content: str, stage_context: str) -> str:
        if self.current_stage == self.EXPLORATION:
            return (
                "You are compressing RCA analysis context.\n"
                "Return ONLY a JSON object with this exact schema:\n"
                "{\n"
                '  "summary_type": "exploration",\n'
                '  "stage_context": "<copied context>",\n'
                '  "components": ["..."],\n'
                '  "topology": ["caller -> callee", "..."],\n'
                '  "kpi_baselines": ["kpi=value (component/time)", "..."],\n'
                '  "global_anomalies": ["...", "..."],\n'
                '  "open_questions": ["...", "..."]\n'
                "}\n"
                "Rules:\n"
                "- Keep each list <= 6 items.\n"
                "- Use concrete metrics/timestamps where available.\n"
                "- If unknown, return empty string/list, not prose.\n\n"
                f"Context: {stage_context}\n\n"
                f"Analysis:\n{content}"
            )

        return (
            "You are compressing RCA node analysis context.\n"
            "Return ONLY a JSON object with this exact schema:\n"
            "{\n"
            '  "summary_type": "node",\n'
            '  "stage_context": "<copied context>",\n'
            '  "node_id": "<id>",\n'
            '  "component": "<component>",\n'
            '  "candidate_time": "<UTC time>",\n'
            '  "analysis_window_utc": "<window or empty>",\n'
            '  "verdict": "CONFIRMED|REJECTED|PENDING|UNKNOWN",\n'
            '  "confidence": "high|medium|low|",\n'
            '  "evidence_table": [\n'
            '    {"claim":"...","supporting_evidence":"...","counter_evidence":"...","status":"supported|refuted|mixed"}\n'
            "  ],\n"
            '  "next_action": "<short next step or empty>"\n'
            "}\n"
            "Rules:\n"
            "- evidence_table must be explicit claim/evidence/counter-evidence rows.\n"
            "- Keep evidence_table <= 4 rows.\n"
            "- Include concrete numbers/timestamps in evidence where possible.\n"
            "- Use UTC in analysis_window_utc when available.\n\n"
            f"Context: {stage_context}\n\n"
            f"Analysis:\n{content}"
        )

    def _normalize_structured_summary(
        self,
        parsed: dict,
        stage_context: str,
    ) -> dict:
        def as_list(value, max_items: int = 6, max_len: int = 220) -> list[str]:
            if not isinstance(value, list):
                return []
            out = []
            for item in value[:max_items]:
                clipped = self._clip(item, max_len=max_len)
                if clipped:
                    out.append(clipped)
            return out

        if self.current_stage == self.EXPLORATION:
            return {
                "summary_type": "exploration",
                "stage_context": stage_context,
                "components": as_list(parsed.get("components"), max_items=8, max_len=80),
                "topology": as_list(parsed.get("topology"), max_items=8),
                "kpi_baselines": as_list(parsed.get("kpi_baselines")),
                "global_anomalies": as_list(parsed.get("global_anomalies")),
                "open_questions": as_list(parsed.get("open_questions"), max_items=4),
            }

        node_id = ""
        component = ""
        candidate_time = ""
        if self.current_node_id and self.current_node_id in self.tree.nodes:
            node = self.tree.nodes[self.current_node_id]
            node_id = node.node_id
            component = node.component
            candidate_time = node.time

        verdict = str(parsed.get("verdict", "UNKNOWN")).upper()
        if verdict not in {"CONFIRMED", "REJECTED", "PENDING", "UNKNOWN"}:
            verdict = "UNKNOWN"

        confidence = str(parsed.get("confidence", "")).lower()
        if confidence not in {"high", "medium", "low", ""}:
            confidence = ""

        rows = parsed.get("evidence_table", [])
        normalized_rows = []
        if isinstance(rows, list):
            for row in rows[:4]:
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status", "mixed")).lower()
                if status not in {"supported", "refuted", "mixed"}:
                    status = "mixed"
                normalized_rows.append({
                    "claim": self._clip(row.get("claim", ""), max_len=180),
                    "supporting_evidence": self._clip(row.get("supporting_evidence", ""), max_len=260),
                    "counter_evidence": self._clip(row.get("counter_evidence", ""), max_len=220),
                    "status": status,
                })

        return {
            "summary_type": "node",
            "stage_context": stage_context,
            "node_id": self._clip(parsed.get("node_id", node_id), max_len=20) or node_id,
            "component": self._clip(parsed.get("component", component), max_len=80) or component,
            "candidate_time": self._clip(parsed.get("candidate_time", candidate_time), max_len=32) or candidate_time,
            "analysis_window_utc": self._clip(parsed.get("analysis_window_utc", ""), max_len=80),
            "verdict": verdict,
            "confidence": confidence,
            "evidence_table": normalized_rows,
            "next_action": self._clip(parsed.get("next_action", ""), max_len=180),
        }

    def _summarize_working_memory(self) -> str:
        """LLM call to compress working memory into a fixed structured summary."""
        if not self.working_memory:
            fallback = {
                "summary_type": "fallback",
                "stage_context": f"Stage: {self.current_stage}",
                "note": "no data collected",
            }
            return json.dumps(fallback, ensure_ascii=True, separators=(",", ":"))

        content_parts = []
        for msg in self.working_memory:
            role = msg["role"]
            text = msg.get("content", "")
            if len(text) > 3000:
                text = text[:3000] + "\n... (truncated)"
            content_parts.append(f"[{role}]: {text}")
        content = "\n\n".join(content_parts)

        stage_context = f"Stage: {self.current_stage}"
        if self.current_node_id and self.current_node_id in self.tree.nodes:
            node = self.tree.nodes[self.current_node_id]
            stage_context += f" | Node [{node.node_id}] C={node.component} T={node.time}"

        summarize_messages = [
            {
                "role": "system",
                "content": self._build_structured_summary_prompt(content, stage_context),
            },
        ]

        try:
            summary = get_chat_completion(summarize_messages, self.configs)
            if summary:
                parsed = json.loads(_extract_json(summary))
                normalized = self._normalize_structured_summary(parsed, stage_context)
                return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
        except Exception as e:
            logger.error(f"Summarization failed: {e}")

        for msg in reversed(self.working_memory):
            if msg["role"] == "assistant":
                fallback = {
                    "summary_type": "fallback",
                    "stage_context": stage_context,
                    "note": "summarization failed; using assistant tail",
                    "assistant_tail": self._clip(msg["content"], max_len=500),
                }
                return json.dumps(fallback, ensure_ascii=True, separators=(",", ":"))

        fallback = {
            "summary_type": "fallback",
            "stage_context": stage_context,
            "note": "summarization failed",
        }
        return json.dumps(fallback, ensure_ascii=True, separators=(",", ":"))

    # ------------------------------------------------------------------
    # Force submit
    # ------------------------------------------------------------------

    def _force_submit(self) -> str:
        """Force submission using tree result or best fallback."""
        result = self.tree.get_result()
        if result and result.get("component") and result.get("reason"):
            prediction = {
                "1": {
                    "root cause component": result["component"],
                    "root cause reason": result["reason"],
                    "root cause occurrence time": result["time"],
                }
            }
            return _build_action_string("submit", {"prediction": prediction})

        messages = self.working_memory + [{
            "role": "user",
            "content": FORCE_SUBMIT_TEMPLATE.format(
                cand=self._possible_rca_cand,
                objective=self.problem_desc,
                tree=self.tree.render(),
            ),
        }]
        try:
            response = get_chat_completion(messages, self.configs)
            if response:
                parsed = json.loads(_extract_json(response))
                return _build_action_string(
                    parsed.get("action", "submit"),
                    parsed.get("args", {}),
                )
        except Exception:
            pass

        return '```\nsubmit({})\n```'

    # ------------------------------------------------------------------
    # Interface methods
    # ------------------------------------------------------------------

    def get_model_name(self) -> str:
        model = self.configs.get("MODEL", "unknown")
        effort = self.configs.get("REASONING_EFFORT")
        return f"{model}-{effort}" if effort else model

    def cleanup(self):
        """No-op: kernel cleanup handled by StaticRCAActionsWithExecutor."""
        pass
