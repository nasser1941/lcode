"""Command-line entry point: `lcode` (chat) plus the setup, models, doctor and config subcommands."""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from rich.prompt import Confirm
from rich.table import Table

from lcode import __version__, catalog, config, limits, web
from lcode.agent import Agent, Settings
from lcode.catalog import ModelSpec
from lcode.config import ConfigError, format_tokens, parse_context
from lcode.hardware import Hardware, detect
from lcode.ollama import MIN_VERSION, Ollama, OllamaError, redact, version_tuple

console = Console(highlight=False)

OLLAMA_INSTALL = {
    "linux": "curl -fsSL https://ollama.com/install.sh | sh",
    "macos": "brew install ollama && brew services start ollama   (or download the app from https://ollama.com)",
}


class NotInstalled(Exception):
    pass


def fail(message: str, code: int = 1):
    console.print(f"[red]error:[/] {message}")
    sys.exit(code)


def check_ollama(ollama: Ollama, hw: Hardware | None = None) -> str:
    try:
        version = ollama.version()
    except OllamaError as e:
        hint = OLLAMA_INSTALL.get((hw or detect()).os, OLLAMA_INSTALL["linux"])
        fail(f"{e}\n\nIs Ollama installed and running? Install it with:\n  {hint}")
    if version_tuple(version) < MIN_VERSION:
        fail(f"Ollama {version} is too old; lcode needs {'.'.join(map(str, MIN_VERSION))} or newer. Update Ollama.")
    return version


def resolve_model(ollama: Ollama, name: str) -> tuple[str, ModelSpec | None]:
    """Map a catalog key or Ollama tag to an installed Ollama model name."""
    spec = catalog.find(name)
    installed = ollama.installed_names()
    if spec:
        for candidate in (spec.local_name, spec.tag):
            if candidate in installed:
                return candidate, spec
        raise NotInstalled(f"{spec.name} is not installed yet. Run:  lcode setup {spec.key}")
    if name in installed or f"{name}:latest" in installed:
        return name, None
    raise NotInstalled(
        f"Model '{name}' is not installed in Ollama. Pick one with `lcode models` and install it with "
        f"`lcode setup <model>`, or pull any Ollama model with `ollama pull {name}`."
    )


def choose_context(
    ollama: Ollama, model: str, spec: ModelSpec | None, requested: int | None, hw: Hardware
) -> tuple[int, str]:
    """Pick the context window: requested > largest that fits (catalog models) > 32K. Capped at the model max."""
    try:
        limit = ollama.max_context(model) or (spec.max_context if spec else None)
    except OllamaError:
        limit = spec.max_context if spec else None
    note = ""
    if requested:
        ctx = requested
    elif spec:
        ctx = limits.cap(model, spec.fit(hw)[0] or catalog.MIN_USEFUL_CONTEXT)
    else:
        ctx = limits.cap(model, 32768)
    if limit and ctx > limit:
        ctx, note = limit, f"capped at the model maximum of {format_tokens(limit)}"
    if spec and requested and spec.memory_gib(ctx) > hw.budget_gib:
        note = (
            f"needs ~{spec.memory_gib(ctx):.0f} GB but only ~{hw.budget_gib:.0f} GB is available; "
            "expect out-of-memory errors or heavy slowdowns"
        )
    return ctx, note


# ----------------------------------------------------------------------------- lcode models


def print_models(ollama: Ollama | None, hw: Hardware, current: str | None = None) -> None:
    try:
        installed = ollama.installed_names() if ollama else set()
    except OllamaError:
        installed = set()
    rec = catalog.recommend(hw)
    wide = console.width >= 130  # the descriptive columns don't fit an 80-column terminal
    columns = ["Key", "Model", "Type", "Size", "Max ctx", "Fits here", "Speed", "Status"]
    if not wide:
        columns = [c for c in columns if c not in ("Model", "Type")]
    table = Table(title=f"Models for this machine: {hw.describe()}", title_justify="left", header_style="bold")
    for col in columns:
        table.add_column(
            col, no_wrap=col in ("Key", "Size", "Max ctx", "Fits here"), min_width=11 if col == "Status" else None
        )
    for spec in catalog.load():
        ctx, speed = spec.fit(hw)
        marks = []
        if spec.local_name in installed or spec.tag in installed:
            marks.append("[green]installed[/]")
        if rec and rec[0].key == spec.key:
            marks.append("[cyan]recommended[/]")
        if current and catalog.find(current) is spec:
            marks.append("[bold]current[/]")
        marks.append("tested" if spec.tested else "[dim]untested[/]")
        row = {
            "Key": spec.key,
            "Model": spec.name,
            "Type": spec.params,
            "Size": f"{spec.size_gb:.1f} GB",
            "Max ctx": format_tokens(spec.max_context),
            "Fits here": format_tokens(ctx) if ctx else "[red]no[/]",
            "Speed": speed if wide else speed.split(" (")[0],
            "Status": ", ".join(marks) if wide else "\n".join(marks),
        }
        table.add_row(*(row[c] for c in columns))
    console.print(table)
    console.print(
        "[dim]Install one with [/]lcode setup <key>[dim]. 'Fits here' is the largest context that fits in memory; "
        "choose another with --context. Any other Ollama model with tool calling also works: lcode --model <tag>[/]"
    )


