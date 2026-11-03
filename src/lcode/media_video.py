"""Video mode's jobs: a clip from a description or pictures, and long clips made in parts and joined."""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text

from lcode import comfy, edits, imageopts, media_cloud
from lcode import media_catalog as catalog
from lcode.clips import clip_info, mp4_seconds
from lcode.media import (
    Made,
    MediaError,
    Plan,
    add_thumbnail,
    current,
    describe_job,
    ensure_installed,
    folder,
    prepare,
    record,
    run_plan,
    show,
    shown,
)

if TYPE_CHECKING:
    from lcode.agent import Agent


def generate_video(
    agent: Agent,
    job: dict,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
    label: str = "",
    part: tuple[Path, str] | None = None,
) -> Made:
    """One video: from the prompt, or starting from the job's image. Its shape is 16:9, or 9:16 for an upright start
    image (the shapes video models know best; the image is cropped to it), unless the request gives one. A part of a
    long clip (`part`: its folder and name) is saved there, without a number in the gallery."""
    prompt = job["prompt"].strip()
    server = prepare(agent, ask, cancelled)
    sources = [Path(p) for p in job.get("sources") or []]
    start = sources[0] if sources else None
    end = sources[1] if len(sources) > 1 else None
    clip = Path(job["extend"]) if job.get("extend") else None
    refs = [Path(p) for p in job.get("refs") or []]
    restyle = Path(job["restyle"]) if job.get("restyle") else None
    task = (
        "restyle" if restyle else "references" if refs else "first-last-frame" if end
        else "camera" if options.camera else "extend" if clip else ""
    )  # fmt: skip
    model = video_model(agent, task, quiet=part is not None)
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
    seed = options.seed if options.seed is not None else random.randrange(2**32)
    steps, megapixels = model.preset(options.quality)
    ratio = options.size
    if job.get("keep_shape", True) and options.size == imageopts.Options().size:  # no shape asked for
        ratio = "16:9"
        if start is not None:
            try:
                w, h = edits.image_size(start)
                ratio = "9:16" if h > w else "16:9"  # a square or odd shape breaks up; this crops it
            except (OSError, edits.EditError):
                pass
    width, height = imageopts.dimensions(imageopts.SIZES.get(ratio, ratio), megapixels, multiple=32)
    if clip is not None:  # the new part continues the clip, so it has its size
        width, height = job["size"]
    fps = int(workflow.defaults.get("fps", 24))
    if clip is not None:  # one clip, one pace: the new part plays at the clip's fps
        fps = int(clip_info(clip)["fps"] or fps)
    if restyle is not None:  # the restyled clip has the clip's size and pace (up to the model's length)
        info = clip_info(restyle)
        width, height = (info["width"] // 16 * 16 or 832), (info["height"] // 16 * 16 or 480)
        fps = int(info["fps"] or fps)
        options = replace(options, seconds=min(options.seconds if job.get("seconds_given") else 99,
                                               (min(info["frames"] or 81, 81) - 1) / fps))  # fmt: skip
    text = imageopts.styled(job.get("expanded") or prompt, options.style)
    values = {**workflow.defaults, **files, "prompt": text, "seed": seed, "width": width, "height": height,
              "length": imageopts.frames(options.seconds, fps), "fps": fps}  # fmt: skip
    if steps:
        values["steps"] = steps
    if options.negative:
        values["negative"] = options.negative
    values["camera"] = imageopts.CAMERA[options.camera or "static"]
    graph = comfy.fill(workflow.graph, values)
    latent = next((edits.node_of(graph, c) for c in VIDEO_LATENTS if edits.node_of(graph, c)), None)
    try:
        if start is not None:
            graph["8100"] = {"class_type": "LoadImage", "inputs": {"image": server.upload(start)}}
            graph[latent]["inputs"]["start_image"] = ["8100", 0]
            loader = edits.node_of(graph, "CLIPVisionLoader")
            if loader is not None:  # the camera model also looks at the start image
                look = {"clip_vision": [loader, 0], "image": ["8100", 0], "crop": "none"}
                graph["8102"] = {"class_type": "CLIPVisionEncode", "inputs": look}
                graph[latent]["inputs"]["clip_vision_output"] = ["8102", 0]
        if end is not None:
            graph["8101"] = {"class_type": "LoadImage", "inputs": {"image": server.upload(end)}}
            graph[latent]["inputs"]["end_image"] = ["8101", 0]
        if clip is not None:
            continue_clip(graph, latent, server.upload(clip), values["length"])
        vace = edits.node_of(graph, "WanVaceToVideo")
        if refs and vace is not None:
            graph[vace]["inputs"]["reference_image"] = reference_strip(graph, [server.upload(p) for p in refs])
        if restyle is not None and vace is not None:
            graph[vace]["inputs"]["control_video"] = control_from(graph, server.upload(restyle), values["length"])
    except (comfy.ComfyError, OSError) as e:
        raise MediaError(f"Couldn't hand the files to ComfyUI: {e}") from e
    add_thumbnail(graph, width, height)
    meta = describe_job(model, workflow, prompt, text, seed, values, options, files)
    meta.update({"seconds": round((values["length"] - 1) / fps, 2), "fps": fps, "frames": values["length"],
                 "start_image": shown(agent, start) if start else "", "end_image": shown(agent, end) if end else "",
                 "camera": options.camera, "extends": shown(agent, clip) if clip else "",
                 "references": [shown(agent, p) for p in refs],
                 "restyles": shown(agent, restyle) if restyle else ""})  # fmt: skip
    keys = ("kind", "prompt", "sources", "mask", "pads", "keep_shape", "extend", "size", "refs", "restyle")
    remembered = {k: job.get(k) for k in keys}
    stem = f"{clip.stem}-extended" if clip else (f"{restyle.stem}-restyled" if restyle else "")
    plan = Plan(model, workflow, graph, meta, prompt, seed, options, "video", job.get("sources") or [], remembered,
                part[1] if part else stem, part[0] if part else None, part is not None)  # fmt: skip
    return run_plan(agent, server, plan, cancelled, label)


VIDEO_LATENTS = ("Wan22ImageToVideoLatent", "WanFunInpaintToVideo", "WanCameraImageToVideo", "WanVaceToVideo")


WHY = {
    "first-last-frame": "a clip between two frames",
    "camera": "a camera move",
    "extend": "extending a clip",
    "references": "following reference pictures",
    "restyle": "restyling a clip",
}


def reference_strip(graph: dict, names: list[str]) -> list:
    """The reference pictures side by side in one image, as VACE takes them."""
    graph["8300"] = {"class_type": "LoadImage", "inputs": {"image": names[0]}}
    strip = ["8300", 0]
    for k, name in enumerate(names[1:], 1):
        load, stitch = str(8300 + 2 * k - 1), str(8300 + 2 * k)
        graph[load] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph[stitch] = {
            "class_type": "ImageStitch",
            "inputs": {"image1": strip, "direction": "right", "match_image_size": True, "spacing_width": 0,
                       "spacing_color": "white", "image2": [load, 0]},
        }  # fmt: skip
        strip = [stitch, 0]
    return strip


def control_from(graph: dict, clip_name: str, length: int) -> list:
    """Restyling: the clip's outlines (Canny edges), frame by frame, for VACE to follow."""
    graph["8400"] = {"class_type": "LoadVideo", "inputs": {"file": clip_name}}
    graph["8401"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["8400", 0]}}
    graph["8402"] = {
        "class_type": "ImageFromBatch",
        "inputs": {"image": ["8401", 0], "batch_index": 0, "length": length},
    }
    latent = edits.node_of(graph, "WanVaceToVideo")
    w, h = graph[latent]["inputs"]["width"], graph[latent]["inputs"]["height"]
    graph["8403"] = {
        "class_type": "ImageScale",
        "inputs": {"image": ["8402", 0], "upscale_method": "bilinear", "width": w, "height": h, "crop": "center"},
    }
    graph["8404"] = {
        "class_type": "Canny",
        "inputs": {"image": ["8403", 0], "low_threshold": 0.3, "high_threshold": 0.6},
    }
    return ["8404", 0]


