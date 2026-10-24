"""Films: from one description to a short film with the same characters in every shot.

1. **A storyboard.** The prompt assistant (lcode.assistant) writes it: a title, the look of the whole film, the cast
   (each living character described once; things and places are described in the shots), and the shots (what
   happens, how long, the camera, who's in it). It's saved as `storyboard.json` in the film's folder, so it can be
   edited and made again.
2. **The cast.** Each character gets a reference picture (FLUX.2 klein), made once and kept in `cast/`.
3. **The shots.** Each shot starts as a keyframe: a picture of the shot made with the cast's pictures as references,
   so the characters look the same from shot to shot. Video mode's model then animates the keyframe.
4. **The film.** The shots are joined with cuts or fades (ffmpeg) into `film.mp4`, and a sound track can be added.

Any shot can be made again alone (`/film shot 2`), and the film joined again (`/film stitch`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from lcode import assistant, comfy, imageopts, media

if TYPE_CHECKING:
    from lcode.agent import Agent

MAX_SHOTS = 8
STORYBOARD = (
    "You write storyboards for short AI-generated films. From the user's idea, write a film of {shots} shots, each "
    "about {seconds} seconds. Answer with JSON only, in English:\n"
    '{{"title": "a short title", "look": "the visual style of the whole film in one sentence (medium, light, '
    'colour, lens)", "cast": [{{"name": "a one-word name", "description": "what the character looks like: '
    'species or age, build, clothes, colours, distinctive features, in one sentence"}}], "shots": [{{"description": '
    '"what happens in this shot, where, and who is in it, in one or two sentences, using the cast names", "camera": '
    '"a shot type and camera move, like slow zoom in, tracking shot, static wide shot", "cast": ["names in this '
    'shot"], "seconds": {seconds}}}]}}\n'
    "The cast is one to three living characters (people, animals, creatures), with the same names in every shot. "
    "Things and places are never cast members: a kite, a car, a house or a forest is described in the shots. Each "
    "shot is one continuous moment; the film tells a small story from the first shot to the last. Describe what is "
    "seen, never what is felt or heard."
)
TRANSITIONS = ("cut", "fade")
FADE = 0.5  # seconds a fade takes


class FilmError(Exception):
    pass


@dataclass
class Shot:
    description: str
    camera: str = "static shot"
    cast: list[str] = field(default_factory=list)
    seconds: float = 4.0


@dataclass
class Board:
    title: str
    look: str
    cast: dict[str, str]  # name -> description
    shots: list[Shot]
    idea: str = ""
    transition: str = "cut"
    sound: str = ""  # a sound track for the whole film, made after it's joined
    quality: str = "fast"

    def to_json(self) -> dict:
        data = asdict(self)
        data["cast"] = [{"name": n, "description": d} for n, d in self.cast.items()]
        return data

    @classmethod
    def from_json(cls, data: dict) -> Board:
        cast = {}
        for member in data.get("cast") or []:
            if isinstance(member, dict) and str(member.get("name") or "").strip():
                cast[str(member["name"]).strip()] = str(member.get("description") or "").strip()
        shots = []
        for raw in data.get("shots") or []:
            if not isinstance(raw, dict) or not str(raw.get("description") or "").strip():
                continue
            seconds = raw.get("seconds")
            shots.append(
                Shot(
                    str(raw["description"]).strip(),
                    str(raw.get("camera") or "static shot").strip(),
                    [str(n).strip() for n in raw.get("cast") or [] if str(n).strip() in cast],
                    float(seconds) if isinstance(seconds, (int, float)) and 1 <= seconds <= 8 else 4.0,
                )
            )
        if not shots:
            raise FilmError("the storyboard has no shots")
        return cls(
            str(data.get("title") or "Untitled").strip(),
            str(data.get("look") or "").strip(),
            cast,
            shots[:MAX_SHOTS],
            str(data.get("idea") or ""),
            data.get("transition") if data.get("transition") in TRANSITIONS else "cut",
            str(data.get("sound") or ""),
            data.get("quality") if data.get("quality") in imageopts.QUALITIES else "fast",
        )


# ----------------------------------------------------------------------------- the storyboard


def films_folder(agent: Agent) -> Path:
    return media.folder(agent) / "films"


def plan(agent: Agent, idea: str, shots: int = 3, seconds: float = 4.0) -> tuple[Board, Path]:
    """The assistant's storyboard for an idea, saved in a new film folder."""
    model = assistant.pick(agent.ollama, agent.settings.prompt_assistant if agent.settings.prompt_assistant
                           not in assistant.SCOPES else "auto")  # fmt: skip
    if model is None:
        raise FilmError("Films need the prompt assistant to write the storyboard: ollama pull qwen3.5:4b")
    helper = assistant.Assistant(agent.ollama, model)
    system = STORYBOARD.format(shots=shots, seconds=f"{seconds:g}")
    with agent.console.status(f"The prompt assistant ({model}) is writing the storyboard…", spinner="dots"):
        try:
            data = assistant.parse_json(helper.write(system, idea, json_answer=True))
        except assistant.AssistantError as e:
            raise FilmError(str(e)) from e
    data["idea"] = idea
    board = Board.from_json(data)
    folder = new_folder(agent, board.title)
    save(board, folder)
    return board, folder


