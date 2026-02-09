# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Static Problems Module

Problems that use static datasets (OpenRCA, Alibaba, etc.) instead of live systems.
"""

from .openrca_detection import (
    OpenRCABankDetection,
    OpenRCATelecomDetection,
    OpenRCAMarketCloudbed1Detection,
    OpenRCAMarketCloudbed2Detection,
)

__all__ = [
    'OpenRCABankDetection',
    'OpenRCATelecomDetection',
    'OpenRCAMarketCloudbed1Detection',
    'OpenRCAMarketCloudbed2Detection',
]