def cmd_models(args) -> None:
    cfg = config.load()
    ollama = Ollama(cfg["ollama_host"])
    print_models(ollama, detect(), cfg["model"])


# ----------------------------------------------------------------------------- lcode setup


def pull_with_progress(ollama: Ollama, tag: str) -> None:
    columns = (
        TextColumn("  {task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
    with Progress(*columns, console=console) as progress:
        tasks: dict[str, int] = {}

        def on_event(event: dict) -> None:
            digest, total = event.get("digest"), event.get("total")
            if digest and total:
                if digest not in tasks:
                    tasks[digest] = progress.add_task(f"layer {digest.split(':')[-1][:12]}", total=total)
                progress.update(tasks[digest], completed=event.get("completed", 0))
            elif event.get("status") and not digest:
                progress.console.print(f"  [dim]{event['status']}[/]")

        ollama.pull(tag, on_event)


def cmd_setup(args) -> None:
    cfg = config.load()
    ollama = Ollama(cfg["ollama_host"])
    hw = detect()
    console.print(f"[bold]Machine:[/] {hw.describe()}")
    console.print(f"[bold]Ollama:[/]  {redact(ollama.host)} (version {check_ollama(ollama, hw)})")
    requested = None
    if args.context:
        try:
            requested = parse_context(args.context)
        except ConfigError as e:
            fail(str(e))

    if args.model:
        spec = catalog.find(args.model)
        tag = spec.tag if spec else args.model
    else:
        rec = catalog.recommend(hw)
        if not rec:
            fail("no catalog model fits in this machine's memory. See `lcode models`.")
        spec, _ = rec
        tag = spec.tag
        console.print(f"[bold]Recommended:[/] {spec.name} ({spec.params}) — {spec.notes}")
    if spec:
        ctx = requested or spec.fit(hw)[0] or catalog.MIN_USEFUL_CONTEXT
        ctx = min(ctx, spec.max_context)
        console.print(
            f"[bold]Context:[/] {format_tokens(ctx)} tokens (~{spec.memory_gib(ctx):.0f} GB of the "
            f"~{hw.budget_gib:.0f} GB available; speed: {spec.fit(hw)[1]})"
        )
        if spec.memory_gib(ctx) > hw.budget_gib:
            console.print("[yellow]warning:[/] this probably doesn't fit in memory; pick a smaller --context or model.")
        if not spec.tested:
            console.print(
                "[yellow]note:[/] this model hasn't been verified with lcode yet — please report how it goes."
            )
    else:
        ctx = requested
        console.print(f"[yellow]note:[/] {tag} is not in lcode's catalog, so its memory needs can't be estimated.")

    if not args.yes and not Confirm.ask(f"Download {tag} and make it lcode's default model?", default=True):
        console.print("Nothing changed. See all options with `lcode models`.")
        return

    try:
        pull_with_progress(ollama, tag)
        name = tag
        if spec and ollama.create_text_only(spec.local_name, tag):
            name = spec.local_name
            console.print(f"  Created [bold]{name}[/] (text-only variant: frees ~1 GB of GPU memory, no extra disk)")
    except OllamaError as e:
        fail(str(e))

    updates: dict = {"model": spec.key if spec else tag}
    if ctx:
        updates["context"] = ctx
    config.save(updates, remove=() if ctx else ("context",))
    console.print(f"\n[green]✓ Ready.[/] Saved to {config.CONFIG_PATH}\n  cd into a project and run: [bold]lcode[/]")


# ----------------------------------------------------------------------------- lcode doctor


def cmd_doctor(args) -> None:
    ok = True

    def line(label: str, value: str, good: bool | None = True) -> None:
        mark = {True: "[green]✓[/]", False: "[red]✗[/]", None: "[yellow]![/]"}[good]
        console.print(f"{mark} [bold]{label:<10}[/] {value}")

    try:
        cfg = config.load()
    except ConfigError as e:
        fail(str(e))
    hw = detect()
    console.print(
        f"lcode {__version__} · Python {platform.python_version()} · {platform.system()} {platform.release()} "
        f"({platform.machine()})\n"
    )
    line("Hardware", hw.describe(), True if (hw.gpu or hw.unified) else None)
    line(
        "Config",
        f"{config.CONFIG_PATH}" + ("" if config.CONFIG_PATH.exists() else " (not created yet)"),
        True if config.CONFIG_PATH.exists() else None,
    )
    ollama = Ollama(cfg["ollama_host"])
    try:
        version = ollama.version()
        fresh = version_tuple(version) >= MIN_VERSION
        line("Ollama", f"{redact(ollama.host)} · version {version}" + ("" if fresh else " (too old)"), fresh)
        ok &= fresh
    except OllamaError as e:
        line("Ollama", f"{e}", False)
        line("", f"install: {OLLAMA_INSTALL.get(hw.os, OLLAMA_INSTALL['linux'])}", None)
        sys.exit(1)
    try:
        model, spec = resolve_model(ollama, cfg["model"])
        line("Model", f"{cfg['model']} → {model}", True)
        ctx, note = choose_context(ollama, model, spec, cfg["context"], hw)
        detail = f"{format_tokens(ctx)} tokens"
        if spec:
            detail += f" · ~{spec.memory_gib(ctx):.0f} GB needed, ~{hw.budget_gib:.0f} GB available"
        line("Context", detail + (f" ({note})" if note else ""), None if note else True)
        if limits.get(model):
            line(
                "Limit",
                f"{format_tokens(limits.get(model))} for {model}: larger contexts ran out of GPU memory "
                f"here (delete {limits.path()} to try again)",
                None,
            )
        loaded = [m for m in ollama.running() if m.get("name") == model or m.get("model") == model]
        if loaded:
            m = loaded[0]
            line(
                "Loaded",
                f"{m.get('size_vram', 0) / 1e9:.1f} GB on GPU of {m.get('size', 0) / 1e9:.1f} GB, "
                f"context {m.get('context_length', '?')}",
                True,
            )
    except NotInstalled as e:
        line("Model", str(e), False)
        ok = False
    backend = web.resolve_backend(cfg["search_backend"], cfg["searxng_url"])
    if cfg["web"] == "off":
        line("Web", "off (lcode config set web on)", None)
    elif backend:
        line("Web", f"{cfg['web']} · search via {web.BACKEND_NAMES[backend]} · page fetching", True)
    else:
        line(
            "Web",
            f"{cfg['web']} · page fetching only; for search set OLLAMA_API_KEY (free key: "
            "https://ollama.com/settings/keys) or see the docs",
            None,
        )
    line(
        "ripgrep",
        "found" if shutil.which("rg") else "not found — install it for faster search",
        bool(shutil.which("rg")) or None,
    )
    line("git", "found" if shutil.which("git") else "not found", bool(shutil.which("git")) or None)
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------------- lcode config


def cmd_config(args) -> None:
    try:
        if args.action == "set":
            config.save({args.key: args.value})
            console.print(f"Set {args.key} = {config.read_file()[args.key]}")
        elif args.action == "unset":
            config.coerce(args.key, None)
            config.save({}, remove=(args.key,))
            console.print(f"Unset {args.key} (back to the default)")
        elif args.action == "path":
            console.print(str(config.CONFIG_PATH))
        else:
            cfg, from_file = config.load(), config.read_file()
            table = Table(title=str(config.CONFIG_PATH), title_justify="left", header_style="bold")
            for col in ("Setting", "Value", "Source", "Description"):
                table.add_column(col)
            for key, (default, _, help_text) in config.SETTINGS.items():
                source = "file" if key in from_file else "default"
                if cfg[key] != from_file.get(key, default):
                    source = "environment"
                value = format_tokens(cfg[key]) if key == "context" and cfg[key] else str(cfg[key])
                table.add_row(key, value, source, help_text)
            console.print(table)
            console.print("[dim]Change with: lcode config set <setting> <value>[/]")
    except ConfigError as e:
        fail(str(e))


# ----------------------------------------------------------------------------- lcode (chat)


def cmd_chat(args) -> None:
    from lcode.repl import repl

    try:
        cfg = config.load()
        requested_ctx = parse_context(args.context) if args.context else cfg["context"]
        same_model = (catalog.find(args.model) or args.model) == (catalog.find(cfg["model"]) or cfg["model"])
        if not args.context and args.model and not same_model:
            requested_ctx = None  # the saved context was sized for the saved model; fit this one instead
    except ConfigError as e:
        fail(str(e))
    cwd = Path(args.repo).expanduser().resolve()
    if not cwd.is_dir():
        fail(f"not a directory: {cwd}")
    ollama = Ollama(cfg["ollama_host"])
    hw = detect()
    check_ollama(ollama, hw)
    try:
        model, spec = resolve_model(ollama, args.model or cfg["model"])
    except NotInstalled as e:
        if not args.model and cfg["model"] == config.DEFAULT_MODEL and not config.CONFIG_PATH.exists():
            fail("lcode isn't set up yet. Run:  lcode setup")
        fail(str(e))
    context, note = choose_context(ollama, model, spec, requested_ctx, hw)
    if note:
        console.print(f"[yellow]Context {format_tokens(context)}: {note}[/]")
    mode = "yolo" if args.yolo else ("auto-edit" if args.auto_edit else cfg["permission_mode"])
    num_batch = cfg["num_batch"]
    if num_batch is None and spec and spec.num_batch:
        if model == spec.local_name:
            num_batch = spec.num_batch
        else:  # the tuned batch size assumes the text-only variant; the vision projector needs that VRAM
            console.print(f"[dim]Tip: run `lcode setup {spec.key}` once to create the faster text-only variant.[/]")
    settings = Settings(
        model=model,
        context=context,
        num_batch=num_batch,
        keep_alive=cfg["keep_alive"],
        think=cfg["think"] and not args.no_think,
        show_thinking=args.show_thinking,
        permission_mode=mode,
        web="off" if args.no_web else cfg["web"],
        search_backend=cfg["search_backend"],
        searxng_url=cfg["searxng_url"],
        checkpoints=cfg["checkpoints"],
    )
    agent = Agent(ollama, settings, cwd, console=console)
    repl(agent, prompt=args.prompt, hardware=hw, cont=args.cont, resume=args.resume)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcode",
        description="A local-first terminal coding agent powered by open-weight models via Ollama.",
        epilog="Subcommands: lcode setup | models | doctor | config  (lcode <subcommand> --help). "
        "Docs: https://nasser1941.github.io/lcode/",
    )
    parser.add_argument("-p", "--prompt", help="run one request non-interactively and exit")
    parser.add_argument("-m", "--model", help="catalog key (see `lcode models`) or any installed Ollama model")
    parser.add_argument("--context", "--ctx", dest="context", help="context window, e.g. 65536, 128k or 1m")
    parser.add_argument("-r", "--repo", default=".", help="working directory (default: current directory)")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last session here")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION",
        help="resume a saved session: pick from a list, or give its number, name or id",
    )
    parser.add_argument("--auto-edit", action="store_true", help="apply file edits without asking")
    parser.add_argument("--yolo", action="store_true", help="never ask for permission (edits and commands)")
    parser.add_argument("--no-think", action="store_true", help="disable model reasoning (faster, less accurate)")
    parser.add_argument("--no-web", action="store_true", help="don't let the model search or fetch web pages")
    parser.add_argument("--show-thinking", action="store_true", help="print the model's reasoning as it streams")
    parser.add_argument("-V", "--version", action="version", version=f"lcode {__version__}")
    return parser


