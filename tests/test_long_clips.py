"""Clips longer than a video model makes at once: made in parts, each from the last frame of the one before (the last
ending on the end picture), joined by ffmpeg (stubbed here), against a stand-in ComfyUI."""

import json
import shutil
import subprocess

import pytest

from comfy_standin import StandIn, tiny_png
from conftest import output
from lcode import media
from lcode.repl import handle_command
from test_video import LAPTOP, WAN, node


@pytest.fixture
def standin():
    with StandIn(models=WAN) as server:
        yield server


@pytest.fixture
def agent(make_agent, standin, repo, monkeypatch):
    agent = make_agent(comfyui_url=standin.url)
    agent.settings.mode = "video"
    agent.interactive = False
    (repo / "photo.png").write_bytes(tiny_png())
    (repo / "end.png").write_bytes(tiny_png())
    ran = []
    real_run, real_which = subprocess.run, shutil.which

    def ffmpeg(command, **kwargs):  # subprocess.run for everyone: only ffmpeg is stubbed
        if command[0] != "/usr/bin/ffmpeg":
            return real_run(command, **kwargs)
        ran.append(command)
        out = media.Path(command[-1])
        out.write_bytes(tiny_png() if out.suffix == ".png" else b"\x00\x00\x00\x18ftypmp42 joined")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else real_which(name))
    monkeypatch.setattr(media.subprocess, "run", ffmpeg)
    agent.ffmpeg = ran
    return agent


def text(agent):
    return " ".join(output(agent).split())


def test_parts_are_as_few_and_as_even_as_can_be():
    assert media.parts_of(25) == [6.25] * 4
    assert media.parts_of(60) == [7.5] * 8
    assert media.parts_of(17) == [5.75, 5.75, 5.5]
    assert media.parts_of(9) == [4.5, 4.5] and media.parts_of(8) == [8.0]
    odd = media.parts_of(59.9)
    assert max(odd) <= 8 and sum(odd) == 60.0  # in quarter seconds


def test_a_25_second_clip_from_a_start_to_an_end_picture(agent, standin, repo):
    [made] = media.request(agent, "@photo.png @end.png the flower opens --seconds 25 --quality fast")
    assert len(standin.prompts) == 4
    *middle, last = standin.prompts
    for k, graph in enumerate(middle):  # each part from the last frame of the one before
        latent = node(graph, "Wan22ImageToVideoLatent")["inputs"]
        assert latent["length"] == 153 and latent["start_image"] == ["8100", 0]  # 6.25 s at 24 fps
        start = graph["8100"]["inputs"]["image"]
        assert start.endswith("photo.png") if k == 0 else start.endswith(f"{k:02d}-last.png")
    ending = node(last, "WanFunInpaintToVideo")["inputs"]  # the last part ends on the end picture
    assert ending["start_image"] == ["8100", 0] and ending["end_image"] == ["8101", 0]
    assert last["8100"]["inputs"]["image"].endswith("03-last.png")
    assert last["8101"]["inputs"]["image"].endswith("end.png")
    seeds = [node(g, "KSampler")["inputs"]["seed"] for g in standin.prompts]
    assert seeds == [made.seed + k for k in range(4)]
    *frames, join = agent.ffmpeg
    assert len(frames) == 4 and all(c[c.index("-sseof") + 1] == "-1" for c in frames)
    graph = join[join.index("-filter_complex") + 1]
    assert join.count("-i") == 4 and graph.count("trim=start_frame=1") == 3  # each join's repeated frame left out
    assert "concat=n=4" in graph and "fps=24" in graph and "scale=864:480" in graph
    assert len(agent.media_results) == 1  # the parts aren't in the gallery: the clip is
    entry = agent.media_results[0]
    assert entry["path"] == str(made.files[0]) and entry["job"]["sources"][1].endswith("end.png")
    meta = json.loads(made.files[0].with_suffix(".json").read_text())
    assert [p["seconds"] for p in meta["parts"]] == [6.25] * 4 and meta["end_image"] == "end.png"
    assert meta["model"] == "Wan 2.2 TI2V 5B + Wan 2.2 Fun InP 5B"
    assert all((repo / p["file"]).exists() for p in meta["parts"])
    said = text(agent)
    assert "A 25 s clip: lcode makes it in 4 parts of 6.25 s, each going on from the last frame of the one" in said
    assert "Part 4 goes to your end picture: the change toward it happens there." in said and "part 4 of 4" in said
    assert "makes this one" not in said  # the parts don't each say which model makes them


