"""`lcode mcp ...` commands, the session's startup and the status table shared with /mcp."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm
from rich.table import Table

from lcode import config
from lcode.config import format_tokens
from lcode.mcp import catalog
from lcode.mcp import config as mcp_config
from lcode.mcp.auth import OAuth
from lcode.mcp.manager import McpManager
from lcode.mcp.protocol import McpError

STATUS = {
    "ready": "[green]ready[/]",
    "starting": "[cyan]starting…[/]",
    "login": "[yellow]needs sign-in[/]",
    "failed": "[red]failed[/]",
    "disabled": "[dim]disabled[/]",
    "off": "[dim]not started[/]",
}


# ----------------------------------------------------------------------------- sessions


def start_session(cwd: Path, tool_mode: str, console: Console, interactive: bool) -> McpManager | None:
    """Start the configured servers in the background. Asks before using a project's .mcp.json."""
    project = mcp_config.project_file(cwd)
    use_project = False
    if project:
        if mcp_config.is_approved(project):
            use_project = True
        elif interactive:
            try:
                names = ", ".join(mcp_config.read(project)) or "none"
            except ValueError as e:
                console.print(f"[yellow]{escape(str(e))}[/]")
                names = ""
            if names:
                console.print(
                    f"[yellow]{escape(str(project))} adds MCP servers to this project: {escape(names)}.[/]\n"
                    "They start programs or connect to services on your behalf, so only use them if you trust "
                    "this repository."
                )
                if Confirm.ask("Use them?", default=False, console=console):
                    mcp_config.approve(project)
                    use_project = True
        else:
            console.print(f"[dim]Not using the MCP servers in {project} until you approve them in a session.[/]")
    servers, problems = mcp_config.load_all(cwd, include_project=use_project)
    for problem in problems:
        console.print(f"[yellow]MCP: {escape(problem)}[/]")
    if not servers:
        return None
    manager = McpManager(cwd, servers, tool_mode)
    manager.start()
    return manager


def status_table(manager: McpManager, context: int | None = None) -> Table:
    table = Table(header_style="bold", title="MCP servers", title_justify="left")
    table.add_column("Server", style="cyan")
    table.add_column("Status")
    table.add_column("Tools", justify="right")
    table.add_column("Details")
    for state in manager.servers.values():
        detail = state.error or state.cfg.target
        if state.cfg.source != "user":
            detail += f" [dim](from {Path(state.cfg.source).name})[/]"
        tools = str(len(state.tools)) if state.status == "ready" else ""
        table.add_row(escape(state.name), STATUS.get(state.status, state.status), tools, escape(detail))
    if context and manager.ready():
        mode = "on demand (mcp_find_tools)" if manager.searching(context) else "all sent with every request"
        table.caption = f"Tool definitions: ~{format_tokens(manager.cost())} tokens · {mode}"
    return table


# ----------------------------------------------------------------------------- lcode mcp


class _Parser(argparse.ArgumentParser):
    """Everything after `--` is the command of a local server, whatever it looks like."""

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        args = list(sys.argv[1:] if args is None else args)
        command: list[str] = []
        if "--" in args:
            split = args.index("--")
            args, command = args[:split], args[split + 1 :]
        parsed = super().parse_args(args, namespace)
        parsed.command = command
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="lcode mcp",
        description="Connect lcode to MCP servers: Jira, GitHub, AWS, databases, browsers and more.",
        epilog="Settings live in ~/.config/lcode/mcp.json (and a project's .mcp.json). "
        "Docs: https://nasser1941.github.io/lcode/mcp/",
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="configured servers and whether they work")
    sub.add_parser("catalog", help="ready-made servers you can add")
    add = sub.add_parser(
        "add",
        help="add a server from the catalog, or your own",
        description="lcode mcp add github  |  lcode mcp add NAME --url URL  |  lcode mcp add NAME -- COMMAND ARGS",
    )
    add.add_argument("name")
    add.add_argument("--url", help="a remote (Streamable HTTP) server")
    add.add_argument("--header", action="append", default=[], metavar="'NAME: VALUE'", help="HTTP header")
    add.add_argument("--env", action="append", default=[], metavar="NAME=VALUE", help="environment variable")
    add.add_argument("--no-login", action="store_true", help="don't sign in right away")
    add.add_argument("-y", "--yes", action="store_true", help="replace an existing server without asking")
    for action, text in (
        ("remove", "remove a server (and its sign-in)"),
        ("login", "sign in to a remote server in the browser"),
        ("logout", "forget a server's sign-in"),
        ("tools", "list a server's tools"),
        ("enable", "turn a server back on"),
        ("disable", "turn a server off without removing it"),
    ):
        sub.add_parser(action, help=text).add_argument("name")
    return parser


