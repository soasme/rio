"""Rio's command-line application."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio

from rio.cli import run as run_module
from rio.cli.login import login
from rio.coding.rendering import PrintOutputMode
from rio.coding.thinking import normalize_thinking_level


def build_parser() -> argparse.ArgumentParser:
    """Build Rio's command-line parser."""
    parser = argparse.ArgumentParser(
        prog="rio",
        description="Run autonomous coding tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(title="commands", metavar="COMMAND")

    run_parser = commands.add_parser(
        "run", help="execute a task", description="Execute a task in a durable session."
    )
    run_parser.add_argument(
        "task", nargs="*", metavar="TASK", help="Task text or a Markdown task file."
    )
    run_parser.add_argument("--provider", help="Provider name.")
    run_parser.add_argument("-m", "--model", help="Model name.")
    run_parser.add_argument("--cwd", type=Path, help="Working directory.")
    run_parser.add_argument("-t", "--thinking", help="Reasoning effort level.")
    run_parser.add_argument(
        "-e",
        "--extension",
        type=Path,
        action="append",
        default=[],
        help="Extension path (repeatable).",
    )
    trust = run_parser.add_mutually_exclusive_group()
    trust.add_argument("-a", "--approve", action="store_true", help="Trust project resources.")
    trust.add_argument("--no-approve", action="store_true", help="Do not trust project resources.")
    run_parser.add_argument("-r", "--resume", help="Session ID to resume.")
    run_parser.add_argument(
        "--output", choices=tuple(PrintOutputMode), default=PrintOutputMode.human,
        help="Output format (default: human).",
    )
    run_parser.set_defaults(handler=_run, parser=run_parser)

    login_parser = commands.add_parser(
        "login",
        help="log in to a provider",
        description="Log in to a provider and save its credentials.",
    )
    login_parser.add_argument("provider", help="Provider name.")
    login_parser.add_argument("--method", help="Authentication method.")
    login_parser.set_defaults(handler=_login, parser=login_parser)
    return parser


def app(argv: Sequence[str] | None = None) -> None:
    """Run the command-line application."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return
    args.handler(args)


def _run(args: argparse.Namespace) -> None:
    prompt = _resolve_task(args.task)
    if not prompt:
        args.parser.error("a task or Markdown task file is required")
    try:
        level = normalize_thinking_level(args.thinking) if args.thinking else None
        succeeded, session_id = anyio.run(
            run_module.run_persistent_session,
            prompt,
            args.cwd,
            args.provider,
            args.model,
            level,
            tuple(args.extension),
            "approve" if args.approve else "decline" if args.no_approve else None,
            args.resume,
            PrintOutputMode(args.output),
        )
    except ValueError as exc:
        args.parser.error(str(exc))
    if args.output == PrintOutputMode.human:
        print(f"\nSession: {session_id}")
    if not succeeded:
        raise SystemExit(1)


def _login(args: argparse.Namespace) -> None:
    try:
        print(login(args.provider, args.method))
    except ValueError as exc:
        args.parser.error(str(exc))


def _resolve_task(parts: Sequence[str]) -> str:
    """Join task arguments, expanding a sole Markdown task file."""
    task = " ".join(parts).strip()
    path = Path(task).expanduser()
    if path.is_file() and path.suffix.lower() == ".md":
        return path.read_text(encoding="utf-8")
    return task


def main() -> None:
    """Console-script entry point."""
    app(sys.argv[1:])


__all__ = ["app", "build_parser", "main"]
