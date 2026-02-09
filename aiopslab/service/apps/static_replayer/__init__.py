# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Static Telemetry Replayer

A system for replaying static datasets (OpenRCA, Alibaba, ACME) as real-time telemetry.
Supports query-based time remapping, history bulk loading, and selective telemetry replay.
"""

from .replayer import StaticReplayer

__all__ = ['StaticReplayer']
