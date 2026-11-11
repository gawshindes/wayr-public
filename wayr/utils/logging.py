"""Logging utilities for WAYR platform."""

import logging
from typing import Optional
from rich.logging import RichHandler
from rich.console import Console


def setup_logging(level: str = "INFO") -> None:
    """
    Set up logging with Rich handler for better console output.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Remove any existing handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Configure rich handler
    console = Console(stderr=True)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=True,
        markup=True,
        rich_tracebacks=True,
    )

    # Set format
    FORMAT = "%(message)s"
    rich_handler.setFormatter(logging.Formatter(FORMAT))

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=FORMAT,
        handlers=[rich_handler],
    )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Get a logger instance.

    Args:
        name: Logger name (typically __name__ of the module)

    Returns:
        Logger instance
    """
    return logging.getLogger(name or __name__)
