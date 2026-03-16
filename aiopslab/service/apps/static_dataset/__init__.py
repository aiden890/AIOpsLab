"""
Static Dataset Application

Manages static datasets (OpenRCA, Alibaba, etc.) as Docker-based telemetry sources.
"""

from .dataset import StaticDataset
from .re2tt_dataset import RE2TTDataset, FAULT_REASON_MAP

__all__ = ["StaticDataset", "RE2TTDataset", "FAULT_REASON_MAP"]
