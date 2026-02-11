# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Logging configuration for static replayer system."""

import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional


def setup_logging(
    log_dir: Optional[Path] = None,
    level: str = "INFO",
    session_id: Optional[str] = None
) -> Path:
    """
    Setup structured logging for static replayer system.

    Args:
        log_dir: Directory for log files (None = logs/ in project root)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        session_id: Unique session ID for this experiment

    Returns:
        Path to created log file
    """

    # Create formatters
    detailed_formatter = logging.Formatter(
        fmt='%(asctime)s | %(name)-30s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    simple_formatter = logging.Formatter(
        fmt='[%(levelname)s] %(message)s'
    )

    # Console handler (simple format)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(simple_formatter)

    # File handler (detailed format)
    if log_dir is None:
        log_dir = Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_part = f"_{session_id}" if session_id else ""
    log_file = log_dir / f"static_replayer_{timestamp}{session_part}.log"

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel("DEBUG")  # Always DEBUG in file
    file_handler.setFormatter(detailed_formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel("DEBUG")
    root_logger.handlers = []  # Clear existing handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return log_file