def run(args: argparse.Namespace, console: Console) -> int:
    action = args.action or "list"
    try:
        if action == "list":
            return cmd_list(console)
        if action == "catalog":
            return cmd_catalog(console)
        if action == "add":
            return cmd_add(args, console)
        return {"remove": cmd_remove, "login": cmd_login, "logout": cmd_logout, "tools": cmd_tools}.get(
            action, cmd_toggle
        )(args, console)
    except ValueError as e:  # an invalid mcp.json
        console.print(f"[red]error:[/] {escape(str(e))}")
        return 1


def _manager(names: list[str] | None = None) -> McpManager:
    cwd = Path.cwd()
    project = mcp_config.project_file(cwd)
    servers, problems = mcp_config.load_all(cwd, include_project=bool(project and mcp_config.is_approved(project)))
    if problems:
        raise ValueError("; ".join(problems))
    if names is not None:
        servers = [s for s in servers if s.name in names]
    return McpManager(cwd, servers)


def _check(manager: McpManager, console: Console, names: list[str] | None = None) -> None:
    with console.status("Connecting to the servers (the first start can take a minute)…"):
        manager.start(names)
        manager.wait()


def cmd_list(console: Console) -> int:
    manager = _manager()
    if not manager.servers:
        console.print("No MCP servers yet. See what's ready to add with [bold]lcode mcp catalog[/].")
        return 0
    _check(manager, console)
    try:
        console.print(status_table(manager, context=config.load().get("context") or 32768))
    finally:
        manager.close()
    if any(s.status == "login" for s in manager.servers.values()):
        console.print("Sign in with: lcode mcp login <server>")
    return 0 if all(s.status in ("ready", "disabled") for s in manager.servers.values()) else 1


def cmd_catalog(console: Console) -> int:
    table = Table(header_style="bold", title="Ready-made MCP servers", title_justify="left")
    table.add_column("Add with", style="cyan", no_wrap=True)
    table.add_column("What it does")
    table.add_column("Needs")
    for preset in catalog.load().values():
        needs = []
        if preset.login:
            needs.append("browser sign-in")
        needs += [f"{i.prompt.split(' (')[0].lower()}" for i in preset.inputs if not i.optional]
        missing = preset.missing()
        needs += [f"[red]{m} (not installed)[/]" if m in missing else m for m in preset.requires]
        table.add_row(
            f"lcode mcp add {preset.key}",
            f"[bold]{escape(preset.name)}[/]: {escape(preset.description)}",
            ", ".join(needs) or "nothing",
        )
    console.print(table)
    console.print("[dim]Or add any other server: lcode mcp add NAME --url URL, or lcode mcp add NAME -- COMMAND[/]")
    return 0


def _ask(spec: catalog.Input, console: Console) -> str | None:
    """The value for one input: None means "read it from the environment variable"."""
    if os.environ.get(spec.var):
        console.print(f"  {spec.prompt}: using ${spec.var} from the environment")
        return None
    suggestion = spec.suggestion()
    if suggestion:
        source = " ".join(spec.from_command)
        if Confirm.ask(f"  Use the token from `{source}`?", default=True, console=console):
            return suggestion
    default = f" \\[{escape(spec.default)}]" if spec.default else ""  # escaped, or rich takes it for markup
    while True:
        value = console.input(f"  {escape(spec.prompt)}{default}: ", password=spec.secret).strip()
        value = value or spec.default or ""
        if value or spec.optional:
            return value or None
        console.print(f"  [yellow]{spec.prompt} is needed[/] (or set ${spec.var} and run this again)")


def cmd_add(args: argparse.Namespace, console: Console) -> int:
    name = args.name
    command = args.command
    presets = catalog.load()
    preset = presets.get(name) if not (args.url or command) else None
    if preset is None and not (args.url or command):
        console.print(
            f"[red]error:[/] {escape(name)} isn't in the catalog (see lcode mcp catalog). For your own server use "
            f"--url URL or -- COMMAND ARGS."
        )
        return 1
    servers = mcp_config.load_user()
    if (
        name in servers
        and not args.yes
        and not Confirm.ask(f"Replace the existing {name}?", default=False, console=console)
    ):
        return 1
    if preset:
        console.print(f"[bold]{escape(preset.name)}[/]: {escape(preset.description)}")
        if preset.setup:
            console.print(f"[dim]{escape(preset.setup)}[/]")
        missing = preset.missing()
        if missing:
            console.print(f"[yellow]Not installed: {', '.join(missing)}.[/] The server won't start without it.")
            if not Confirm.ask("Add it anyway?", default=False, console=console):
                return 1
        answers = {spec.var: _ask(spec, console) for spec in preset.inputs}
        raw = catalog.build(preset, answers)
    else:
        raw = _custom(args, command)
    servers[name] = raw
    path = mcp_config.save_user(servers)
    console.print(f"[green]Added {escape(name)}[/] to {path}")
    return _connect_new(name, console, login=bool(preset and preset.login) and not args.no_login)


def _custom(args: argparse.Namespace, command: list[str]) -> dict:
    env = dict(item.split("=", 1) for item in args.env if "=" in item)
    if args.url:
        headers = {}
        for item in args.header:
            key, _, value = item.partition(":")
            if key.strip():
                headers[key.strip()] = value.strip()
        return {"type": "http", "url": args.url, **({"headers": headers} if headers else {})}
    return {"command": command[0], "args": command[1:], **({"env": env} if env else {})}


