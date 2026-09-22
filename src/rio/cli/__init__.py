"""Rio's command-line application."""

from __future__ import annotations

import typer

from rio.cli.login import login
from rio.cli.run import run

app = typer.Typer(name="rio", help="Rio command-line interface.", add_completion=False)
app.command()(run)
app.command()(login)

__all__ = ["app"]
