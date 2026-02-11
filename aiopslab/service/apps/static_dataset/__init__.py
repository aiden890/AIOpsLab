"""
Static Dataset Application

Manages static datasets (OpenRCA, Alibaba, etc.) as Docker-based telemetry sources.
"""

from .dataset import StaticDataset

__all__ = ["StaticDataset"]