def _connect_new(name: str, console: Console, login: bool) -> int:
    manager = _manager([name])
    state = manager.servers[name]
    try:
        if login and state.cfg.transport == "http":
            OAuth(name, state.cfg.url, state.cfg.oauth).login(notify=lambda text: console.print(escape(text)))
        _check(manager, console)
        if state.status == "login" and Confirm.ask(
            f"{name} needs you to sign in. Sign in now?", default=True, console=console
        ):
            OAuth(name, state.cfg.url, state.cfg.oauth).login(notify=lambda text: console.print(escape(text)))
            _check(manager, console)
        if state.status == "ready":
            console.print(
                f"[green]✓[/] {escape(name)} works: {len(state.tools)} tools "
                f"(~{format_tokens(manager.cost())} tokens of definitions). It's available in new lcode sessions."
            )
            return 0
        console.print(f"[red]✗[/] {escape(name)}: {escape(state.error)}")
        if state.cfg.transport == "stdio":
            console.print(f"[dim]The server's log: {_log(name)}[/]")
        return 1
    except McpError as e:
        console.print(f"[red]✗[/] {escape(name)}: {escape(str(e))}")
        return 1
    finally:
        manager.close()


def _log(name: str) -> Path:
    from lcode.mcp.manager import _slug, logs_dir

    return logs_dir() / f"{_slug(name)}.log"


def _user_server(name: str, console: Console) -> dict | None:
    servers = mcp_config.load_user()
    if name not in servers:
        console.print(f"[red]error:[/] no server named {escape(name)} in {mcp_config.user_path()}")
        return None
    return servers


def cmd_remove(args: argparse.Namespace, console: Console) -> int:
    servers = _user_server(args.name, console)
    if servers is None:
        return 1
    raw = servers.pop(args.name)
    mcp_config.save_user(servers)
    if isinstance(raw, dict) and raw.get("url"):
        OAuth(args.name, str(raw["url"])).logout()
    console.print(f"Removed {escape(args.name)}.")
    return 0


def _remote(name: str, console: Console) -> mcp_config.ServerConfig | None:
    manager = _manager([name])
    if name not in manager.servers:
        console.print(f"[red]error:[/] no server named {escape(name)} (see lcode mcp list)")
        return None
    cfg = manager.servers[name].cfg
    if cfg.transport != "http":
        console.print(
            f"{escape(name)} runs on this machine and uses its own credentials; there's nothing to sign in to."
        )
        return None
    return cfg


def cmd_login(args: argparse.Namespace, console: Console) -> int:
    cfg = _remote(args.name, console)
    if cfg is None:
        return 1
    try:
        OAuth(cfg.name, cfg.url, cfg.oauth).login(notify=lambda text: console.print(escape(text)))
    except McpError as e:
        console.print(f"[red]Sign-in failed:[/] {escape(str(e))}")
        return 1
    console.print(f"[green]Signed in to {escape(cfg.name)}.[/]")
    return _connect_new(cfg.name, console, login=False)


def cmd_logout(args: argparse.Namespace, console: Console) -> int:
    cfg = _remote(args.name, console)
    if cfg is None:
        return 1
    removed = OAuth(cfg.name, cfg.url, cfg.oauth).logout()
    console.print(f"Signed out of {escape(cfg.name)}." if removed else f"You weren't signed in to {escape(cfg.name)}.")
    return 0


def cmd_tools(args: argparse.Namespace, console: Console) -> int:
    manager = _manager([args.name])
    if args.name not in manager.servers:
        console.print(f"[red]error:[/] no server named {escape(args.name)} (see lcode mcp list)")
        return 1
    _check(manager, console)
    state = manager.servers[args.name]
    try:
        if state.status != "ready":
            console.print(f"[red]✗[/] {escape(args.name)}: {escape(state.error)}")
            return 1
        table = Table(header_style="bold", title=f"{args.name}: {len(state.tools)} tools", title_justify="left")
        table.add_column("Tool", style="cyan")
        table.add_column("Description")
        for tool in state.tools:
            first = (tool.get("description") or "").strip().split("\n")[0]
            table.add_row(escape(tool["name"]), escape(first[:120]))
        table.caption = f'~{format_tokens(manager.cost())} tokens of definitions. Offer fewer with "tools" in mcp.json.'
        console.print(table)
        return 0
    finally:
        manager.close()


def cmd_toggle(args: argparse.Namespace, console: Console) -> int:
    servers = _user_server(args.name, console)
    if servers is None:
        return 1
    entry = servers[args.name]
    if args.action == "disable":
        entry["disabled"] = True
    else:
        entry.pop("disabled", None)
    mcp_config.save_user(servers)
    console.print(f"{escape(args.name)} is {'off' if args.action == 'disable' else 'on'} for new sessions.")
    return 0
