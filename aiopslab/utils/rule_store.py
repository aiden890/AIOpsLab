"""Rule library storage and search for the RCA knowledge base.

Storage layout:
  results/rule_library/
    rules.json              — master rule index (array, with pre-computed embeddings)
    snippets/<id>.py        — parameterized executor code snippets
    <case_id>/evidence.json — per-case evidence chain
"""

import ast
import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class RuleStore:
    """Manages persistence and search for rules and code snippets."""

    def __init__(self, library_dir: str = "results/rule_library"):
        self.library_dir = Path(library_dir)
        self.rules_path = self.library_dir / "rules.json"
        self.snippets_dir = self.library_dir / "snippets"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.snippets_dir.mkdir(parents=True, exist_ok=True)
        self._embedding_model = None
        self._embedding_available: Optional[bool] = None

    # -------------------------------------------------------------------------
    # Save
    # -------------------------------------------------------------------------

    def save_rule(self, rule: dict):
        """Append or merge a rule into rules.json.

        Deduplication: if rule_id already exists, extends source_cases,
        upgrades confidence, and merges linked_snippets.
        Computes and stores embedding before saving.
        """
        rule = dict(rule)
        rule_id = rule.get("rule_id")
        if not rule_id:
            logger.warning("Rule missing rule_id — skipping save")
            return

        rule["_embedding"] = self._compute_embedding(self._rule_text(rule))

        rules = self._load_rules()
        existing_idx = next(
            (i for i, r in enumerate(rules) if r.get("rule_id") == rule_id), None
        )

        if existing_idx is not None:
            existing = rules[existing_idx]

            # Merge source_cases (preserve order, deduplicate)
            merged_cases = list(dict.fromkeys(
                existing.get("source_cases", []) + rule.get("source_cases", [])
            ))
            rule["source_cases"] = merged_cases

            # Upgrade confidence based on number of source cases
            n = len(merged_cases)
            if n >= 4:
                rule["confidence"] = "high"
            elif n >= 2:
                rule["confidence"] = "medium"

            # Merge linked_snippets
            rule["linked_snippets"] = list(dict.fromkeys(
                existing.get("linked_snippets", []) + rule.get("linked_snippets", [])
            ))

            rules[existing_idx] = rule
            logger.info(f"Merged rule '{rule_id}' ({n} source cases, confidence={rule.get('confidence')})")
        else:
            rules.append(rule)
            logger.info(f"Saved new rule '{rule_id}'")

        self._save_rules(rules)

    def save_snippet(self, snippet_id: str, code: str, metadata: dict) -> bool:
        """Validate snippet contract and save to snippets/<snippet_id>.py.

        Contract: snippet code must define a function named `run`.

        Returns True if saved successfully, False if validation fails.
        """
        # Validate syntax
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            logger.warning(f"Snippet '{snippet_id}' has syntax error — skipping: {e}")
            return False

        # Validate: must define a `run` function
        has_run = any(
            isinstance(node, ast.FunctionDef) and node.name == "run"
            for node in ast.walk(tree)
        )
        if not has_run:
            logger.warning(f"Snippet '{snippet_id}' missing 'run(**kwargs)' function — skipping")
            return False

        # Build comment header block
        params = metadata.get("parameters", {})
        params_lines = "\n".join(f"#   {k}: {v}" for k, v in params.items())
        rct = ", ".join(metadata.get("root_cause_types", []))
        header = (
            f"# snippet_id: {snippet_id}\n"
            f"# description: {metadata.get('description', '')}\n"
            f"# when_to_use: {metadata.get('when_to_use', '')}\n"
            f"# root_cause_types: {rct}\n"
            f"# parameters:\n"
            f"{params_lines if params_lines else '#   (none)'}\n"
            f"# source_case: {metadata.get('source_case', '')}\n\n"
        )

        snippet_path = self.snippets_dir / f"{snippet_id}.py"
        snippet_path.write_text(header + code)
        logger.info(f"Saved snippet '{snippet_id}' → {snippet_path}")
        return True

    def save_evidence(self, case_id: str, evidence: dict):
        """Save evidence chain to <case_id>/evidence.json."""
        evidence_dir = self.library_dir / case_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / "evidence.json"
        with open(evidence_path, "w") as f:
            json.dump(evidence, f, indent=2)
        logger.info(f"Saved evidence for case '{case_id}' → {evidence_path}")

    # -------------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------------

    def search_keyword(self, query: str, top_k: int = 3) -> list:
        """Case-insensitive keyword match across rule text fields."""
        rules = self._load_rules()
        if not rules:
            return []

        query_terms = query.lower().split()
        scored = []
        for rule in rules:
            snippet_descs = " ".join(
                self._get_snippet_descriptions(rule.get("linked_snippets", []))
            )
            text = " ".join([
                " ".join(rule.get("abstract_signals", [])),
                rule.get("reasoning_summary", ""),
                rule.get("root_cause_type", ""),
                rule.get("investigation_strategy", ""),
                rule.get("rule_id", ""),
                snippet_descs,
            ]).lower()

            score = sum(1 for term in query_terms if term in text)
            if score > 0:
                scored.append((score, rule))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]

    def search_embedding(self, query: str, top_k: int = 3) -> list:
        """Cosine similarity search using sentence-transformers embeddings."""
        if not self._ensure_embedding_model():
            logger.warning("Embedding unavailable — falling back to keyword search")
            return self.search_keyword(query, top_k)

        rules = self._load_rules()
        if not rules:
            return []

        import numpy as np

        query_emb = self._compute_embedding(query)
        if not query_emb:
            return self.search_keyword(query, top_k)

        q = np.array(query_emb)
        scored = []
        for rule in rules:
            rule_emb = rule.get("_embedding")
            if not rule_emb:
                continue
            r = np.array(rule_emb)
            denom = np.linalg.norm(q) * np.linalg.norm(r)
            sim = float(np.dot(q, r) / denom) if denom > 0 else 0.0
            scored.append((sim, rule))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]

    def search_llm(self, query: str, top_k: int = 3, llm_fn=None) -> list:
        """LLM-based relevance ranking over all rule summaries."""
        rules = self._load_rules()
        if not rules or not llm_fn:
            return self.search_keyword(query, top_k)

        summaries = []
        for i, rule in enumerate(rules):
            summaries.append(
                f"[{i}] rule_id={rule.get('rule_id')}\n"
                f"    type={rule.get('root_cause_type')}\n"
                f"    summary={rule.get('reasoning_summary', '')[:200]}"
            )

        prompt = (
            f"Query: '{query}'\n\n"
            f"Rules:\n" + "\n".join(summaries) + "\n\n"
            f"Return the indices of the top {top_k} most relevant rules as a "
            f"comma-separated list (e.g., '0,2'). Only return indices, nothing else."
        )

        try:
            response = llm_fn(prompt)
            indices = [
                int(x.strip()) for x in response.strip().split(",")
                if x.strip().isdigit()
            ]
            return [rules[i] for i in indices[:top_k] if i < len(rules)]
        except Exception as e:
            logger.warning(f"LLM search failed: {e} — falling back to keyword")
            return self.search_keyword(query, top_k)

    def search_hybrid(self, query: str, top_k: int = 3) -> list:
        """Keyword pre-filter → embedding rerank (default search method)."""
        rules = self._load_rules()
        if not rules:
            return []

        # Keyword pre-filter: expand candidates beyond top_k
        keyword_results = self.search_keyword(query, top_k=max(top_k * 3, 10))
        candidates = keyword_results if len(keyword_results) >= top_k else rules

        if not self._ensure_embedding_model():
            return keyword_results[:top_k]

        import numpy as np

        query_emb = self._compute_embedding(query)
        if not query_emb:
            return keyword_results[:top_k]

        q = np.array(query_emb)
        scored = []
        for rule in candidates:
            rule_emb = rule.get("_embedding")
            if not rule_emb:
                continue
            r = np.array(rule_emb)
            denom = np.linalg.norm(q) * np.linalg.norm(r)
            sim = float(np.dot(q, r) / denom) if denom > 0 else 0.0
            scored.append((sim, rule))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]

    # -------------------------------------------------------------------------
    # Snippet access
    # -------------------------------------------------------------------------

    def load_snippet_code(self, snippet_id: str) -> Optional[str]:
        """Read the code block from a snippet file (skips comment header)."""
        snippet_path = self.snippets_dir / f"{snippet_id}.py"
        if not snippet_path.exists():
            return None

        lines = snippet_path.read_text().splitlines()
        code_start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or stripped == "":
                code_start = i + 1
            else:
                code_start = i
                break

        return "\n".join(lines[code_start:]).strip()

    def load_snippet_metadata(self, snippet_id: str) -> dict:
        """Parse metadata from the comment header block of a snippet file."""
        snippet_path = self.snippets_dir / f"{snippet_id}.py"
        if not snippet_path.exists():
            return {}

        meta = {}
        for line in snippet_path.read_text().splitlines():
            if not line.startswith("#"):
                break
            m = re.match(r"^#\s+(\w+):\s*(.*)", line)
            if m:
                meta[m.group(1)] = m.group(2).strip()
        return meta

    def list_snippets(self) -> list:
        """Return list of {snippet_id, description, when_to_use} for all snippets."""
        result = []
        for path in sorted(self.snippets_dir.glob("*.py")):
            snippet_id = path.stem
            meta = self.load_snippet_metadata(snippet_id)
            result.append({
                "snippet_id": snippet_id,
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
            })
        return result

    def get_available_snippet_ids(self) -> list:
        """Return list of all snippet IDs in the library."""
        return [p.stem for p in sorted(self.snippets_dir.glob("*.py"))]

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _load_rules(self) -> list:
        if not self.rules_path.exists():
            return []
        try:
            with open(self.rules_path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load rules.json: {e}")
            return []

    def _save_rules(self, rules: list):
        with open(self.rules_path, "w") as f:
            json.dump(rules, f, indent=2)

    def _rule_text(self, rule: dict) -> str:
        """Concatenate all searchable text fields for embedding."""
        return " ".join(filter(None, [
            rule.get("root_cause_type", ""),
            " ".join(rule.get("abstract_signals", [])),
            rule.get("temporal_pattern", ""),
            rule.get("investigation_strategy", ""),
            rule.get("reasoning_summary", ""),
        ]))

    def _ensure_embedding_model(self) -> bool:
        """Lazy-load sentence-transformers. Returns True if available."""
        if self._embedding_available is not None:
            return self._embedding_available
        try:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
            self._embedding_available = True
            logger.info("Embedding model loaded: all-MiniLM-L6-v2")
        except ImportError:
            self._embedding_available = False
            logger.warning(
                "sentence-transformers not installed — "
                "embedding search unavailable. Install with: pip install sentence-transformers"
            )
        return self._embedding_available

    def _compute_embedding(self, text: str) -> list:
        """Compute embedding vector. Returns empty list if unavailable."""
        if not self._ensure_embedding_model():
            return []
        try:
            return self._embedding_model.encode(text).tolist()
        except Exception as e:
            logger.warning(f"Embedding computation failed: {e}")
            return []

    def _get_snippet_descriptions(self, snippet_ids: list) -> list:
        """Collect description strings for listed snippet IDs."""
        return [
            meta["description"]
            for sid in snippet_ids
            if (meta := self.load_snippet_metadata(sid)).get("description")
        ]
