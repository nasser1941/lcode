"""Command-line entry point: `lcode` (chat) plus the setup, models, doctor and config subcommands."""

from __future__ import annotations

import argparse
import contextlib
import platform
import re
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import requests
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from rich.prompt import Confirm
from rich.table import Table

from lcode import __version__, backends, catalog, config, limits, sessions, web
from lcode.agent import Settings
from lcode.catalog import ModelSpec
from lcode.cloud import is_cloud, signin_command
from lcode.config import ConfigError, format_tokens, parse_context
from lcode.hardware import Hardware, detect
from lcode.ollama import MIN_VERSION, Ollama, OllamaError, redact, version_tuple

console = Console(highlight=False)


class NotInstalled(Exception):
    pass


def fail(message: str, code: int = 1):
    console.print(f"[red]error:[/] {message}")
    sys.exit(code)


def connect(cfg: dict) -> Ollama:
    """The model server the settings point at (an OpenAI-compatible one has the same interface)."""
    if cfg["backend"] == "ollama":
        return Ollama(cfg["ollama_host"])
    try:
        return backends.connect(cfg)
    except OllamaError as e:
        fail(str(e))


def check_ollama(ollama: Ollama, hw: Hardware | None = None) -> str:
    from lcode import api

    try:
        return api.check_server(ollama, hw)
    except api.SetupError as e:
        fail(str(e))


def resolve_model(ollama: Ollama, name: str) -> tuple[str, ModelSpec | None]:
    """Map a catalog key or Ollama tag to an installed Ollama model name (or a model the server serves)."""
    if backends.is_openai(ollama):
        try:
            return backends.pick_model(ollama, name), None
        except OllamaError as e:
            raise NotInstalled(str(e)) from e
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
    if backends.is_openai(ollama):
        return backends.context_for(ollama, model, requested)
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


def print_served(
    client: backends.OpenAICompatible, current: str | None, numbered: bool = False, out: Console | None = None
) -> list[str]:
    out = out or console
    try:
        served = client.chat_models()
        current = backends.pick_model(client, current) if current else None
    except OllamaError as e:
        if not isinstance(e, backends.BackendError) or "doesn't serve" not in str(e):
            out.print(f"[red]{e}[/]")
            return []
        served = []
    out.print(f"[bold]Models served by {escape(client.describe())}[/]")
    for i, name in enumerate(served, 1):
        number = f"[cyan]{i:>3}[/]  " if numbered else "  "
        out.print(f"{number}{escape(name)}" + ("  [bold]current[/]" if name == current else ""))
    if not served:
        out.print("  none listed")
    if not numbered:
        out.print(
            "[dim]Models are downloaded and loaded in the server itself. Choose one with [/]lcode --model <id>[dim] "
            "or [/]lcode config set model <id>[dim].[/]"
        )
    return served


def other_installed(ollama: Ollama, installed: set[str]) -> list[str]:
    """Installed Ollama models outside the catalog that can chat and call tools (so lcode can use them), by name.
    Embedding models are left out: Ollama may list `tools` for them too, but not `completion`."""
    known = {name for spec in catalog.load() for name in (spec.tag, spec.local_name)}
    usable = []
    for name in sorted({n.removesuffix(":latest") for n in installed} - known):
        try:
            capabilities = ollama.show(name).get("capabilities") or []
            if "completion" in capabilities and "tools" in capabilities:
                usable.append(name)
        except OllamaError:
            continue
    return usable


def model_choices(ollama: Ollama | None) -> list[str]:
    """What `/models` numbers, in its order: the catalog's keys, then other installed models that call tools.
    For an OpenAI-compatible server, the models it serves."""
    if ollama is not None and backends.is_openai(ollama):
        try:
            return ollama.chat_models()  # type: ignore[union-attr]
        except OllamaError:
            return []
    try:
        installed = ollama.installed_names() if ollama else set()
    except OllamaError:
        installed = set()
    return [spec.key for spec in catalog.load()] + (other_installed(ollama, installed) if ollama else [])


