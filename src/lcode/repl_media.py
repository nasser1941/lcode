"""Image and video mode's slash commands: /edit, /vary, /upscale, /film and the rest, and the gallery."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text

from lcode import (
    media,
    modes,
)
from lcode.agent import Agent
from lcode.steer import KeyReader, Steering

IMAGE_COMMANDS = (
    "/options", "/again", "/vary", "/gallery", "/open", "/play", "/edit", "/use", "/extend", "/upscale", "/nobg",
    "/animate", "/smooth", "/sound", "/restyle",
)  # fmt: skip


VIDEO_TOO = (
    "/options", "/again", "/vary", "/gallery", "/open", "/play", "/use", "/extend", "/upscale", "/smooth", "/sound",
    "/restyle",
)  # what video mode has  # fmt: skip


NUMBER = re.compile(r"^#?(\d+)$")


def image_command(agent: Agent, cmd: str, arg: str) -> None:
    """Image mode's commands: /options, follow-ups on results (/again, /vary, /edit, /extend, /upscale, /nobg),
    references (/use), /gallery and /open."""
    from dataclasses import replace

    from lcode import edits, imageopts

    c = agent.console
    video = agent.settings.mode == "video"
    if cmd == "/options":
        if not arg.strip():
            c.print(f"Options: [bold]{escape(agent.image_options.describe(video))}[/]")
            c.print(
                '[dim]Set them with /options n=4 ar=16:9 seed=42 quality=best style=photo no="text"; '
                f"styles: {', '.join(imageopts.STYLES)}. Flags in a request (--n 4) win for that request.[/]"
            )
            return
        try:
            agent.image_options = imageopts.set_values(agent.image_options, imageopts.parse_assignments(arg))
        except imageopts.OptionError as e:
            c.print(f"[red]{escape(str(e))}[/]")
            return
        c.print(f"Options: [bold]{escape(agent.image_options.describe(video))}[/]")
        return
    if cmd == "/gallery":
        gallery(agent, everything=arg.strip() == "all")
        return
    if cmd not in ("/open", "/play") and not (agent.settings.mode == "image" or (video and cmd in VIDEO_TOO)):
        where = "image or video mode" if cmd in VIDEO_TOO else "image mode"
        c.print(f"[yellow]{cmd} works in {where}: /mode {'image' if where == 'image mode' else 'video'}.[/]")
        return
    first, _, rest = arg.strip().partition(" ")
    number = first if NUMBER.match(first) else ""
    rest = rest if number else arg.strip()
    try:
        if cmd == "/use":
            use_references(agent, arg.strip())
            return
        if cmd == "/again":
            if agent.last_request is None:
                c.print("[yellow]Nothing to do again yet: describe an image first.[/]")
                return
            job = dict(agent.last_request)
            _, job["options"] = imageopts.parse(arg, imageopts.replace_seed(job["options"]))
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        entry = media.result(agent, f"#{found.group(1)}" if number and (found := NUMBER.match(number)) else "")
        source = Path(entry["path"])
        if cmd in ("/open", "/play"):
            problem = media.open_file(source)
            c.print(f"[yellow]{escape(problem)}[/]" if problem else f"[dim]Opened #{entry['n']}.[/]")
            return
        if cmd == "/vary":
            job: dict[str, Any] = dict(entry.get("job") or {"kind": "image", "sources": [], "mask": None, "pads": None})
            tools = ("upscale", "remove-background", "interpolate", "sound")
            if entry.get("kind") in tools or job["kind"] not in ("image", "edit", "video"):
                c.print(f"[yellow]#{entry['n']} is {escape(entry['prompt'])}: /vary the image it came from.[/]")
                return
            job["prompt"] = entry["prompt"] if job["kind"] == "image" else job.get("prompt", entry["prompt"])
            options = imageopts.replace_seed(imageopts.Options.from_json(entry.get("options") or {}), n=4)
            _, job["options"] = imageopts.parse(rest, options)
            c.print(f"[dim]Variations of #{entry['n']}: {escape(entry['prompt'])}[/]")
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/animate":
            if not edits.is_image(source):
                c.print(f"[yellow]#{entry['n']} isn't a picture: /animate takes an image.[/]")
                return
            for line in modes.switch(agent, "video"):
                c.print(f"[dim]{escape(line)}[/]")
            text, options = imageopts.parse(rest, imageopts.replace_seed(agent.image_options, n=1))
            job = {"kind": "video", "prompt": text or entry["prompt"], "options": options, "sources": [str(source)],
                   "mask": None, "pads": None, "keep_shape": "--ar" not in rest and "--size" not in rest}  # fmt: skip
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/edit":
            text, refs = edits.split_references(rest)
            instruction, options = imageopts.parse(text, imageopts.replace_seed(agent.image_options))
            if not instruction:
                c.print("[yellow]Say what to change: /edit 3 make the sky purple[/]")
                return
            sources = [str(source)] + [str(media.resolve(agent, r)) for r in refs]
            job = {"kind": "edit", "prompt": instruction, "options": options, "sources": sources, "mask": None,
                   "pads": None, "keep_shape": "--ar" not in rest and "--size" not in rest}  # fmt: skip
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/extend" and video:
            if edits.is_image(source):
                c.print(f"[yellow]#{entry['n']} is a picture: /animate it, or /extend a clip.[/]")
                return
            text, options = imageopts.parse(rest, imageopts.replace_seed(agent.image_options, n=1))
            job = {"kind": "video", "prompt": text or entry["prompt"], "options": options, "sources": [],
                   "mask": None, "pads": None, "keep_shape": True, "extend": str(source),
                   "size": [entry["width"], entry["height"]]}  # fmt: skip
            c.print(f"[dim]Continuing #{entry['n']} from its last frame, {options.seconds:g} s more.[/]")
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/extend":
            width, height = edits.image_size(source)
            pads, text = edits.parse_extend(rest, width, height)
            text, options = imageopts.parse(text, imageopts.replace_seed(agent.image_options, n=1))
            job = {"kind": "edit", "prompt": text, "options": replace(options, size="1:1"), "sources": [str(source)],
                   "mask": None, "pads": pads, "keep_shape": True}  # fmt: skip
            grown = f"{width + pads[0] + pads[2]}×{height + pads[1] + pads[3]}"
            c.print(f"[dim]Extending #{entry['n']} from {width}×{height} to {grown} before scaling.[/]")
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/upscale":
            scale = rest.strip().lower().rstrip("x×") or "2"
            if scale not in ("2", "4"):
                c.print("[yellow]/upscale makes 2 or 4 times the size: /upscale 3 4[/]")
                return
            make_image(
                agent, run=lambda cancel, ask: media.apply_tool(agent, "upscale", source, int(scale), cancel, ask)
            )
            return
        if cmd == "/smooth":
            times = int(rest.strip().lower().rstrip("x×") or "2")
            if times not in (2, 4):
                c.print("[yellow]/smooth makes 2 or 4 times the frames: /smooth 4 2[/]")
                return
            make_image(
                agent, run=lambda cancel, ask: media.apply_tool(agent, "interpolate", source, times, cancel, ask)
            )
            return
        if cmd == "/sound":
            make_image(agent, run=lambda cancel, ask: media.apply_tool(agent, "sound", source, 1, cancel, ask, rest))
            return
        if cmd == "/restyle":
            if not media.is_video(source):
                c.print("[yellow]/restyle works on clips: /restyle 4 as a watercolor painting.[/]")
                return
            text, options = imageopts.parse(rest, imageopts.replace_seed(agent.image_options, n=1))
            if not text:
                c.print("[yellow]Say the new look: /restyle 4 as a watercolor painting[/]")
                return
            job = {"kind": "video", "prompt": text, "options": options, "sources": [], "mask": None, "pads": None,
                   "keep_shape": True, "restyle": str(source), "seconds_given": "--seconds" in rest}  # fmt: skip
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/nobg":
            make_image(
                agent, run=lambda cancel, ask: media.apply_tool(agent, "remove-background", source, 2, cancel, ask)
            )
            return
    except (media.MediaError, edits.EditError, imageopts.OptionError) as e:
        c.print(f"[yellow]{escape(str(e))}[/]")


def use_references(agent: Agent, arg: str) -> None:
    """/use 3 @photo.png: images every next request follows (as references) until /use off."""
    c = agent.console
    if arg in ("off", "none", "clear"):
        agent.image_refs = []
        c.print("[dim]No reference images now.[/]")
        return
    if arg:
        refs = [r.lstrip("@") for r in arg.split()]
        paths = [media.resolve(agent, f"#{found.group(1)}" if (found := NUMBER.match(r)) else r) for r in refs]
        agent.image_refs = [str(p) for p in paths]
    if not agent.image_refs:
        c.print("[dim]No reference images. /use 3 (or /use photo.png) makes the next requests follow one.[/]")
        return
    shown = ", ".join(media.shown(agent, Path(p)) for p in agent.image_refs)
    c.print(f"References for the next requests: [bold]{escape(shown)}[/] [dim](/use off stops)[/]")


FILM_FLAGS = re.compile(r'--(shots|seconds|quality|transition|sound)\s+("[^"]*"|\S+)')


def film_command(agent: Agent, arg: str) -> None:
    """/film: a storyboard from an idea, the film made from it, a shot made again, the shots joined again."""
    from lcode import film

    c = agent.console
    flags = {k: v.strip('"') for k, v in FILM_FLAGS.findall(arg)}
    text = " ".join(FILM_FLAGS.sub(" ", arg).split())
    word, _, rest = text.partition(" ")
    try:
        if word == "list":
            folders = sorted(p for p in film.films_folder(agent).glob("*") if (p / "storyboard.json").exists())
            if not folders:
                c.print("[dim]No films here yet: /film <your idea>[/]")
            for folder in folders:
                board = film.load(folder)
                made = "made" if (folder / "film.mp4").exists() else "storyboard only"
                about = f"{len(board.shots)} shots · {made}"
                c.print(f"  [bold]{escape(folder.name)}[/] {escape(board.title)} [dim]· {about}[/]")
            return
        if word in ("make", "stitch", "shot", "show"):
            numbers_text = ""
            if word == "shot":
                numbers_text, _, name = rest.partition(" ")
                folder = film.find(agent, name.strip())
            else:
                folder = film.find(agent, rest.strip())
            board = film.with_options(film.load(folder), flags.get("transition", ""), flags.get("sound", ""),
                                      flags.get("quality", ""))  # fmt: skip
            if word == "show":
                c.print(escape(film.describe(board, folder, agent)))
                return
            film.save(board, folder)
            if word == "stitch":
                film.stitch(agent, board, folder)
                return
            only = film.shot_numbers(numbers_text, len(board.shots)) if word == "shot" else None
            make_image(agent, run=lambda cancel, ask: film.make(agent, board, folder, only, cancel, ask))
            return
        just_plan = word == "plan"
        idea = rest if just_plan else text
        if not idea.strip():
            c.print("[yellow]Say what the film is about: /film a fox finds a lost kitten in the snow --shots 3[/]")
            return
        shots = int(flags.get("shots", "3"))
        if not 1 <= shots <= film.MAX_SHOTS:
            c.print(f"[yellow]A film has 1 to {film.MAX_SHOTS} shots.[/]")
            return
        seconds = float(flags.get("seconds", "4"))
        board, folder = film.plan(agent, idea, shots, min(max(seconds, 1.0), 8.0))
        board = film.with_options(board, flags.get("transition", ""), flags.get("sound", ""), flags.get("quality", ""))
        film.save(board, folder)
        c.print(escape(film.describe(board, folder, agent)))
        if just_plan:
            c.print(
                f"[dim]Edit {escape(media.shown(agent, folder / 'storyboard.json'))} if you like, then /film make.[/]"
            )
            return
        make_image(agent, run=lambda cancel, ask: film.make(agent, board, folder, None, cancel, ask))
    except (film.FilmError, ValueError) as e:
        c.print(f"[yellow]{escape(str(e))}[/]")


def gallery(agent: Agent, everything: bool = False) -> None:
    """The session's images (or every image in the media folder), with what made them."""
    import json as json_module

    c = agent.console
    if everything:
        folder = media.folder(agent)
        entries = []
        for sidecar in sorted(folder.glob("*.json"), reverse=True)[:50]:
            try:
                meta = json_module.loads(sidecar.read_text())
            except (OSError, ValueError):
                continue
            entries.append({"path": str(sidecar.with_name(meta.get("file", sidecar.stem + ".png"))), **meta})
        if not entries:
            c.print(f"[dim]No images in {media.shown(agent, folder)} yet.[/]")
            return
        c.print(f"[bold]{len(entries)} latest image(s) in {escape(media.shown(agent, folder))}[/]")
    else:
        entries = agent.media_results
        if not entries:
            c.print("[dim]No images in this session yet: describe one. /gallery all lists every image here.[/]")
            return
    for entry in entries:
        number = f"#{entry['n']:<3}" if "n" in entry else "    "
        c.print(
            f"  [bold green]{number}[/] {escape(media.shown(agent, Path(entry['path'])))}\n"
            f"       [dim]{escape(entry.get('prompt', ''))} · {entry.get('width')}×{entry.get('height')} · "
            f"{escape(str(entry.get('model', '')))} · seed {entry.get('seed')}[/]"
        )


