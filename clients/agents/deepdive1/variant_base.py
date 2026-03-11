"""Shared base for DeepDive prompt variants."""

from clients.agents.deepdive1.agent import (
    DeepDiveAgent1,
    _DIAGNOSIS_RULES,
    _format_possible_rca,
)


class DeepDivePromptVariantAgent(DeepDiveAgent1):
    """DeepDive agent that swaps prompt templates while keeping core logic."""

    EXPLORATION_PROMPT = ""
    DEEPDIVE_PROMPT_TEMPLATE = ""
    EXPAND_PROMPT_TEMPLATE = ""
    SYSTEM_TEMPLATE = ""

    def _build_messages(self) -> list[dict]:
        stage_prompt = self._get_stage_prompt()
        system_template = getattr(self, "system_template", self.SYSTEM_TEMPLATE)
        system_content = system_template.format(
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

        messages.extend(self.working_memory)
        return messages

    def _get_stage_prompt(self) -> str:
        exploration_prompt = getattr(self, "exploration_prompt", self.EXPLORATION_PROMPT)
        deepdive_prompt_template = getattr(self, "deepdive_prompt_template", self.DEEPDIVE_PROMPT_TEMPLATE)
        expand_prompt_template = getattr(self, "expand_prompt_template", self.EXPAND_PROMPT_TEMPLATE)

        if self.current_stage == self.EXPLORATION:
            return exploration_prompt

        if self.current_stage == self.DEEP_DIVE and self.current_node_id:
            node = self.tree.nodes[self.current_node_id]
            reasons_str = ", ".join(self._reasons_list) if self._reasons_list else "(any)"
            return deepdive_prompt_template.format(
                node_id=node.node_id,
                component=node.component,
                time=node.time,
                reasons=reasons_str,
            )

        if self.current_stage == self.EXPAND and self.current_node_id:
            node = self.tree.nodes[self.current_node_id]
            return expand_prompt_template.format(
                node_id=node.node_id,
                component=node.component,
                time=node.time,
            )

        return ""

    def get_system_prompt(self) -> str:
        stage_prompt = self._get_stage_prompt()
        system_template = getattr(self, "system_template", self.SYSTEM_TEMPLATE)
        return system_template.format(
            problem_desc=self.problem_desc,
            diagnosis_rules=_DIAGNOSIS_RULES,
            dataset_notes=self._dataset_notes,
            action_list=self.action_content,
            possible_root_causes=_format_possible_rca(self._possible_rca),
            system_understanding=self.system_understanding,
            tree=self.tree.render(),
            stage_prompt=stage_prompt,
        )