def new_folder(agent: Agent, title: str) -> Path:
    base = films_folder(agent) / comfy.slug(title, 30)
    folder, n = base, 2
    while folder.exists():
        folder, n = base.with_name(f"{base.name}-{n}"), n + 1
    folder.mkdir(parents=True)
    return folder


def save(board: Board, folder: Path) -> None:
    (folder / "storyboard.json").write_text(json.dumps(board.to_json(), indent=2, ensure_ascii=False) + "\n")
    lines = [f"# {board.title}", "", f"*{board.look}*", "", "## Cast", ""]
    lines += [f"- **{name}:** {desc}" for name, desc in board.cast.items()]
    lines += ["", "## Shots", ""]
    for n, shot in enumerate(board.shots, 1):
        who = f" ({', '.join(shot.cast)})" if shot.cast else ""
        lines.append(f"{n}. {shot.description}{who} — {shot.camera}, {shot.seconds:g} s")
    (folder / "storyboard.md").write_text("\n".join(lines) + "\n")


def load(folder: Path) -> Board:
    try:
        return Board.from_json(json.loads((folder / "storyboard.json").read_text()))
    except (OSError, ValueError) as e:
        raise FilmError(f"can't read {folder / 'storyboard.json'}: {e}") from e


def latest(agent: Agent) -> Path:
    folders = sorted((p for p in films_folder(agent).glob("*") if (p / "storyboard.json").exists()),
                     key=lambda p: p.stat().st_mtime)  # fmt: skip
    if not folders:
        raise FilmError("No film here yet: /film <your idea>")
    return folders[-1]


def find(agent: Agent, name: str) -> Path:
    if not name:
        return latest(agent)
    folder = films_folder(agent) / name
    if not (folder / "storyboard.json").exists():
        raise FilmError(f"There's no film {name!r}: /film list")
    return folder


def describe(board: Board, folder: Path, agent: Agent) -> str:
    lines = [f"{board.title}  ({media.shown(agent, folder)})", f"  look: {board.look}"]
    lines += [f"  cast: {name} — {desc}" for name, desc in board.cast.items()]
    for n, shot in enumerate(board.shots, 1):
        who = f" [{', '.join(shot.cast)}]" if shot.cast else ""
        lines.append(f"  {n}. {shot.description}{who} — {shot.camera}, {shot.seconds:g} s")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- making it


