from __future__ import annotations

from pathlib import Path

from clients.tree_traversal.dataset_profile import build_profile

from .base import DatasetPlugin


class TelecomPlugin(DatasetPlugin):
    def dataset_name(self) -> str:
        return self.dataset

    @classmethod
    def create(cls, dataset: str) -> "TelecomPlugin":
        config = Path("aiopslab/service/apps/static_dataset/config/openrca_telecom.json")
        profile = build_profile(dataset, config_path=str(config) if config.exists() else None)
        return cls(dataset, profile)

