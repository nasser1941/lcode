"""lcode doctor: what lcode finds on this machine, and what to run to fix what's missing."""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

from rich.markup import escape

from lcode import __version__, backends, config, limits, residency, sessions, web
from lcode.cloud import is_cloud, signin_command
from lcode.config import ConfigError, format_tokens
from lcode.ollama import MIN_VERSION, OllamaError, redact, version_tuple


def cmd_doctor(args) -> None:
    from lcode import cli  # the CLI's console and Ollama, looked up when called (tests stand in for them)

    ok = True

    def line(label: str, value: str, good: bool | None = True) -> None:
        mark = {True: "[green]✓[/]", False: "[red]✗[/]", None: "[yellow]![/]"}[good]
        cli.console.print(f"{mark} [bold]{label:<10}[/] {value}")

    try:
        cfg = config.load()
    except ConfigError as e:
        cli.fail(str(e))
    hw = cli.detect()
    slots = None  # how many subagents could run at once with this model and context
    cli.console.print(
        f"lcode {__version__} · Python {platform.python_version()} · {platform.system()} {platform.release()} "
        f"({platform.machine()})\n"
    )
    line("Hardware", hw.describe(), True if (hw.gpu or hw.unified) else None)
    line(
        "Config",
        f"{config.CONFIG_PATH}" + ("" if config.CONFIG_PATH.exists() else " (not created yet)"),
        True if config.CONFIG_PATH.exists() else None,
    )
    ollama = cli.connect(cfg)
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
        model, spec = cli.resolve_model(ollama, cfg["model"])
        line("Model", f"{cfg['model']} → {model}", True)
        if is_cloud(model):
            line(
                "",
                "runs in Ollama's cloud: what lcode sends goes to ollama.com, billed per token, and this Ollama "
                f"must be signed in once: {signin_command(ollama.host)}",
                None,
            )
        ctx, note = cli.choose_context(ollama, model, spec, cfg["context"], hw)
        if spec:
            from lcode.subagents import parallel_slots

            slots = (parallel_slots(spec, ctx, hw), format_tokens(ctx))
        detail = f"{format_tokens(ctx)} tokens"
        if spec:
            detail += f" · ~{spec.memory_gib(ctx):.0f} GB needed, ~{hw.budget_gib:.0f} GB available"
        line("Context", detail + (f" ({note})" if note else ""), None if note else True)
        fit = residency.ram_fit(ollama, spec, ctx, hw) if spec else None
        if fit:
            line("RAM", fit.describe(), None if fit.short else True)
            if fit.short:
                line("", f"{fit.why()} {fit.advice()}", None)
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
        if learned := limits.get(model):
            line(
                "Limit",
                f"{format_tokens(learned)} for {model}: larger contexts ran out of GPU memory "
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
    except cli.NotInstalled as e:
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
        assert install is not None  # found, or this returned above
        where = f"{install.path} (lcode starts it in image mode)"
    _, missing = catalog.chosen_files(model, catalog.lister(server if stats else None, install))
    if missing:
        size = sum(f.gb for f in missing)
        return f"{where} · {model.name} not installed: lcode media install {model.key} ({size:.1f} GB)", None
    return f"{where} · image model: {model.name}", True