def print_models(
    ollama: Ollama | None,
    hw: Hardware,
    current: str | None = None,
    numbered: bool = False,
    out: Console | None = None,
) -> list[str]:
    """The models table; with `numbered`, a number on each row to pick it by. Returns the rows' names in order."""
    out = out or console
    if ollama is not None and backends.is_openai(ollama):
        return print_served(ollama, current, numbered, out)  # type: ignore[arg-type]
    try:
        installed = ollama.installed_names() if ollama else set()
    except OllamaError:
        installed = set()
    rec = catalog.recommend(hw)
    wide = out.width >= 130  # the descriptive columns don't fit an 80-column terminal
    columns = ["Key", "Model", "Type", "Size", "Max ctx", "Fits here", "Speed", "Status"]
    if not wide:
        columns = [c for c in columns if c not in ("Model", "Type")]
    if numbered:
        columns.insert(0, "#")
    table = Table(title=f"Models for this machine: {hw.describe()}", title_justify="left", header_style="bold")
    for col in columns:
        table.add_column(
            col,
            no_wrap=col in ("#", "Key", "Size", "Max ctx", "Fits here"),
            min_width=11 if col == "Status" else None,
            justify="right" if col == "#" else "left",
            style="cyan" if col == "#" else None,
        )
    choices: list[str] = []
    for spec in catalog.load():
        choices.append(spec.key)
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
            "#": str(len(choices)),
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
    for name in other_installed(ollama, installed) if ollama else []:
        choices.append(name)
        marks = ["[green]installed[/]"] + (["[bold]current[/]"] if name == current else []) + ["[dim]not in catalog[/]"]
        row = {"#": str(len(choices)), "Key": escape(name), "Status": ", ".join(marks) if wide else "\n".join(marks)}
        table.add_row(*(row.get(c, "") for c in columns))
    out.print(table)
    if not numbered:
        out.print(
            "[dim]Install one with [/]lcode setup <key>[dim]. 'Fits here' is the largest context that fits in memory; "
            "choose another with --context. Any other Ollama model with tool calling also works: lcode --model <tag>[/]"
        )
    return choices