def make(
    agent: Agent,
    board: Board,
    folder: Path,
    only: list[int] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
) -> Path:
    """Make the cast's pictures (once), the shots (all, or `only` those numbers), and join the film."""
    say = agent.say
    for name, desc in board.cast.items():
        picture = folder / "cast" / f"{comfy.slug(name, 20)}.png"
        if picture.exists():
            continue
        say(f"Cast: {name}")
        prompt = (
            f"{board.look} Character reference of {name}, alone: {desc}. Full body, facing the camera, plain "
            "background."
        )
        made = with_mode(agent, "image", lambda p=prompt: media.generate_image(
            agent, p, cancelled, ask, imageopts.Options(size="3:4", quality=board.quality)))  # fmt: skip
        keep(made.files[0], picture)
    for n, shot in enumerate(board.shots, 1):
        if only and n not in only:
            continue
        say(f"Shot {n} of {len(board.shots)}: {shot.description}")
        keyframe = folder / "keyframes" / f"{n:02d}.png"
        refs = [folder / "cast" / f"{comfy.slug(name, 20)}.png" for name in shot.cast]
        if refs:
            names = "; ".join(f"image {k} is {name}" for k, name in enumerate(shot.cast, 1))
            prompt = f"{board.look} {shot.description} ({names}). A film still, wide 16:9 frame."
            options = imageopts.Options(size="16:9", quality=board.quality)
            job = {"kind": "edit", "prompt": prompt, "options": options, "sources": [str(p) for p in refs],
                   "mask": None, "pads": None, "keep_shape": False, "raw": True}  # fmt: skip
            made = with_mode(agent, "image", lambda j=job: media.edit_image(agent, j, j["options"], cancelled, ask))
        else:
            prompt = f"{board.look} {shot.description}. A film still, wide 16:9 frame."
            made = with_mode(agent, "image", lambda p=prompt: media.generate_image(
                agent, p, cancelled, ask, imageopts.Options(size="16:9", quality=board.quality)))  # fmt: skip
        keep(made.files[0], keyframe)
        options = imageopts.Options(seconds=shot.seconds, quality=board.quality)
        job = {"kind": "video", "prompt": f"{shot.description} {shot.camera}. {board.look}", "options": options,
               "sources": [str(keyframe)], "mask": None, "pads": None, "keep_shape": True}  # fmt: skip
        [clip] = with_mode(agent, "video", lambda j=job: media.redo(agent, j, cancelled, ask))
        keep(clip.files[0], folder / "shots" / f"{n:02d}.mp4")
    return stitch(agent, board, folder, cancelled, ask)


def with_mode(agent: Agent, mode: str, run):
    """Run one step in image or video mode (the right model for it), then go back."""
    before = agent.settings.mode
    agent.settings.mode = mode
    try:
        return run()
    finally:
        agent.settings.mode = before


def keep(made: Path, where: Path) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(made, where)


def stitch(
    agent: Agent,
    board: Board,
    folder: Path,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
) -> Path:
    """Join the shots into film.mp4 (cuts or fades), and add the sound track when the storyboard has one."""
    shots = [folder / "shots" / f"{n:02d}.mp4" for n in range(1, len(board.shots) + 1)]
    missing = [p.name for p in shots if not p.exists()]
    if missing:
        raise FilmError(f"Shots not made yet: {', '.join(missing)} (/film make)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FilmError("Joining the shots needs ffmpeg: install it (apt install ffmpeg, brew install ffmpeg)")
    film = folder / "film.mp4"
    command = join_command(ffmpeg, shots, film, board.transition, [media.clip_info(p)["seconds"] for p in shots])
    with agent.console.status("Joining the shots…", spinner="dots"):
        done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise FilmError(f"ffmpeg couldn't join the shots: {done.stderr.strip()[-400:]}")
    if board.sound:
        made = with_mode(agent, "video", lambda: media.apply_tool(agent, "sound", film, 1, cancelled, ask, board.sound))
        keep(made.files[0], folder / "film-sound.mp4")
        film = folder / "film-sound.mp4"
    agent.console.print(f"[green]◆ {board.title}:[/] {media.shown(agent, film)}")
    return film


def join_command(ffmpeg: str, shots: list[Path], out: Path, transition: str, lengths: list[float]) -> list[str]:
    """ffmpeg's command for the shots one after another: cut, or each fading into the next (xfade)."""
    command = [ffmpeg, "-y", "-v", "error"]
    for shot in shots:
        command += ["-i", str(shot)]
    count = len(shots)
    if transition == "fade" and count > 1:
        chain, last = [], "[0:v]"
        for k in range(1, count):
            joined = sum(lengths[:k]) - (k - 1) * FADE  # how long the film is so far
            label = f"[v{k}]"
            start = max(0.0, joined - FADE)
            chain.append(f"{last}[{k}:v]xfade=transition=fade:duration={FADE}:offset={start:.3f}{label}")
            last = label
        graph = ";".join(chain)
    else:
        last = "[out]"
        graph = "".join(f"[{k}:v]" for k in range(count)) + f"concat=n={count}:v=1:a=0{last}"
    return [*command, "-filter_complex", graph, "-map", last, "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]  # fmt: skip


def shot_numbers(text: str, count: int) -> list[int]:
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    bad = [n for n in numbers if not 1 <= n <= count]
    if not numbers or bad:
        raise FilmError(f"Say which shot: 1 to {count} (/film shot 2)")
    return numbers


def with_options(board: Board, transition: str = "", sound: str = "", quality: str = "") -> Board:
    return replace(
        board,
        transition=transition if transition in TRANSITIONS else board.transition,
        sound=sound or board.sound,
        quality=quality if quality in imageopts.QUALITIES else board.quality,
    )