def test_a_clip_through_several_pictures_in_order(agent, standin, repo):
    (repo / "middle.png").write_bytes(tiny_png())
    [made] = media.request(agent, "@photo.png @middle.png @end.png the flower opens --seconds 20")
    parts = standin.prompts
    assert len(parts) == 4  # two shares of 10 s, each in two parts of 5 s
    starts = [graph["8100"]["inputs"]["image"] for graph in parts]
    assert [name.endswith(end) for name, end in zip(starts, ["photo.png", "01-last.png", "02-last.png", "03-last.png"],
                                                         strict=True)] == [True] * 4  # fmt: skip
    assert all(node(parts[k], "Wan22ImageToVideoLatent") for k in (0, 2))  # going on from the last frame
    assert parts[1]["8101"]["inputs"]["image"].endswith("middle.png")  # then to the next picture
    assert parts[3]["8101"]["inputs"]["image"].endswith("end.png")
    meta = json.loads(made.files[0].with_suffix(".json").read_text())
    assert meta["pictures"] == ["photo.png", "middle.png", "end.png"] and meta["end_image"] == "end.png"
    assert "Parts 2 and 4 each go to your next picture, in order." in text(agent)
    standin.prompts.clear()
    media.request(agent, "@photo.png @middle.png @end.png the flower opens --seconds 4")  # short: one part each
    assert len(standin.prompts) == 2 and all(node(g, "WanFunInpaintToVideo") for g in standin.prompts)
    with pytest.raises(media.MediaError, match="3 pictures need a clip of at least 2 s"):
        media.request(agent, "@photo.png @middle.png @end.png the flower opens --seconds 1")
    assert media.plan_parts(25, 2) == [(6.25, None), (6.25, 1), (6.25, None), (6.25, 2)]


def test_extending_a_clip_by_more_than_a_part(agent, standin):
    media.request(agent, "a hummingbird near red flowers --seconds 2")
    handle_command(agent, "/extend 1 --seconds 12 it flies away", LAPTOP)
    parts = standin.prompts[1:]
    assert len(parts) == 2
    first = parts[0]
    assert first["8100"]["inputs"]["image"].endswith("00-last.png")  # the clip's own last frame
    latent = node(first, "Wan22ImageToVideoLatent")["inputs"]
    assert (latent["width"], latent["height"]) == (864, 480) and latent["length"] == 145  # the clip's size; 6 s
    join = agent.ffmpeg[-1]
    assert join.count("-i") == 3 and join[join.index("-i") + 1].endswith(".mp4")  # the clip, then the parts
    entry = agent.media_results[-1]
    assert entry["path"].endswith("-extended.mp4") and entry["prompt"] == "it flies away"
    assert "12 s more: lcode makes it in 2 parts of 6 s" in text(agent)


def test_stopping_keeps_what_was_made(agent, standin):
    made = media.request(agent, "waves on a beach --seconds 20", cancelled=lambda: len(agent.ffmpeg) >= 1)
    assert len(standin.prompts) == 1 and len(made) == 1
    assert made[0].files[0].name.endswith("-partial.mp4") and agent.media_results[-1]["path"] == str(made[0].files[0])
    assert "Stopped after part 1 of 3: what's made is #1." in text(agent)


def test_limits_and_what_a_long_clip_needs(agent, standin, monkeypatch):
    with pytest.raises(media.MediaError, match="seconds is how long a video is: 1 to 60"):
        media.request(agent, "a lake --seconds 61")
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    with pytest.raises(media.MediaError, match="made in parts and joined with ffmpeg: install it"):
        media.request(agent, "a lake --seconds 20")
    assert standin.prompts == []


def test_sound_for_a_clip_longer_than_stable_audio_makes(agent, standin):
    media.request(agent, "rain on a window --seconds 60")
    handle_command(agent, "/sound 1 rain and distant thunder", LAPTOP)
    lengths = [v["inputs"]["seconds"] for v in standin.prompts[-1].values() if "seconds" in v["inputs"]]
    assert lengths == [47.0]
    assert "Stable Audio Open makes up to 47 s of sound: the rest of the clip is silent." in text(agent)