def cmd_models(args) -> None:
    cfg = config.load()
    print_models(connect(cfg), detect(), cfg["model"])


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
    if cfg["backend"] != "ollama":
        name = backends.PRESETS[cfg["backend"]][0]
        fail(
            f"lcode setup downloads models into Ollama, but lcode uses {name} (backend = {cfg['backend']}). "
            f"Download and load a model in {name}, then: lcode config set model <id> (see lcode models). "
            "Back to Ollama: lcode config set backend ollama"
        )
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
    slots = None  # how many subagents could run at once with this model and context
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
    ollama = connect(cfg)
    openai = backends.is_openai(ollama)
    try:
        if openai:
            ollama.version()
            served = ollama.chat_models()  # type: ignore[attr-defined]
            line("Server", f"{ollama.describe()} · {len(served)} model(s) served", True)
        else:
            version = ollama.version()
            fresh = version_tuple(version) >= MIN_VERSION
            line("Ollama", f"{redact(ollama.host)} · version {version}" + ("" if fresh else " (too old)"), fresh)
            ok &= fresh
    except OllamaError as e:
        line("Server" if openai else "Ollama", f"{e}", False)
        if openai:
            line("", f"start {ollama.name}'s server, or set its address: lcode config set base_url <url>", None)
        else:
            from lcode import api

            for text in api.ollama_down(ollama.host, hw.os).splitlines():
                line("", escape(text.strip()), None)
        sys.exit(1)
    try:
        model, spec = resolve_model(ollama, cfg["model"])
        line("Model", f"{cfg['model']} → {model}", True)
        if is_cloud(model):
            line(
                "",
                "runs in Ollama's cloud: what lcode sends goes to ollama.com, billed per token, and this Ollama "
                f"must be signed in once: {signin_command(ollama.host)}",
                None,
            )
        ctx, note = choose_context(ollama, model, spec, cfg["context"], hw)
        if spec:
            from lcode.subagents import parallel_slots

            slots = (parallel_slots(spec, ctx, hw), format_tokens(ctx))
        detail = f"{format_tokens(ctx)} tokens"
        if spec:
            detail += f" · ~{spec.memory_gib(ctx):.0f} GB needed, ~{hw.budget_gib:.0f} GB available"
        line("Context", detail + (f" ({note})" if note else ""), None if note else True)
        from lcode import vision

        seer = vision.pick_model(ollama, model, cfg["vision_model"])
        if seer:
            line(
                "Vision",
                f"{seer} looks at screenshots and images" + ("" if seer == model else " (loaded when needed)"),
                True,
            )
        elif openai:
            line("Vision", "off; set vision_model to a model the server serves that can see images", None)
        else:
            line("Vision", "off" if cfg["vision_model"] == "off" else vision.INSTALL_HINT, None)
        if seer:
            from lcode import clipboard

            line("Clipboard", *clipboard.status())
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
    from lcode.mcp import config as mcp_config

    try:
        servers = mcp_config.load_user()
        names = ", ".join(n for n, raw in servers.items() if not (isinstance(raw, dict) and raw.get("disabled")))
        line(
            "MCP", f"{names} (check them with lcode mcp list)" if names else "no servers (see lcode mcp catalog)", True
        )
    except ValueError as e:
        line("MCP", str(e), False)
        ok = False
    line("ComfyUI", *comfyui_status(cfg))
    if cfg["sandbox"] == "off":
        line("Sandbox", "off (lcode config set sandbox docker)", True)
    else:
        from lcode.sandbox import engine_problem

        problem = engine_problem(cfg["sandbox"])
        network = "network on" if cfg["sandbox_network"] else "no network"
        line("Sandbox", problem or f"{cfg['sandbox']} · {network}", problem is None)
        ok &= problem is None
    if not cfg["subagents"]:
        line("Agents", "subagents off (lcode config set subagents true)", True)
    else:
        from lcode import subagents

        types, problems = subagents.load_types(Path.cwd())
        custom = [t.name for t in types.values() if t.source != "built-in"]
        n = cfg["max_parallel_agents"]
        how = f"up to {n} at once (needs OLLAMA_NUM_PARALLEL={n} on the Ollama server)" if n > 1 else "one at a time"
        line("Agents", f"subagents {how}" + (f" · custom: {', '.join(custom)}" if custom else ""), True)
        if slots and n == 1 and slots[0] > 1:
            line(
                "",
                f"{slots[0]} could run at once here ({slots[0]} × {slots[1]} context fits): set "
                f"OLLAMA_NUM_PARALLEL={slots[0]} on the Ollama server and max_parallel_agents {slots[0]}",
                None,
            )
        elif slots and n > slots[0]:
            line("", f"{n} × {slots[1]} context may not fit in memory here; {slots[0]} would", None)
        for problem in problems:
            line("", problem, None)
    from lcode import codesearch, extensions, hooks, lsp

    hook_set, rules = hooks.load(Path.cwd(), extensions.status(Path.cwd()) == "approved")
    if hook_set.hooks or rules or hook_set.problems:
        events = sorted({h.event for h in hook_set.hooks})
        summary = f"{len(hook_set.hooks)} hook(s) ({', '.join(events)})" if hook_set.hooks else "no hooks"
        summary += f", {len(rules.allow)} allow and {len(rules.deny)} deny rule(s)"
        line("Hooks", summary, not hook_set.problems or None)
        for problem in hook_set.problems:
            line("", problem, None)
    from lcode.checkpoints import work_tree_for

    if cfg["embed_model"] == "off":
        line("Search", "semantic code search off (lcode config set embed_model auto)", True)
    else:
        embedder = codesearch.pick_model(ollama, cfg["embed_model"])
        if embedder is None:
            line("Search", "no embedding model for semantic code search: ollama pull qwen3-embedding:0.6b", None)
        else:
            index = codesearch.Index(work_tree_for(Path.cwd()), embedder)
            state = (
                f"{len(index.rows())} chunks indexed here" if index.exists() else "not indexed here yet: lcode index"
            )
            line("Search", f"{embedder} · {state}", True if index.exists() else None)

    if cfg["lsp"] == "off":
        line("Code intel", "off (lcode config set lsp auto)", True)
    else:
        found = lsp.available()
        names = [f"{lsp.NAMES[lang]} ({Path(cmds[0][0]).name})" for lang, cmds in found.items()]
        line("Code intel", ", ".join(names) or "no language servers found", True if found else None)
        if "python" not in found:
            line("", f"for Python: {lsp.INSTALL['python']}", None)
        if "typescript" not in found:
            line("", f"for TypeScript and JavaScript: {lsp.INSTALL['typescript']}", None)
    project_state = extensions.status(Path.cwd())
    ext = extensions.load(Path.cwd(), include_project=project_state == "approved", skills_from=cfg["skills"])
    found = f"{len(ext.commands)} command(s), {len(ext.skills)} skill(s)"
    note = {
        "none": "",
        "approved": " · this repository's are approved",
        "new": " · this repository brings its own: approve them when lcode asks",
        "changed": " · this repository's changed since you approved them: lcode asks again",
    }[project_state]
    line("Extras", found + note, None if project_state in ("new", "changed") else True)
    for problem in ext.problems:
        line("", problem, None)
    if cfg["memory"] == "off":
        line("Memory", "off (lcode config set memory ask)", True)
    else:
        from lcode.memory import Memory

        project, user = Memory(Path.cwd()).counts()
        line("Memory", f"{cfg['memory']} · {project} note(s) for this repository, {user} for every repository", True)
    line("Sessions", sessions_status(Path.cwd()), True)
    node = shutil.which("npx")
    line("Node.js", "found" if node else "not found (some MCP servers need npx)", True if node else None)
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


# ----------------------------------------------------------------------------- lcode bench