def video_model(agent: Agent, task: str, quiet: bool = False) -> catalog.MediaModel:
    """The video model for a job: the chosen one, or the first local one that does what the job needs (said, unless
    `quiet`: the parts of a long clip)."""
    chosen = current(agent, "video")
    if not chosen.cloud and (not task or task in chosen.tasks):
        return chosen
    model = catalog.load().for_task("video", task or "text-to-video")
    if model is None:
        raise MediaError(f"None of lcode's video models does {WHY.get(task, task)} yet.")
    where = f"; {chosen.name} doesn't do that here" if chosen.cloud else ""
    if not quiet:
        agent.say(f"{model.name} makes this one ({WHY.get(task, task)}{where}).")
    return model


def continue_clip(graph: dict, latent: str | None, clip_name: str, length: int) -> None:
    """Extending a clip: start from its last frame, and join the new frames (without that first one) after it."""
    graph["8200"] = {"class_type": "LoadVideo", "inputs": {"file": clip_name}}
    graph["8201"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["8200", 0]}}
    graph["8202"] = {"class_type": "ImageFromBatch", "inputs": {"image": ["8201", 0], "batch_index": -1, "length": 1}}
    graph[latent]["inputs"]["start_image"] = ["8202", 0]
    video = edits.node_of(graph, "CreateVideo")
    decoded = graph[video]["inputs"]["images"]
    graph["8203"] = {"class_type": "ImageFromBatch", "inputs": {"image": decoded, "batch_index": 1, "length": length}}
    graph["8204"] = {"class_type": "ImageBatch", "inputs": {"image1": ["8201", 0], "image2": ["8203", 0]}}
    graph[video]["inputs"]["images"] = ["8204", 0]


