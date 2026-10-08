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
from rio.cli.login import login
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
        "--agents-md",
        type=Path,
        metavar="PATH",
        help="Project instructions file (default: AGENTS.md in the working directory).",
    )
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
        "--output",
        choices=tuple(PrintOutputMode),
        default=PrintOutputMode.human,
        help="Output format (default: human).",
    )
    run_parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    run_parser.set_defaults(handler=_run, parser=run_parser)

    login_parser = commands.add_parser(
        "login",
        help="log in to a provider",
        description="Log in to a provider and save its credentials.",
    )
    login_parser.add_argument("provider", help="Provider name.")
    login_parser.add_argument("--method", help="Authentication method.")
    login_parser.set_defaults(handler=_login, parser=login_parser)

    _add_mcp_parser(commands)

    version_parser = commands.add_parser("version", help="print the installed version")
    version_parser.set_defaults(handler=_version)
    return parser


def _add_mcp_parser(commands: argparse._SubParsersAction) -> None:
    mcp_parser = commands.add_parser(
        "mcp",
        help="manage MCP servers",
        description="Add, remove, check, and sign in to MCP servers. Servers are read from "
        "~/.rio/mcp.json and, in trusted projects, .rio/mcp.json.",
    )
    mcp_parser.set_defaults(handler=lambda args: args.parser.print_help(), parser=mcp_parser)
    mcp = mcp_parser.add_subparsers(title="commands", metavar="COMMAND")

    add = mcp.add_parser(
        "add",
        help="add or replace a server",
        usage="rio mcp add NAME [options] (--url URL | -- COMMAND [ARGS...])",
    )
    add.add_argument("name", help="Server name.")
    add.add_argument("command", nargs="*", help="Command and arguments of a stdio server.")
    add.add_argument("--url", help="Streamable HTTP server URL.")
    add.add_argument("-l", "--local", action="store_true", help="Use the project .rio/mcp.json.")
    add.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    add.add_argument("--cwd", dest="server_cwd", metavar="DIR", help="Stdio working directory.")
    add.add_argument("--header", action="append", default=[], metavar="KEY=VALUE")
    add.add_argument("--description", help="What the server offers, shown to the model.")
    add.add_argument("--oauth-client-id", help="Pre-registered OAuth client id.")
    add.add_argument("--oauth-client-secret", help="OAuth client secret; may be ${NAME}.")
    add.add_argument("--oauth-callback-port", type=int, help="Fixed OAuth callback port.")
    add.add_argument("--oauth-scope", help="OAuth scopes to request.")
    add.set_defaults(handler=_mcp_add, parser=add)

    remove = mcp.add_parser("remove", help="remove a server")
    remove.add_argument("name", help="Server name.")
    remove.add_argument("-l", "--local", action="store_true", help="Use the project .rio/mcp.json.")
    remove.set_defaults(handler=_mcp_remove, parser=remove)

    listing = mcp.add_parser("list", help="connect to each server and show its tools")
    listing.add_argument("--json", action="store_true", help="Print JSON.")
    listing.set_defaults(handler=_mcp_list, parser=listing)

    login_parser = mcp.add_parser("login", help="sign in to an OAuth server in the browser")
    login_parser.add_argument("name", help="Server name.")
    login_parser.add_argument(
        "--timeout", type=float, default=300.0, help="Seconds to wait (default: 300)."
    )
    login_parser.set_defaults(handler=_mcp_login, parser=login_parser)

    logout = mcp.add_parser("logout", help="delete a server's stored OAuth credentials")
    logout.add_argument("name", help="Server name.")
    logout.set_defaults(handler=_mcp_logout, parser=logout)


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
    if not prompt and not args.resume:
        args.parser.error("a task or Markdown task file is required")
    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    status.report("working")
    try:
        level = normalize_thinking_level(args.thinking) if args.thinking else None
        if os.name != "posix":
            raise ValueError("Rio execution requires POSIX process supervision")
        succeeded, session_id = anyio.run(
            functools.partial(run_module.run_persistent_session, agents_md=args.agents_md),
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
        status.report("error")
        args.parser.error(str(exc))
    except KeyboardInterrupt:
        status.report("idle")
        raise
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
        print(login(args.provider, args.method))
    except ValueError as exc:
        args.parser.error(str(exc))


def _mcp(args: argparse.Namespace, run) -> None:  # noqa: ANN001
    from rio.coding.mcp.commands import McpCommandError

    try:
        print(run())
    except McpCommandError as exc:
        print(f"rio mcp: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def _mcp_add(args: argparse.Namespace) -> None:
    from rio.coding.mcp.commands import AddOptions, add

    oauth = {
        key: value
        for key, value in (
            ("clientId", args.oauth_client_id),
            ("clientSecret", args.oauth_client_secret),
            ("callbackPort", args.oauth_callback_port),
            ("scope", args.oauth_scope),
        )
        if value is not None
    }
    options = AddOptions(
        name=args.name,
        command=tuple(args.command),
        url=args.url,
        local=args.local,
        env=tuple(args.env),
        headers=tuple(args.header),
        cwd=args.server_cwd,
        description=args.description,
        oauth=oauth,
    )
    _mcp(args, lambda: add(options, Path.cwd()))


def _mcp_remove(args: argparse.Namespace) -> None:
    from rio.coding.mcp.commands import remove

    _mcp(args, lambda: remove(args.name, local=args.local, cwd=Path.cwd()))


def _mcp_list(args: argparse.Namespace) -> None:
    from rio.coding.mcp.commands import list_servers

    text, ok = list_servers(Path.cwd(), as_json=args.json)
    print(text)
    if not ok:
        raise SystemExit(1)


def _mcp_login(args: argparse.Namespace) -> None:
    from rio.coding.mcp.commands import login as mcp_login

    _mcp(args, lambda: mcp_login(args.name, Path.cwd(), timeout=args.timeout))


def _mcp_logout(args: argparse.Namespace) -> None:
    from rio.coding.mcp.commands import logout

    _mcp(args, lambda: logout(args.name, Path.cwd()))


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