def cmd_bench(args) -> None:
    from lcode import bench

    if args.list:
        bench.print_tasks(console)
        return
    try:
        cfg = config.load()
        context = parse_context(args.context) if args.context else bench.DEFAULT_CONTEXT
        tasks = bench.select_tasks(args.tasks) * max(1, args.rounds)
    except (ConfigError, ValueError) as e:
        fail(str(e))
    if args.timeout <= 0:
        fail("--timeout must be a positive number of seconds")
    ollama = connect(cfg)
    hw = detect()
    version = check_ollama(ollama, hw)
    server = ollama.describe() if backends.is_openai(ollama) else ""
    console.print(f"[bold]lcode bench[/] · {hw.describe()} · {server or f'Ollama {version}'}")
    console.print("[dim]Each task runs in a new temporary folder with every permission granted and web access off.[/]")
    names = args.models or [cfg["model"]]
    resolved: dict[str, tuple[str, ModelSpec | None] | NotInstalled] = {}
    for name in names:
        try:
            resolved[name] = resolve_model(ollama, name)
        except NotInstalled as e:
            resolved[name] = e
    benched = {r[0] for r in resolved.values() if isinstance(r, tuple)}
    others = [m.get("name", "?") for m in ollama.running() if m.get("name", "").removesuffix(":latest") not in benched]
    if others:
        console.print(
            f"[yellow]Already loaded in Ollama: {', '.join(others)}. Models that share the GPU run slower; "
            "for comparable results, stop other sessions first.[/]"
        )
    runs = []
    for i, name in enumerate(names):
        found = resolved[name]
        if isinstance(found, NotInstalled):
            console.print(f"\n[yellow]Skipping {name}: {found}[/]")
            runs.append(bench.ModelRun(name, name, error=str(found)))
            continue
        model, spec = found
        ctx, note = choose_context(ollama, model, spec, context, hw)
        if note:
            console.print(f"[yellow]{model}, context {format_tokens(ctx)}: {note}[/]")
        num_batch = cfg["num_batch"]
        if num_batch is None and spec and spec.num_batch and model == spec.local_name:
            num_batch = spec.num_batch
        settings = Settings(
            model=model,
            context=ctx,
            num_batch=num_batch,
            keep_alive=cfg["keep_alive"],
            think=cfg["think"] and not args.no_think,
            permission_mode="yolo",
            web="off",
            checkpoints=False,
            prune=not args.no_prune,
            compact_at=cfg["compact_at"] if args.compact_at is None else args.compact_at,
            checks=cfg["checks"] if args.checks is None else args.checks,
            review=cfg["review"] if args.review is None else args.review,
            escalate_model=args.escalate_model,
            escalate_url=cfg["escalate_url"],
            escalate_price=cfg["escalate_price"],
            repo_map=args.repo_map,
            embed_model=cfg["embed_model"] if args.code_search else "off",
        )
        run = bench.run_model(
            ollama, name, settings, tasks, console, args.timeout, args.keep, args.verbose, args.session, args.retry
        )
        runs.append(run)
        if run.interrupted:
            break
        if i < len(names) - 1:
            try:
                ollama.unload(model)  # so the next model gets all the GPU memory
            except OllamaError:
                pass
    bench.print_summary(console, runs, tasks)
    if args.json:
        path = Path(args.json).expanduser()
        bench.write_json(path, bench.report(runs, hw, version, server))
        console.print(f"Results saved to {path}")
    if args.markdown:
        print("\n" + bench.markdown(runs, tasks, hw, version, server))
    if args.keep:
        console.print(f"[dim]Task folders are kept in {tempfile.gettempdir()} (lcode-bench-*).[/]")


# ----------------------------------------------------------------------------- lcode (chat)


