"""The model, mode and context-window commands: /model, /models, /mode, /context and the context breakdown."""

from __future__ import annotations

from typing import cast

from rich.markup import escape
from rich.table import Table

from lcode import (
    backends,
    catalog,
    limits,
    media,
    modes,
    residency,
)
from lcode import context as context_tools
from lcode.agent import Agent
from lcode.agent_context import AUTO_COMPACT_RATIO
from lcode.catalog import MIN_USEFUL_CONTEXT
from lcode.cloud import is_cloud
from lcode.cloud import notice as cloud_notice
from lcode.config import PERMISSION_MODES, ConfigError, format_tokens, parse_context
from lcode.hardware import Hardware
from lcode.ollama import OllamaError

CONTEXT_CHOICES = (16384, 32768, 65536, 131072, 262144, 524288, 1048576)


def model_limit(agent: Agent) -> int | None:
    """The largest context the model supports (what Ollama reports, else the catalog's), if known."""
    spec = catalog.find(agent.settings.model)
    try:
        return agent.ollama.max_context(agent.settings.model) or (spec.max_context if spec else None)
    except OllamaError:
        return spec.max_context if spec else None


def context_options(agent: Agent, hardware: Hardware) -> list[dict]:
    """The context sizes this model supports, with how each one fits this machine."""
    s = agent.settings
    spec = catalog.find(s.model)
    max_ctx = model_limit(agent)
    sizes = {c for c in CONTEXT_CHOICES if not max_ctx or c <= max_ctx} | {s.context}
    if max_ctx:
        sizes.add(max_ctx)
    learned = limits.get(s.model)
    fits = spec.fit(hardware)[0] if spec else None
    recommended = limits.cap(s.model, fits) if fits else None
    options = []
    for size in sorted(sizes):
        if spec:
            memory = spec.memory_gib(size)
            if hardware.unified:
                fit = "fits" if memory <= hardware.budget_gib - catalog.HEADROOM_GIB else "too large"
            elif hardware.gpu and memory <= hardware.vram_gib:
                fit = "on GPU"
            elif memory <= hardware.budget_gib - catalog.HEADROOM_GIB:
                fit = "GPU + RAM" if spec.moe else "GPU + RAM, slow"
            else:
                fit = "too large"
            note = f"~{memory:.0f} GB · {fit}"
        else:
            note = "no memory estimate for this model"
        if learned and size > learned:
            note += " · ran out of memory here before"
        options.append({"size": size, "note": note, "current": size == s.context, "recommended": size == recommended})
    return options


