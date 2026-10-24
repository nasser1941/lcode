"""Films: a storyboard from the prompt assistant, the cast made once, keyframes with the cast as references,
shots animated from them, joined by ffmpeg (stubbed here) — against a stand-in ComfyUI and a fake Ollama."""

import json
import subprocess

import pytest

from comfy_standin import FLUX, StandIn
from conftest import output
from lcode import film
from lcode.hardware import Hardware
from lcode.repl import handle_command

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
MODELS = {
    "diffusion_models": [*FLUX["diffusion_models"], "wan2.2_ti2v_5B_fp16.safetensors"],
    "text_encoders": [*FLUX["text_encoders"], "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "t5-base.safetensors"],
    "vae": [*FLUX["vae"], "wan2.2_vae.safetensors"],
    "checkpoints": ["stable-audio-open-1.0.safetensors"],
}
STORYBOARD = {
    "title": "The Fox and the Kitten",
    "look": "cinematic realism, soft winter light",
    "cast": [{"name": "Fox", "description": "a red fox"}, {"name": "Kitten", "description": "a small white kitten"}],
    "shots": [
        {"description": "Fox finds Kitten under a snowbank.", "camera": "static shot", "cast": ["Fox", "Kitten"],
         "seconds": 3},
        {"description": "They walk home through the pines.", "camera": "tracking shot", "cast": ["Fox", "Kitten"],
         "seconds": 3},
        {"description": "A warm doorway glows in the dark forest.", "camera": "slow zoom in", "cast": [], "seconds": 2},
    ],
}  # fmt: skip


def text(agent):
    return " ".join(output(agent).split())


@pytest.fixture
def standin():
    with StandIn(models=MODELS) as server:
        yield server


@pytest.fixture
def agent(make_agent, standin, monkeypatch):
    agent = make_agent(comfyui_url=standin.url, prompt_assistant="video")
    agent.settings.mode = "video"
    agent.interactive = False
    monkeypatch.setattr(agent.ollama, "installed_names", lambda: {"qwen3.5:4b"})
    joined = []

    def ffmpeg(command, **kwargs):
        joined.append(command)
        film.Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42 a film")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(film.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(film.subprocess, "run", ffmpeg)
    agent.joined = joined
    return agent


def graphs_of(standin, class_type):
    return [g for g in standin.prompts if any(n["class_type"] == class_type for n in g.values())]


def test_a_storyboard_is_written_and_saved(agent):
    agent.ollama.chat_replies = [json.dumps(STORYBOARD), json.dumps(STORYBOARD)]
    board, folder = film.plan(agent, "a fox finds a lost kitten", 3, 3)
    assert board.title == "The Fox and the Kitten" and list(board.cast) == ["Fox", "Kitten"]
    assert [s.seconds for s in board.shots] == [3, 3, 2] and board.shots[2].cast == []
    assert folder.name == "the-fox-and-the-kitten" and json.loads((folder / "storyboard.json").read_text())["idea"]
    assert (
        "1. Fox finds Kitten under a snowbank. (Fox, Kitten) — static shot, 3 s"
        in (folder / "storyboard.md").read_text()
    )
    chat = agent.ollama.chats[0]
    assert chat["format"] == "json" and "3 shots, each about 3 seconds" in chat["messages"][0]["content"]
    _, again = film.plan(agent, "a fox finds a lost kitten", 3, 3)
    assert again.name == "the-fox-and-the-kitten-2"  # a second film doesn't overwrite the first


def test_a_storyboard_needs_the_assistant(agent, monkeypatch):
    monkeypatch.setattr(agent.ollama, "installed_names", lambda: set())
    with pytest.raises(film.FilmError, match=r"ollama pull qwen3\.5:4b"):
        film.plan(agent, "a fox", 3, 3)


def test_what_a_storyboard_may_hold():
    shot = {"description": "Ann waves.", "cast": ["Ann", "Bob"], "seconds": 99}
    board = film.Board.from_json({"title": "T", "cast": [{"name": "Ann", "description": "a girl"}], "shots": [shot]})
    assert board.shots[0].cast == ["Ann"] and board.shots[0].seconds == 4.0  # unknown names and lengths dropped
    with pytest.raises(film.FilmError, match="no shots"):
        film.Board.from_json({"title": "T", "shots": []})


def test_the_cast_stays_the_same_from_shot_to_shot(agent, standin):
    agent.ollama.chat_replies = [json.dumps(STORYBOARD)]
    board, folder = film.plan(agent, "a fox finds a lost kitten", 3, 3)
    board = film.with_options(board, "fade", "wind in the pines")
    result = film.make(agent, board, folder)
    assert sorted(p.name for p in (folder / "cast").iterdir()) == ["fox.png", "kitten.png"]
    keyframes = graphs_of(standin, "ReferenceLatent")
    assert len(keyframes) == 2  # the two shots with cast members, each made with their pictures as references
    loads = [n["inputs"]["image"] for n in keyframes[0].values() if n["class_type"] == "LoadImage"]
    assert [name.split("-", 2)[-1] for name in loads] == ["fox.png", "kitten.png"]
    videos = graphs_of(standin, "Wan22ImageToVideoLatent")
    assert len(videos) == 3 and all(
        "start_image" in next(n for n in g.values() if n["class_type"] == "Wan22ImageToVideoLatent")["inputs"]
        for g in videos
    )
    assert sorted(p.name for p in (folder / "shots").iterdir()) == ["01.mp4", "02.mp4", "03.mp4"]
    command = agent.joined[0]
    assert "xfade=transition=fade" in command[command.index("-filter_complex") + 1]
    assert graphs_of(standin, "EmptyLatentAudio") and result.name == "film-sound.mp4"
    assert agent.settings.mode == "video"  # back where it was


def test_one_shot_made_again(agent, standin):
    agent.ollama.chat_replies = [json.dumps(STORYBOARD)]
    board, folder = film.plan(agent, "a fox finds a lost kitten", 3, 3)
    film.make(agent, board, folder)
    before = len(standin.prompts)
    film.make(agent, board, folder, only=[2])
    assert len(standin.prompts) - before == 2  # the keyframe and the clip; the cast is kept
    with pytest.raises(film.FilmError, match="Say which shot: 1 to 3"):
        film.shot_numbers("9", 3)


def test_joining_cut_or_fade():
    shots = [film.Path(f"/f/shots/0{k}.mp4") for k in (1, 2, 3)]
    cut = film.join_command("ffmpeg", shots, film.Path("/f/film.mp4"), "cut", [3, 3, 3])
    assert cut[cut.index("-filter_complex") + 1] == "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]"
    fade = film.join_command("ffmpeg", shots, film.Path("/f/film.mp4"), "fade", [3, 3, 3])
    graph = fade[fade.index("-filter_complex") + 1]
    assert "offset=2.500[v1]" in graph and "offset=5.000[v2]" in graph and fade[fade.index("-map") + 1] == "[v2]"


def test_film_commands(agent, standin):
    agent.ollama.chat_replies = [json.dumps(STORYBOARD)]
    handle_command(agent, "/film plan a fox finds a lost kitten --shots 3 --transition fade", LAPTOP)
    assert "The Fox and the Kitten" in text(agent) and "storyboard.json if you like, then /film make" in text(agent)
    handle_command(agent, "/film list", LAPTOP)
    assert "the-fox-and-the-kitten The Fox and the Kitten · 3 shots · storyboard only" in text(agent)
    handle_command(agent, "/film stitch", LAPTOP)
    assert "Shots not made yet: 01.mp4, 02.mp4, 03.mp4" in text(agent)
    handle_command(agent, "/film make", LAPTOP)
    assert "◆ The Fox and the Kitten:" in text(agent)
    handle_command(agent, "/film shot 7", LAPTOP)
    assert "Say which shot: 1 to 3" in text(agent)
    handle_command(agent, "/film", LAPTOP)
    assert "Say what the film is about" in text(agent)