def cmd_chat(args) -> None:
    import json

    from lcode import api
    from lcode.repl import repl

    output = args.output or "text"
    if output != "text" and not args.prompt:
        fail("--output json and stream-json need a request: lcode -p '…' --output json")
    if args.max_steps is not None and args.max_steps < 1:
        fail("--max-steps must be at least 1")
    quiet = output != "text"
    out = Console(stderr=True, highlight=False) if quiet else console  # stdout is for the JSON
    cwd = Path(args.repo).expanduser().resolve()
    if not cwd.is_dir():
        fail(f"not a directory: {cwd}")
    worktree = None
    if args.worktree is not None:
        from lcode import gitflow

        try:
            worktree = gitflow.start_worktree(cwd, args.worktree)
        except gitflow.GitError as e:
            fail(f"--worktree: {e}")
        cwd = worktree.cwd
        state = "a new worktree" if worktree.created_branch else "the worktree"
        out.print(f"[dim]Working in {state} {worktree.path}, on branch {escape(worktree.branch)}.[/]")
    mode = "yolo" if args.yolo else ("auto-edit" if args.auto_edit else ("plan" if args.plan else None))
    tools = [t for t in re.split(r"[,\s]+", args.allowed_tools or "") if t] if args.allowed_tools is not None else None
    options = api.Options(
        model=args.model,
        context=args.context,
        permission_mode=mode,
        mode=args.mode,
        allowed_tools=tools,
        max_steps=args.max_steps,
        think=False if args.no_think else None,
        web=False if args.no_web else None,
        memory=False if args.no_memory else None,
        mcp=not args.no_mcp,
        sandbox=args.sandbox,
        show_thinking=args.show_thinking,
        interactive=not args.prompt,
    )
    hw = detect()
    try:
        agent = api.open_agent(cwd, options, out, hw)
    except api.SetupError as e:
        if quiet:
            print(json.dumps({"type": "result", "status": "error", "error": str(e)}), flush=True)
            sys.exit(1)
        fail(str(e))
    exit_on_hangup()
    exit_code = 0
    if args.prompt and agent.settings.mode != "code":
        media_request(agent, args.prompt, out, quiet)  # makes the image or video, then exits
    if args.escalate and args.prompt:
        agent.escalate_approved = True  # the flag is the user's OK for this run
    try:
        if quiet:
            agent.perms.approve = lambda request: False  # nobody to ask: refuse what needs permission
            if output == "stream-json":
                agent.on_event = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
                print(json.dumps(api.start_event(agent)), flush=True)
            result = api.run_request(agent, args.prompt)
            print(json.dumps(result.to_json(), ensure_ascii=False, indent=None if output == "stream-json" else 2))
            exit_code = result.exit_code
        else:
            repl(agent, prompt=args.prompt, hardware=hw, cont=args.cont, resume=args.resume)
    finally:
        api.close_agent(agent)
        if worktree is not None:
            out.print(f"[dim]{escape(worktree.finish())}[/]")
    if exit_code:
        sys.exit(exit_code)


def exit_on_hangup() -> None:
    """A closed terminal (SIGHUP) or a plain kill (SIGTERM) ends lcode the usual way: it saves the session and stops
    what it started (ComfyUI, MCP servers, background jobs) instead of leaving them running."""

    def leave(signum, frame):
        raise SystemExit(128 + signum)

    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):  # only the main thread can
                signal.signal(sig, leave)


def sessions_status(cwd: Path) -> str:
    """How many conversations are saved, and where: a terminal with another LCODE_HOME or XDG_STATE_HOME has
    other ones."""
    listing = sessions.grouped(cwd, everything=True)
    saved = len(listing.here) + len(listing.elsewhere)
    return f"{saved} saved, {len(listing.here)} for this folder · in {sessions.sessions_dir()}"


def comfyui_status(cfg: dict) -> tuple[str, bool | None]:
    """lcode doctor's ComfyUI line: running or found, and whether the image model is there."""
    from lcode import comfy
    from lcode import media_catalog as catalog

    model = catalog.load().find(cfg.get("image_model") or "") or catalog.load().models[catalog.DEFAULT_IMAGE]
    if model.cloud:
        from lcode import media_cloud

        p = media_cloud.provider(model)
        if not p.api_key():
            return f"image model: {model.name}, at {p.name}, but {p.key_env} isn't set ({p.keys_url})", None
        return f"image model: {model.name}, at {p.name} ({p.key_env} is set)", True
    server = comfy.Server(cfg["comfyui_url"])
    stats = server.stats()
    install = None if stats else comfy.find_install(cfg["comfyui_path"])
    if not stats and install is None:
        return "not found: image mode offers to install it (or lcode config set comfyui_path <folder>)", None
    if stats:
        where = f"{(stats.get('system') or {}).get('comfyui_version', '?')} running at {server.url}"
    else:
        where = f"{install.path} (lcode starts it in image mode)"
    _, missing = catalog.chosen_files(model, catalog.lister(server if stats else None, install))
    if missing:
        size = sum(f.gb for f in missing)
        return f"{where} · {model.name} not installed: lcode media install {model.key} ({size:.1f} GB)", None
    return f"{where} · image model: {model.name}", True