def build_subparsers() -> dict[str, argparse.ArgumentParser]:
    subs = {}
    p = argparse.ArgumentParser(prog="lcode setup", description="Download a model and make it lcode's default.")
    p.add_argument("model", nargs="?", help="catalog key or Ollama tag (default: the best fit for this machine)")
    p.add_argument("--context", "--ctx", dest="context", help="context window, e.g. 128k (default: largest that fits)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    subs["setup"] = p
    subs["models"] = argparse.ArgumentParser(prog="lcode models", description="List models and how they fit here.")
    subs["doctor"] = argparse.ArgumentParser(prog="lcode doctor", description="Check the installation.")
    p = argparse.ArgumentParser(prog="lcode config", description="Show or change settings.")
    p.add_argument("action", nargs="?", choices=["show", "set", "unset", "path"], default="show")
    p.add_argument("key", nargs="?", choices=list(config.SETTINGS))
    p.add_argument("value", nargs="?")
    subs["config"] = p
    return subs


COMMANDS = {"setup": cmd_setup, "models": cmd_models, "doctor": cmd_doctor, "config": cmd_config}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] in COMMANDS:
            parser = build_subparsers()[argv[0]]
            args = parser.parse_args(argv[1:])
            if argv[0] == "config" and args.action in ("set", "unset") and not args.key:
                parser.error(f"config {args.action} needs a setting name")
            if argv[0] == "config" and args.action == "set" and args.value is None:
                parser.error("config set needs a value")
            COMMANDS[argv[0]](args)
        else:
            cmd_chat(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelled.[/]")
        sys.exit(130)
    except requests.ConnectionError as e:
        fail(f"lost connection to Ollama: {e}")


if __name__ == "__main__":
    main()
