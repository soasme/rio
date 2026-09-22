"""Compatibility exports for the former coding CLI module."""

from rio.cli import app
from rio.cli.run import run_configured_session, run_print_mode

__all__ = ["app", "run_configured_session", "run_print_mode"]