def make_image(agent: Agent, prompt: str = "", options=None, run=None) -> None:
    """Image mode: ComfyUI makes the images (a request, or `run(cancelled, ask)`); Esc or Ctrl+C cancels them
    there too."""
    steering = Steering()
    reader = KeyReader(steering)
    reading = agent.interactive and reader.start()  # Esc sets steering.pause: here it cancels

    def ask(question: str) -> bool:
        answer = steering.ask(f"  {question} [y/N] ") if reading else input(f"  {question} [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    started = time.monotonic()
    try:
        asking = ask if agent.interactive else None
        if run is not None:
            run(lambda: steering.pause, asking)
        elif options is None:
            media.request(agent, prompt, cancelled=lambda: steering.pause, ask=asking)
        else:
            media.make(agent, prompt, options, cancelled=lambda: steering.pause, ask=asking)
    except media.MediaError as e:
        agent.console.print(f"[yellow]{escape(str(e))}[/]")
    except KeyboardInterrupt:  # while ComfyUI was starting: nothing queued yet
        agent.console.print("[yellow]⏹ Cancelled.[/]")
    except EOFError:
        pass
    finally:
        if reading:
            reader.stop()
        typed = steering.leftover()  # the next prompt, typed while this one was being made
        if typed:
            agent.unsent = typed
            agent.console.print(Text("✎ What you typed is in the prompt below.", style="cyan"))
        agent.save()  # the gallery and options carry over when the session is resumed
        agent.finished(prompt or "image or video", time.monotonic() - started)  # a long render: notify