def media_request(agent, prompt: str, out: Console, quiet: bool) -> None:
    """`lcode --mode image -p "…"`: make the image, say where it went (or, with --output json, a result object)."""
    import json

    from lcode import api, media

    try:
        made = media.request(agent, prompt)
    except (media.MediaError, KeyboardInterrupt) as e:
        message = str(e) or "Cancelled."
        if quiet:
            print(json.dumps({"type": "result", "status": "error", "error": message}), flush=True)
        else:
            out.print(f"[red]{escape(message)}[/]")
        api.close_agent(agent)
        sys.exit(1)
    if quiet:
        result = {
            "type": "result",
            "status": "success",
            "mode": agent.settings.mode,
            "files": [str(path) for one in made for path in one.files],
            "model": made[0].model,
            "seed": made[0].seed,
            "seeds": [one.seed for one in made],
            "width": made[0].width,
            "height": made[0].height,
            "seconds": round(sum(one.seconds for one in made), 1),
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
    api.close_agent(agent)
    sys.exit(0)


def compact_tokens(value: str) -> int:
    """`--compact-at`: a token count such as 64k, or 0."""
    try:
        return config.parse_compact_at(value)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcode",
        description="A local-first terminal coding agent powered by open-weight models via Ollama.",
        epilog="Subcommands: lcode setup | models | doctor | config | bench | trace | mcp | index | update "
        "(lcode <subcommand> --help). "
        "Docs: https://nasser1941.github.io/lcode/",
    )
    parser.add_argument("-p", "--prompt", help="run one request non-interactively and exit")
    parser.add_argument(
        "--output",
        choices=["text", "json", "stream-json"],
        help="with -p: text (default), json (one result object) or stream-json (an event per line)",
    )
    parser.add_argument("--max-steps", type=int, metavar="N", help="stop a request after N model steps")
    parser.add_argument("--allowed-tools", metavar="TOOLS", help='only these tools, e.g. "read_file,grep,glob,bash"')
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
    parser.add_argument(
        "-w",
        "--worktree",
        nargs="?",
        const="",
        metavar="NAME",
        help="work in a git worktree of its own, on branch NAME (new, or continue one); removed if nothing changed",
    )
    parser.add_argument("--plan", action="store_true", help="start in plan mode: agree on a plan before any change")
    parser.add_argument("--mode", choices=config.WORK_MODES, help="what lcode makes: code (default), image or video")
    parser.add_argument("--auto-edit", action="store_true", help="apply file edits without asking")
    parser.add_argument("--yolo", action="store_true", help="never ask for permission (edits and commands)")
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="with -p: let lcode ask escalate_model for help when stuck, without asking you (it costs money)",
    )
    parser.add_argument("--no-think", action="store_true", help="disable model reasoning (faster, less accurate)")
    parser.add_argument("--no-web", action="store_true", help="don't let the model search or fetch web pages")
    parser.add_argument("--no-mcp", action="store_true", help="don't start MCP servers in this session")
    parser.add_argument("--no-memory", action="store_true", help="don't load or save memory notes in this session")
    parser.add_argument("--sandbox", action="store_true", help="run shell commands in a container (Docker or Podman)")
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
    p = argparse.ArgumentParser(
        prog="lcode media", description="Image and video models: list them, with how they fit here, or install one."
    )
    p.add_argument("action", nargs="?", choices=["list", "install"], default="list")
    p.add_argument("model", nargs="?", help="with install: a model key from the list, e.g. z-image-turbo")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before downloading")
    subs["media"] = p
    p = argparse.ArgumentParser(prog="lcode config", description="Show or change settings.")
    p.add_argument("action", nargs="?", choices=["show", "set", "unset", "path"], default="show")
    p.add_argument("key", nargs="?", choices=list(config.SETTINGS))
    p.add_argument("value", nargs="?")
    subs["config"] = p
    p = argparse.ArgumentParser(
        prog="lcode bench",
        description="Score models on small coding tasks on this machine: pass rate, speed and memory.",
    )
    p.add_argument("models", nargs="*", help="catalog keys or Ollama tags (default: the configured model)")
    p.add_argument("--context", "--ctx", dest="context", help="context window (default: 32k, the same everywhere)")
    p.add_argument("--tasks", help="comma-separated task ids to run (default: all; see --list)")
    p.add_argument("--timeout", type=float, default=300, help="seconds per task (default: 300)")
    p.add_argument("--json", metavar="FILE", help="also save the results as JSON")
    p.add_argument("--markdown", action="store_true", help="also print a Markdown table for a test report")
    p.add_argument("--no-think", action="store_true", help="run without model reasoning")
    p.add_argument("--keep", action="store_true", help="keep each task's folder and transcript for inspection")
    p.add_argument("-v", "--verbose", action="store_true", help="show the model working, like a normal session")
    p.add_argument("--list", action="store_true", help="list the tasks and exit")
    p.add_argument(
        "--session",
        action="store_true",
        help="run all tasks in one conversation, to test a long session and its context management",
    )
    p.add_argument(
        "--rounds", type=int, default=1, help="run the tasks this many times (with --session: a longer session)"
    )
    p.add_argument("--repo-map", action="store_true", help="give the model the repository map (off by default)")
    p.add_argument("--code-search", action="store_true", help="index each task's folder and offer semantic code search")
    p.add_argument("--no-prune", action="store_true", help="don't remove old tool output (to compare, with --session)")
    p.add_argument(
        "--review", choices=["auto", "off"], help="review each change before it counts (default: the setting)"
    )
    p.add_argument(
        "--retry",
        action="store_true",
        help='give a failed task one more try, as a user would: "That didn\'t fix it. Look again."',
    )
    p.add_argument(
        "--escalate",
        dest="escalate_model",
        metavar="MODEL",
        help="a stronger model to ask when stuck and on --retry, e.g. ollama/qwen3.8-27b (no questions asked)",
    )
    p.add_argument(
        "--checks",
        metavar="auto|off|COMMAND",
        help="run each task's checks before it counts as done (default: the checks setting)",
    )
    p.add_argument(
        "--compact-at",
        type=compact_tokens,
        metavar="TOKENS",
        help="summarize at this many tokens, e.g. 64k (default: the compact_at setting; 0: only at 85%%)",
    )
    subs["bench"] = p
    p = argparse.ArgumentParser(
        prog="lcode trace",
        description="Requests recorded on this machine (the trace setting): list them, show one step by step, "
        "export one for a bug report with secrets and personal details masked, or replay one with the recorded "
        "answers instead of a model and see where the tool results now differ.",
    )
    p.add_argument("action", nargs="?", default="list", help="list (default), show, export, replay or path")
    p.add_argument("trace", nargs="?", help="a number from the list (1: the newest), a trace id or a trace file")
    p.add_argument("-n", "--limit", type=int, default=20, help="list: how many (default: 20)")
    p.add_argument("-v", "--verbose", action="store_true", help="show: also the model's answers and results")
    p.add_argument(
        "-o", "--output", help="export: the file to write (default: lcode-trace-<id>.jsonl here; - prints it)"
    )
    p.add_argument("--repo", help="replay: in a copy of this folder (default: the recorded folder, at its commit)")
    p.add_argument(
        "--commands", action="store_true", help="replay: run shell commands and the checks again too (in the copy)"
    )
    p.add_argument("--keep", action="store_true", help="replay: keep the copy it ran in")
    subs["trace"] = p
    p = argparse.ArgumentParser(
        prog="lcode index",
        description="Build or refresh the semantic code search index of the repository (an Ollama embedding model).",
    )
    p.add_argument("-r", "--repo", default=".", help="a folder in the repository (default: the current folder)")
    p.add_argument("--cpu", action="store_true", help="embed on the CPU (slower, but keeps the GPU for lcode's model)")
    p.add_argument("--model", help="embedding model to use (default: the embed_model setting)")
    p.add_argument("--status", action="store_true", help="only show the state of the index")
    subs["index"] = p
    from lcode.mcp.commands import build_parser as mcp_parser

    subs["mcp"] = mcp_parser()
    p = argparse.ArgumentParser(
        prog="lcode update",
        description="Update lcode, and Ollama when it runs in Docker (to the newest version on Docker Hub, pinned "
        "to its version tag).",
    )
    p.add_argument("what", nargs="?", choices=["lcode", "ollama"], help="only this one (default: both)")
    p.add_argument("--check", action="store_true", help="only show which versions are out")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before updating")
    subs["update"] = p
    subs["acp"] = argparse.ArgumentParser(
        prog="lcode acp",
        description="Run as the agent inside an editor, over the Agent Client Protocol on stdin and stdout "
        "(Zed and other ACP editors start it for you; see the docs).",
    )
    subs["action"] = argparse.ArgumentParser(
        prog="lcode action",
        description="Run as a GitHub Action on a self-hosted runner: answer @lcode in issues and pull requests, "
        "and review pull requests. Configured through the action's inputs (see the docs).",
    )
    return subs