def parts_of(total: float, longest: float = imageopts.PART_SECONDS) -> list[float]:
    """How long each part of a long clip is: as few parts as it takes, about equally long (in quarter seconds)."""
    quarters = max(4, round(total * 4))
    count = max(1, math.ceil(quarters / (longest * 4)))
    base, extra = divmod(quarters, count)
    return [(base + (1 if k < extra else 0)) / 4 for k in range(count)]


def plan_parts(total: float, stops: int) -> list[tuple[float, int | None]]:
    """A long clip's parts, as (seconds, the picture it ends on or None). With `stops` pictures to reach after the
    start one, each gets an equal share of the clip, made in as few parts as fit; the last of them ends on it."""
    if not stops:
        return [(seconds, None) for seconds in parts_of(total)]
    if total < stops:
        raise MediaError(f"{stops + 1} pictures need a clip of at least {stops} s: --seconds {stops} or more.")
    out: list[tuple[float, int | None]] = []
    for stop in range(1, stops + 1):
        lengths = parts_of(total / stops)
        out += [(seconds, None) for seconds in lengths[:-1]] + [(lengths[-1], stop)]
    return out


def long_clip(
    agent: Agent,
    job: dict,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
    label: str = "",
) -> Made:
    """A clip longer than a video model makes at once: made in parts, each going on from the last frame of the one
    before (and the last one ending on the end picture, when there is one), then joined into one clip with ffmpeg.
    Esc keeps what's made: the parts so far, joined."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError(
            f"A clip longer than {imageopts.PART_SECONDS} s is made in parts and joined with ffmpeg: install it "
            f"(apt install ffmpeg, brew install ffmpeg), or ask for {imageopts.PART_SECONDS} s or less."
        )
    pictures = [Path(p) for p in job.get("sources") or []]
    plan = plan_parts(options.seconds, max(0, len(pictures) - 1))
    lengths = [seconds for seconds, _ in plan]
    chosen = current(agent, "video")
    if chosen.cloud:
        agent.say(
            f"{chosen.name} makes clips of up to {media_cloud.MAX_VIDEO_SECONDS} s: this one is made on your GPU."
        )
    clip = Path(job["extend"]) if job.get("extend") else None
    seed = options.seed if options.seed is not None else random.randrange(2**32)
    stem = f"{clip.stem}-extended" if clip else f"{time.strftime('%Y%m%d-%H%M%S')}-{comfy.slug(job['prompt'])}"
    work = comfy.unique(folder(agent) / f"{stem}-parts")
    say_plan(agent, options.seconds, plan, len(pictures), clip is not None)
    started = time.monotonic()
    frame = last_frame(ffmpeg, clip, work / "00-last.png") if clip else (pictures[0] if pictures else None)
    size = f"{job['size'][0]}x{job['size'][1]}" if clip and job.get("size") else ""
    made: list[Made] = []
    try:
        for k, (seconds, stop) in enumerate(plan):
            if k and cancelled():  # Esc between two parts: the next one isn't started
                raise MediaError("Cancelled before the next part.")
            part = {**job, "extend": None, "restyle": None, "size": None,
                    "sources": [str(frame)] if frame else [],
                    "refs": job.get("refs") if k == 0 and not clip else [],
                    "keep_shape": not size and job.get("keep_shape", True)}  # fmt: skip
            if stop is not None:  # this part ends on the next picture
                part["sources"] = [str(frame), str(pictures[stop])]
            one = replace(options, seconds=seconds, seed=seed + k, size=size or options.size)
            name = f"part {k + 1} of {len(plan)}" + (f", {label}" if label else "")
            made.append(generate_video(agent, part, one, cancelled, ask, name, (work, f"{k + 1:02d}")))
            agent.console.print(Text(f"  part {k + 1} of {len(plan)}: {seconds:g} s, made in "
                                     f"{made[-1].seconds:.0f} s", style="dim"))  # fmt: skip
            frame = last_frame(ffmpeg, made[-1].files[0], work / f"{k + 1:02d}-last.png")
    except MediaError as e:
        if not made:
            raise
        if not str(e).startswith("Cancelled"):
            raise MediaError(f"{e} The parts made so far are in {shown(agent, work)}.") from e
        result = join_long(agent, ffmpeg, job, options, made, lengths, seed, clip, f"{stem}-partial", started, work)
        agent.console.print(f"[yellow]⏹ Stopped after part {len(made)} of {len(lengths)}: what's made is "
                            f"#{result.numbers[0]}.[/]")  # fmt: skip
        return result
    return join_long(agent, ffmpeg, job, options, made, lengths, seed, clip, stem, started, work)


def say_plan(agent: Agent, total: float, plan: list[tuple[float, int | None]], pictures: int, extends: bool) -> None:
    """What a long clip takes, before it starts: its parts, which end on a picture, and about how long (at the pace
    of this session's last clip)."""
    lengths = [seconds for seconds, _ in plan]
    each = f"{lengths[0]:g}" if len(set(lengths)) == 1 else f"{min(lengths):g} to {max(lengths):g}"
    what = f"{total:g} s more" if extends else f"A {total:g} s clip"
    line = (f"{what}: lcode makes it in {len(plan)} parts of {each} s, each going on from the last frame of the one "
            "before, and joins them.")  # fmt: skip
    stops = [k + 1 for k, (_, stop) in enumerate(plan) if stop is not None]
    if pictures == 2:
        line += f" Part {stops[0]} goes to your end picture: the change toward it happens there."
    elif pictures > 2:
        named = ", ".join(str(n) for n in stops[:-1]) + f" and {stops[-1]}"
        line += f" Parts {named} each go to your next picture, in order."
    pace = [r["seconds"] / max(r["options"].get("seconds") or 5, 1) for r in agent.media_results
            if r.get("kind") == "video" and isinstance(r.get("options"), dict)]  # fmt: skip
    if pace:
        line += f" About {max(1, round(pace[-1] * total / 60))} min, at the pace of your last clip."
    agent.say(line)


def last_frame(ffmpeg: str, clip: Path, out: Path) -> Path:
    """A clip's last frame, as a picture: where its next part starts."""
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([ffmpeg, "-y", "-v", "error", "-sseof", "-1", "-i", str(clip), "-update", "1", str(out)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)  # fmt: skip
    if done.returncode != 0 or not out.exists():
        raise MediaError(f"ffmpeg couldn't read the last frame of {clip.name}: {done.stderr.strip()[-300:]}")
    return out


def join_command(ffmpeg: str, clips: list[Path], out: Path, fps: float, width: int, height: int) -> list[str]:
    """ffmpeg's command for one clip from its parts: each after the first starts on the frame the one before ended
    on, so that frame is left out; all at the first part's pace and size."""
    command = [ffmpeg, "-y", "-v", "error"]
    chains = []
    for k, clip in enumerate(clips):
        command += ["-i", str(clip)]
        steps = (["trim=start_frame=1", "setpts=PTS-STARTPTS"] if k else []) + [
            f"fps={fps:g}", f"scale={width}:{height}:flags=lanczos", "setsar=1"]  # fmt: skip
        chains.append(f"[{k}:v]{','.join(steps)}[p{k}]")
    joined = "".join(f"[p{k}]" for k in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[out]"
    return [*command, "-filter_complex", ";".join([*chains, joined]), "-map", "[out]", "-c:v", "libx264",
            "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]  # fmt: skip


def join_long(agent, ffmpeg, job, options, made, lengths, seed, clip, stem, started, work) -> Made:
    """The parts (after the clip they extend, if any) joined into one clip, numbered in the gallery and shown."""
    parts = [m.files[0] for m in made]
    pictures = job.get("sources") or []
    first = clip_info(clip or parts[0])
    width, height = (first["width"] or made[0].width), (first["height"] or made[0].height)
    fps = first["fps"] or 24
    out = comfy.unique(folder(agent) / f"{stem}.mp4")
    with agent.console.status("Joining the parts…", spinner="dots"):
        done = subprocess.run(join_command(ffmpeg, ([clip] if clip else []) + parts, out, fps, width, height),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)  # fmt: skip
    if done.returncode != 0:
        raise MediaError(f"ffmpeg couldn't join the parts (in {shown(agent, work)}): {done.stderr.strip()[-400:]}")
    models = " + ".join(dict.fromkeys(m.model for m in made))
    seconds = mp4_seconds(out) or sum(lengths[: len(made)])
    meta = {"model": models, "prompt": job["prompt"], "seed": seed, "width": width, "height": height,
            "fps": fps, "seconds": round(seconds, 2), "frames": round(seconds * fps),
            "start_image": shown(agent, Path(pictures[0])) if pictures else "",
            "end_image": shown(agent, Path(pictures[-1])) if len(pictures) > 1 else "",
            "pictures": [shown(agent, Path(p)) for p in pictures] if len(pictures) > 2 else [],
            "extends": shown(agent, clip) if clip else "", "camera": options.camera,
            "parts": [{"file": shown(agent, m.files[0]), "seconds": s, "model": m.model, "seed": m.seed}
                      for m, s in zip(made, lengths, strict=False)],
            "file": out.name}  # fmt: skip
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    one = Made([out], models, seed, width, height, time.monotonic() - started, job["prompt"])
    one.thumbnail, one.poster = made[0].thumbnail, made[0].poster
    keys = ("kind", "prompt", "sources", "mask", "pads", "keep_shape", "extend", "size", "refs", "restyle")
    unused = cast(Any, None)  # the joined clip is only recorded, never run: it has no model or workflow of its own
    plan = Plan(unused, unused, {}, meta, job["prompt"], seed, options, "video", job.get("sources") or [],
                {k: job.get(k) for k in keys}, stem)  # fmt: skip
    one.numbers = record(agent, one, plan)
    show(agent, one)
    return one