def context_command(agent: Agent, arg: str, hardware: Hardware) -> None:
    """Show context usage and change the window size, from a list or directly (/context 128k)."""
    c, s = agent.console, agent.settings
    c.print(
        f"In use: {format_tokens(agent.ctx_used)} of {format_tokens(s.context)} tokens "
        f"({100 * agent.ctx_used / s.context:.0f}%) in {len(agent.messages)} message"
        f"{'' if len(agent.messages) == 1 else 's'}; makes room at {format_tokens(agent.compact_limit())} "
        + ("(compact_at)." if agent.compact_limit() < AUTO_COMPACT_RATIO * s.context else "(85%).")
    )
    if agent.mcp and agent.mcp.ready():
        how = "found on demand" if agent.mcp.searching(s.context) else "sent with every request"
        c.print(f"MCP tools: ~{format_tokens(agent.mcp.cost())} tokens of definitions, {how}.")
    if not arg:
        print_breakdown(agent)
    if not arg and backends.is_openai(agent.ollama):
        c.print(
            f"[dim]{agent.ollama.name} sets the context window when it loads the model; lcode uses "
            f"{format_tokens(s.context)} of it. To summarize sooner: /ctx 16k[/]"
        )
        return
    if arg:
        try:
            apply_context(agent, parse_context(arg), hardware)
        except ConfigError as e:
            c.print(f"[red]{e}[/]")
        return
    options = context_options(agent, hardware)
    table = Table(title=f"Context window for {s.model}", title_justify="left", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Memory · fit")
    table.add_column("")
    for i, option in enumerate(options, 1):
        marks = []
        if option["current"]:
            marks.append("[bold]current[/]")
        if option["recommended"]:
            marks.append("[cyan]recommended[/]")
        table.add_row(str(i), format_tokens(option["size"]), option["note"], ", ".join(marks))
    c.print(table)
    limit = model_limit(agent)
    beyond = [size for size in CONTEXT_CHOICES if limit and size > limit]
    if beyond and limit:
        sizes = " and ".join(format_tokens(size) for size in beyond[:2])
        c.print(
            f"[dim]Not offered: {sizes}{' and more' if len(beyond) > 2 else ''}. {s.model} was trained on up to "
            f"{format_tokens(limit)} tokens, and Ollama caps the window there whatever the memory, so a larger "
            "setting would only look larger.[/]"
        )
    try:
        answer = input(f"  Choose a number, a size like 96k, or press Enter to keep {format_tokens(s.context)}: ")
    except EOFError:
        return
    answer = answer.strip()
    if not answer:
        c.print(f"Keeping {format_tokens(s.context)}.")
        return
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        size = options[int(answer) - 1]["size"]
    else:
        try:
            size = parse_context(answer)
        except ConfigError as e:
            c.print(f"[red]{e}[/]")
            return
    apply_context(agent, size, hardware)


def print_breakdown(agent: Agent) -> None:
    """What uses the context, by category."""
    parts = [p for p in context_tools.breakdown(agent) if p.tokens]
    total = sum(p.tokens for p in parts) or 1
    table = Table(title="What uses the context (estimates)", title_justify="left", header_style="bold")
    table.add_column("Part")
    table.add_column("Tokens", justify="right")
    table.add_column("", no_wrap=True)
    table.add_column("", style="dim")
    for part in parts:
        share = part.tokens / total
        bar = "█" * max(1, round(share * 20)) + f" {share:.0%}"
        table.add_row(escape(part.name), format_tokens(part.tokens), bar, escape(part.detail))
    agent.console.print(table)
    if agent.pruned or agent.compacted:
        agent.console.print(
            f"[dim]This session: old tool output removed {agent.pruned} time(s), conversation summarized "
            f"{agent.compacted} time(s).[/]"
        )


def mode_command(agent: Agent, arg: str) -> None:
    """/mode: show the mode, or switch to code, image or video (handing the GPU over)."""
    c = agent.console
    if not arg:
        c.print(
            f"Mode: [bold]{agent.settings.mode}[/] ({modes.TITLES[agent.settings.mode]}). Switch with /mode code, "
            "/mode image or /mode video. The permission mode is /permissions."
        )
        return
    if arg in PERMISSION_MODES:  # what /mode used to set
        agent.perms.mode = arg
        c.print(f"Permission mode: {arg}. [dim](/permissions sets it now; /mode switches code, image and video.)[/]")
        return
    if arg not in modes.MODES:
        c.print(f"[red]Unknown mode {escape(arg)!r}: choose code, image or video.[/]")
        return
    lines = modes.switch(agent, arg)
    c.print(f"[green]{escape(lines[0])}[/]")
    for line in lines[1:]:
        c.print(f"[dim]{escape(line)}[/]")
    if arg == "code":
        from lcode import repl  # it imports this module; looked up here, where tests can stand in for it

        repl.preload(agent)  # the coding model loads in the background, as at the start


def model_command(agent: Agent, arg: str, hardware: Hardware) -> None:
    """/models and /model: the numbered list to pick from, or straight to a number or a name (/model 2)."""
    from lcode.cli import model_choices, print_models

    c, s = agent.console, agent.settings
    if arg and not arg.isdigit():
        switch_model(agent, arg, hardware)
        return
    if arg:
        choices = model_choices(agent.ollama)
    else:
        choices = print_models(agent.ollama, hardware, s.model, numbered=True, out=c)
        if not choices:
            return
        try:
            answer = input(f"  Switch to which model? (number or name, Enter to keep {s.model}): ").strip()
        except EOFError:
            return
        if not answer:
            c.print(f"Keeping {s.model}.")
            return
        arg = answer
    if arg.isdigit():
        if not 1 <= int(arg) <= len(choices):
            c.print(f"[red]There's no model {arg} in the list: choose 1 to {len(choices)}.[/]")
            return
        arg = choices[int(arg) - 1]
    switch_model(agent, arg, hardware)


def media_model_command(agent: Agent, arg: str, hardware: Hardware) -> None:
    """/models and /model in image or video mode: the catalog's models for the mode, numbered; pick one."""
    from lcode import media_catalog as catalog

    c, s = agent.console, agent.settings
    choices = catalog.load().for_mode(s.mode)
    if not choices:
        c.print(f"[yellow]{escape(media.SOON.get(s.mode, modes.ready(agent)))}[/]")
        return
    current = media.current(agent)
    installed = media.installed_lister(agent)
    if not arg:
        for i, m in enumerate(choices, 1):
            mark = "[green]●[/]" if m.key == current.key else " "
            c.print(
                f"  {i}. {mark} [bold]{escape(m.name)}[/] [dim]({m.key})[/]  "
                f"{escape(catalog.describe(m, hardware, installed))}"
            )
            if m.measured:
                c.print(f"        [dim]{escape(m.measured)}[/]")
        try:
            arg = input(f"  Switch to which model? (number or name, Enter to keep {current.name}): ").strip()
        except EOFError:
            return
        if not arg:
            return
    model = catalog.load().find(arg, s.mode)
    if model is None:
        c.print(f"[red]There's no {s.mode} model {escape(arg)!r}: choose 1 to {len(choices)}, or a name.[/]")
        return
    setattr(s, f"{s.mode}_model", model.key)
    c.print(f"[green]{s.mode.capitalize()} model: {escape(model.name)}.[/]")
    if model.cloud:
        from lcode import media_cloud

        agent.say(media_cloud.ready_note(model))
        return
    if installed is not None:
        _, missing = catalog.chosen_files(model, installed)
        if missing:
            size = sum(f.gb for f in missing)
            c.print(f"[dim]Not installed yet: {size:.1f} GB download with your next {s.mode} (you're asked first).[/]")


def switch_model(agent: Agent, wanted: str, hardware: Hardware) -> None:
    """Switch to a catalog key or an installed model, and size the context for it."""
    from lcode.cli import NotInstalled, resolve_model

    c, s = agent.console, agent.settings
    try:
        name, spec = resolve_model(agent.ollama, wanted)
    except (NotInstalled, OllamaError) as e:
        c.print(f"[red]{e}[/]")
        return
    if name == s.model:
        c.print(f"Already using {name} (context {format_tokens(s.context)}).")
        return
    s.model = name
    s.num_batch = spec.num_batch if spec and name == spec.local_name else None
    if spec:  # size the context for the new model: largest window that fits this machine
        s.context = limits.cap(name, spec.fit(hardware)[0] or MIN_USEFUL_CONTEXT)
    elif backends.is_openai(agent.ollama):
        s.context = backends.context_for(cast(backends.OpenAICompatible, agent.ollama), name, None)[0]  # its window
    agent._vision = False  # the model that looks at images goes with the model: decide again
    agent.messages[0]["content"] = agent.system_prompt()
    c.print(f"[green]Switched to {name}[/] (context {format_tokens(s.context)}).")
    if is_cloud(name):
        agent.say(cloud_notice(name))
    warning = residency.dense_warning(agent.ollama, name, s.context, hardware)
    if warning:
        agent.say(warning, warning=True)


def apply_context(agent: Agent, size: int, hardware: Hardware) -> None:
    c, s = agent.console, agent.settings
    if size == s.context:
        c.print(f"The context window is already {format_tokens(size)}.")
        return
    if agent.has_conversation() and agent.ctx_used > AUTO_COMPACT_RATIO * size:
        c.print(
            f"This conversation uses {format_tokens(agent.ctx_used)} tokens, too much for {format_tokens(size)}; "
            "summarizing it first."
        )
        try:
            agent.compact()
        except OllamaError as e:
            c.print(f"[red]Couldn't summarize the conversation, so the context stays the same: {e}[/]")
            return
    c.print(agent.set_context(size))
    spec = catalog.find(s.model)
    if spec and spec.memory_gib(s.context) > hardware.budget_gib:
        c.print(
            f"[yellow]~{spec.memory_gib(s.context):.0f} GB needed but ~{hardware.budget_gib:.0f} GB available; "
            "this may run out of memory, in which case lcode falls back to a smaller size.[/]"
        )
