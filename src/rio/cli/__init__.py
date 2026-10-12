"""Rio's command-line application."""

from __future__ import annotations

import argparse
import functools
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio

from rio.cli import run as run_module
from rio.cli import status
from rio.coding.auth_commands import login_provider
from rio.coding.rendering import PrintOutputMode
from rio.coding.thinking import normalize_thinking_level
from rio.version import current_version


def build_parser() -> argparse.ArgumentParser:
    """Build Rio's command-line parser."""
    parser = argparse.ArgumentParser(
        prog="rio",
        description="Run autonomous coding tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(title="commands", metavar="COMMAND", required=True)

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
        "--agents-md",
        type=Path,
        metavar="PATH",
        help="Project instructions file (default: AGENTS.md in the working directory).",
    )
    run_parser.add_argument("-r", "--resume", help="Session ID to resume.")
    run_parser.add_argument(
        "--output",
        choices=tuple(PrintOutputMode),
        default=PrintOutputMode.human,
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

    version_parser = commands.add_parser("version", help="print the installed version")
    version_parser.set_defaults(handler=_version)
    return parser


def app(argv: Sequence[str] | None = None) -> None:
    """Run the command-line application."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.handler(args)


def _run(args: argparse.Namespace) -> None:
    prompt = _resolve_task(args.task)
    if not prompt and not args.resume:
        args.parser.error("a task or Markdown task file is required")
    status.report("working")
    try:
        level = normalize_thinking_level(args.thinking) if args.thinking else None
        if os.name != "posix":
            raise ValueError("Rio execution requires POSIX process supervision")
        succeeded, session_id = anyio.run(
            functools.partial(
                run_module.run_persistent_session,
                prompt,
                cwd=args.cwd,
                provider_name=args.provider,
                model=args.model,
                thinking_level=level,
                resume=args.resume,
                output_mode=PrintOutputMode(args.output),
                agents_md=args.agents_md,
            ),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        status.report("error")
        print(f"rio: error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        status.report("idle")
        raise SystemExit(130) from None
    except BaseException:
        status.report("error")
        raise
    status.report("done" if succeeded else "error")
    if args.output == PrintOutputMode.human:
        print(f"\nSession: {session_id}")
    if not succeeded:
        raise SystemExit(1)


def _login(args: argparse.Namespace) -> None:
    try:
        print(anyio.run(functools.partial(login_provider, args.provider, method=args.method)))
    except ValueError as exc:
        args.parser.error(str(exc))


def _version(args: argparse.Namespace) -> None:
    print(current_version())


def _resolve_task(parts: Sequence[str]) -> str:
    """Join task arguments, expanding a sole Markdown task file."""
    task = " ".join(parts).strip()
    path = Path(task).expanduser()
    try:
        is_task_file = path.suffix.lower() == ".md" and path.is_file()
    except OSError:  # A long task can exceed the file name limit.
        is_task_file = False
    return path.read_text(encoding="utf-8") if is_task_file else task


def main() -> None:
    """Console-script entry point."""
    app(sys.argv[1:])


__all__ = ["app", "build_parser", "main"]