def cmd_update(args) -> None:
    from lcode import update

    sys.exit(update.command(console, args.what or "", check=args.check, yes=args.yes))


def cmd_acp(args) -> None:
    from lcode import acp

    sys.exit(acp.main())


def cmd_action(args) -> None:
    from lcode import action

    sys.exit(action.main())


def cmd_index(args) -> None:
    """Build or refresh the semantic search index of the repository around the current folder."""
    from lcode import codesearch
    from lcode.checkpoints import work_tree_for

    cfg = config.load()
    root = work_tree_for(Path(args.repo).expanduser().resolve())
    ollama = connect(cfg)
    setting = args.model or cfg["embed_model"]
    if setting == "off":
        fail("semantic code search is off (lcode config set embed_model auto)")
    model = codesearch.pick_model(ollama, "auto" if setting == "off" else setting)
    if model is None and backends.is_openai(ollama):
        fail(f"{ollama.describe()} serves no embedding model: load one there (e.g. qwen3-embedding-0.6b)")
    if model is None:
        fail(
            "no embedding model is installed. Install one, for example:\n  ollama pull qwen3-embedding:0.6b\n"
            "(or nomic-embed-text, smaller), then run lcode index again"
        )
    index = codesearch.Index(root, model)
    changed, removed = index.stale()
    if args.status:
        state = "no index yet" if not index.exists() else f"{len(index.rows())} chunks from {len(index.files)} files"
        console.print(f"{root} · {model} · {state} · {len(changed)} file(s) to (re)index, {len(removed)} removed")
        return
    if not changed and not removed:
        console.print(f"The index of {root} is up to date ({len(index.rows())} chunks, {model}).")
        return
    where = "the CPU" if args.cpu else "the GPU (lcode's model is reloaded at the next request)"
    console.print(f"Indexing {len(changed)} file(s) in {root} with {model} on {where}…")
    started = time.monotonic()
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TimeRemainingColumn

    with Progress("  [progress.description]{task.description}", BarColumn(), MofNCompleteColumn(),
                  TimeRemainingColumn(), console=console, transient=True) as bar:  # fmt: skip
        task = bar.add_task("chunks", total=None)
        try:
            files, chunks = index.update(
                ollama, on_gpu=not args.cpu, progress=lambda done, total: bar.update(task, completed=done, total=total)
            )
        except codesearch.SearchError as e:
            fail(str(e))
    console.print(
        f"[green]✓[/] Indexed {chunks} chunks from {files} file(s) in {time.monotonic() - started:.0f}s; "
        f"{len(index.rows())} chunks in all. The model can now search the code by meaning (search_code)."
    )


