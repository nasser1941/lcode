"""Video mode: a description (or a start image) becomes a clip, with Wan 2.2 TI2V 5B, against a stand-in ComfyUI."""

import json

import pytest

from comfy_standin import FLUX, StandIn, tiny_png
from conftest import FakeOllama, output
from lcode import catalog, cli, config, media
from lcode.hardware import Hardware
from lcode.repl import handle_command, respond

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
WAN = {
    "diffusion_models": [*FLUX["diffusion_models"], "wan2.2_ti2v_5B_fp16.safetensors"],
    "text_encoders": [*FLUX["text_encoders"], "umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
    "vae": [*FLUX["vae"], "wan2.2_vae.safetensors"],
}


def text(agent):
    return " ".join(output(agent).split())


@pytest.fixture
def standin():
    with StandIn(models=WAN) as server:
        yield server


@pytest.fixture
def video_agent(make_agent, standin, repo):
    agent = make_agent(comfyui_url=standin.url)
    agent.settings.mode = "video"
    agent.interactive = False
    (repo / "photo.png").write_bytes(tiny_png())
    return agent


def node(graph, class_type):
    return next(n for n in graph.values() if n["class_type"] == class_type)


def test_a_description_becomes_a_clip(video_agent, standin):
    [made] = media.request(video_agent, "waves rolling onto a beach at sunset")
    graph = standin.prompts[-1]
    latent = node(graph, "Wan22ImageToVideoLatent")["inputs"]
    assert (latent["width"], latent["height"], latent["length"]) == (864, 480, 121)  # 16:9, 5 s at 24 fps
    sampler = node(graph, "KSampler")["inputs"]
    assert sampler["steps"] == 20 and sampler["seed"] == made.seed
    assert node(graph, "CLIPTextEncode")["inputs"]["text"] == "waves rolling onto a beach at sunset"
    assert node(graph, "CreateVideo")["inputs"]["fps"] == 24 and "start_image" not in latent
    assert graph["9003"]["class_type"] == "ImageFromBatch"  # the first frame, for the preview
    assert made.files[0].suffix == ".mp4" and made.files[0].read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    meta = json.loads(made.files[0].with_suffix(".json").read_text())
    assert meta["seconds"] == 5.0 and meta["frames"] == 121 and meta["model"] == "Wan 2.2 TI2V 5B"
    assert video_agent.media_results[-1]["kind"] == "video" and "Wan 2.2 TI2V 5B" in text(video_agent)


def test_length_shape_and_quality(video_agent, standin):
    media.request(video_agent, "a hummingbird --seconds 3 --quality fast --ar 9:16")
    latent = node(standin.prompts[-1], "Wan22ImageToVideoLatent")["inputs"]
    assert latent["length"] == 73 and latent["width"] < latent["height"]
    assert latent["width"] % 32 == 0 and latent["height"] % 32 == 0
    assert node(standin.prompts[-1], "KSampler")["inputs"]["steps"] == 12
    media.request(video_agent, "a hummingbird --quality best")
    latent = node(standin.prompts[-1], "Wan22ImageToVideoLatent")["inputs"]
    assert (latent["width"], latent["height"]) == (1280, 704)  # the model's own 720p
    with pytest.raises(media.MediaError, match="seconds is how long a video is: 1 to 8"):
        media.request(video_agent, "a hummingbird --seconds 30")


def test_a_start_image_sets_the_first_frame_and_the_shape(video_agent, standin):
    media.request(video_agent, "@photo.png the camera slowly pulls back")
    graph = standin.prompts[-1]
    latent = node(graph, "Wan22ImageToVideoLatent")["inputs"]
    assert latent["start_image"] == ["8100", 0] and graph["8100"]["inputs"]["image"].endswith("photo.png")
    assert latent["width"] > latent["height"]  # a square picture is cropped to 16:9, which the model knows
    assert video_agent.media_results[-1]["job"]["sources"] == [str(video_agent.cwd / "photo.png")]
    with pytest.raises(media.MediaError, match="doesn't take a mask"):
        media.request(video_agent, "@photo.png --mask photo.png waves")


def test_a_negative_prompt_replaces_wans_own(video_agent, standin):
    media.request(video_agent, "a city street at night")
    negatives = [n["inputs"]["text"] for n in standin.prompts[-1].values() if n["class_type"] == "CLIPTextEncode"]
    assert any("过曝" in t for t in negatives)  # Wan's default negative prompt
    media.request(video_agent, 'a city street at night --no "rain, people"')
    negatives = [n["inputs"]["text"] for n in standin.prompts[-1].values() if n["class_type"] == "CLIPTextEncode"]
    assert "rain, people" in negatives


def test_animate_an_image_and_play_it(make_agent, standin, repo, monkeypatch):
    agent = make_agent(comfyui_url=standin.url)
    agent.settings.mode = "image"
    agent.interactive = False
    monkeypatch.setattr("lcode.repl.preload", lambda agent, session=None: None)
    respond(agent, "a lighthouse in a storm")
    handle_command(agent, "/animate 1 lightning flashes and the waves crash", LAPTOP)
    assert agent.settings.mode == "video" and agent.media_results[-1]["kind"] == "video"
    latent = node(standin.prompts[-1], "Wan22ImageToVideoLatent")["inputs"]
    assert "start_image" in latent
    handle_command(agent, "/vary 2 --n 1", LAPTOP)  # a variation of a clip is another clip from the same picture
    assert (
        agent.media_results[-1]["kind"] == "video"
        and "start_image" in node(standin.prompts[-1], "Wan22ImageToVideoLatent")["inputs"]
    )
    opened = []
    monkeypatch.setattr(media, "open_file", lambda path: opened.append(path) or "")
    handle_command(agent, "/play", LAPTOP)
    assert opened[0].suffix == ".mp4" and "Opened #3" in text(agent)
    handle_command(agent, "/edit 3 brighter", LAPTOP)
    assert "/edit works in image mode" in text(agent)


def test_the_first_frame_shows_in_terminals_that_show_pictures(video_agent, monkeypatch):
    video_agent.settings.preview = "kitty"
    written = []
    monkeypatch.setattr(video_agent.console.file, "write", lambda s: written.append(s) or len(s))
    media.request(video_agent, "a red fox running")
    assert any(s.startswith("\x1b_Ga=T,f=100") for s in written)


def test_a_clip_from_the_command_line(tmp_path, monkeypatch, capsys, standin, repo):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    cli.main(["config", "set", "comfyui_url", standin.url])
    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    monkeypatch.setattr("lcode.api.check_server", lambda *args: "0.35.1")
    monkeypatch.setattr(cli, "resolve_model", lambda ollama, name: (name, catalog.find(name)))
    monkeypatch.setattr(cli, "choose_context", lambda *args: (32768, ""))
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as done:
        cli.main(["--mode", "video", "-p", "waves at sunset --seconds 2", "--output", "json", "--no-mcp"])
    assert done.value.code == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["mode"] == "video" and result["files"][0].endswith(".mp4")


def test_the_video_model_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    assert config.load()["video_model"] == "wan22-ti2v-5b"
    with pytest.raises(SystemExit):
        cli.main(["config", "set", "video_model", "flux2-klein-4b"])  # an image model
    cli.main(["config", "set", "video_model", "wan22-ti2v-5b"])