def cmd_media(args) -> None:
    """lcode media [list] | install <model>: the media catalog, what's installed in ComfyUI, downloads."""
    from rich.prompt import Confirm

    from lcode import comfy, media
    from lcode import media_catalog as catalog

    try:
        cfg = config.load()
    except ConfigError as e:
        fail(str(e))
    cat, hw = catalog.load(), detect()
    server = comfy.Server(cfg["comfyui_url"])
    install = comfy.find_install(cfg["comfyui_path"])
    running = server.stats() is not None
    installed = catalog.lister(server if running else None, install) if running or install else None
    if args.action == "install":
        model = cat.find(args.model or "")
        if model is None:
            fail(f"no media model {args.model!r}: lcode media lists them")
        if model.cloud:
            from lcode import media_cloud

            console.print(f"Nothing to install: {media_cloud.ready_note(model)}")
            return
        if not running and install is None:
            fail(f"lcode needs ComfyUI to install models into. {comfy.install_hint()}")
        ask = None if args.yes else (lambda q: Confirm.ask(q, default=True, console=console))
        try:
            saved = catalog.install(model, server if running else None, install, console, ask)
        except catalog.CatalogError as e:
            fail(str(e))
        how = f" ({len(saved)} file(s) downloaded)." if saved else "."
        console.print(f"[green]{model.name} is installed[/]{how}")
        return
    where = (
        f"ComfyUI at {server.url}" if running else (f"{install.path} (not running)" if install else "no ComfyUI found")
    )
    console.print(f"[dim]{hw.describe()} · {where} · image: {cfg['image_model']} · video: {cfg['video_model']}[/]\n")
    for mode in ("image", "video", "tool"):
        models = cat.for_mode(mode)
        console.print(
            "[bold]Tools for images[/] [dim](/upscale, /nobg)[/]"
            if mode == "tool"
            else f"[bold]{mode.capitalize()} models[/]"
        )
        if not models:
            console.print(f"  [dim]{media.SOON[mode]}[/]\n")
            continue
        for m in models:
            line = escape(catalog.describe(m, hw, installed))
            console.print(f"  [bold]{m.key:<16}[/] {escape(m.name)} [dim]· {line}[/]")
            if m.measured:
                console.print(f"  {'':<16} [dim]{escape(m.measured)}[/]")
        console.print()
    console.print("[dim]Install one: lcode media install <model> · choose in image mode: /model <model>[/]")


def cmd_trace(args) -> None:
    from lcode import traces

    sys.exit(traces.command(args, console))


def cmd_mcp(args) -> None:
    from lcode.mcp.commands import run

    sys.exit(run(args, console))


COMMANDS = {
    "setup": cmd_setup,
    "models": cmd_models,
    "doctor": cmd_doctor,
    "media": cmd_media,
    "config": cmd_config,
    "bench": cmd_bench,
    "trace": cmd_trace,
    "mcp": cmd_mcp,
    "index": cmd_index,
    "action": cmd_action,
    "acp": cmd_acp,
    "update": cmd_update,
}


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
