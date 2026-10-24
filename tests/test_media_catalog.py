"""The media catalog (media.toml): models and their files, fit on this machine, downloads, /model in image mode."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from comfy_standin import StandIn
from conftest import output
from lcode import cli, comfy, config, media
from lcode import media_catalog as catalog
from lcode.hardware import Hardware
from lcode.repl import handle_command

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
SMALL_GPU = Hardware("linux", "x", 16, "RTX 3060", 8)
MAC = Hardware("macos", "M3", 16, "Apple M3", 0, True)
NO_GPU = Hardware("linux", "x", 16)


def text(agent):
    return " ".join(output(agent).split())


# ----------------------------------------------------------------------------- the catalog itself


def test_every_model_has_a_workflow_its_files_fill():
    cat = catalog.load()
    assert cat.models and catalog.DEFAULT_IMAGE in cat.models
    for model in cat.models.values():
        names = {role: f.name for role, f in model.files.items()}
        for key in filter(None, (model.workflow, model.video_workflow)):
            workflow = comfy.load_workflow(key)
            values = {**workflow.defaults, **names, "prompt": "a fox", "seed": 1, "image": "in.png", "video": "in.mp4"}
            graph = comfy.fill(workflow.graph, values)
            assert "$" not in json.dumps(graph), key  # every placeholder has a value
        assert model.mode in ("image", "video", "tool") and model.tasks and model.license
    for f in cat.files.values():
        assert f.url.startswith("https://huggingface.co/") and f.url.endswith(f.name) and f.gb > 0
        assert f.folder in (
            "diffusion_models",
            "text_encoders",
            "vae",
            "loras",
            "checkpoints",
            "upscale_models",
            "background_removal",
            "clip_vision",
            "frame_interpolation",
        )


def test_files_are_shared_and_counted_once():
    cat = catalog.load()
    flux, zimage = cat.models["flux2-klein-4b"], cat.models["z-image-turbo"]
    assert flux.files["text_encoder"] is zimage.files["text_encoder"]  # one download serves both
    assert flux.size_gb == pytest.approx(4.07 + 8.04 + 0.34)


def test_finding_a_model():
    cat = catalog.load()
    assert cat.find("z-image-turbo").key == "z-image-turbo"
    assert cat.find("Z-Image Turbo").key == "z-image-turbo"
    assert cat.find("2", "image") == cat.for_mode("image")[1]
    assert cat.find("9", "image") is None and cat.find("nothing") is None


def test_whether_it_fits_here():
    flux = catalog.load().models["flux2-klein-4b"]
    assert flux.fit(LAPTOP)[0] == "fits"
    assert flux.fit(SMALL_GPU)[0] == "slower"  # ComfyUI keeps part of it in RAM
    assert flux.fit(MAC)[0] == "fits"
    assert flux.fit(NO_GPU)[0] == "cpu"
    assert flux.fit(Hardware("linux", "x", 4, "GT 1030", 2))[0] == "too big"


def test_installed_under_its_own_name_or_an_alternative():
    flux = catalog.load().models["flux2-klein-4b"]
    folders = {
        "diffusion_models": {"flux-2-klein-4b.safetensors"},  # the full-precision build, not the fp8 one
        "text_encoders": {"qwen_3_4b_fp8_mixed.safetensors"},
        "vae": set(),
    }
    chosen, missing = catalog.chosen_files(flux, lambda folder: folders[folder])
    assert chosen == {"model": "flux-2-klein-4b.safetensors", "text_encoder": "qwen_3_4b_fp8_mixed.safetensors"}
    assert [f.name for f in missing] == ["flux2-vae.safetensors"]


# ----------------------------------------------------------------------------- downloads


class Response:
    """What requests.get returns, for a file served from bytes (honouring Range)."""

    def __init__(self, data: bytes, headers: dict, status: int = 200, sha: str = "", ignore_range=False):
        self.status_code, self.headers, self.history = status, dict(headers), []
        self.data = data
        if sha:
            redirect = type("Redirect", (), {"headers": {"X-Linked-Etag": f'"{sha}"'}})()
            self.history = [redirect]

    def iter_content(self, size):
        for i in range(0, len(self.data), 7):
            yield self.data[i : i + 7]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def server_for(data: bytes, sha: str = "", ignore_range=False, status=None, calls=None):
    def get(url, headers=None, **kwargs):
        if calls is not None:
            calls.append(dict(headers or {}))
        if status:
            return Response(b"", {}, status)
        start = int(re.match(r"bytes=(\d+)-", headers["Range"]).group(1)) if headers.get("Range") else 0
        if start and ignore_range:
            start = 0
        if start >= len(data) and start:
            return Response(b"", {}, 416, sha)
        body = data[start:]
        return Response(body, {"Content-Length": str(len(body))}, 206 if start else 200, sha)

    return get


DATA = b"weights " * 1000
SHA = hashlib.sha256(DATA).hexdigest()


def test_a_download_is_checked_and_lands_complete(tmp_path):
    dest = tmp_path / "vae" / "flux2-vae.safetensors"
    seen = []
    catalog.download("https://huggingface.co/x/flux2-vae.safetensors", dest, lambda d, t: seen.append((d, t)),
                     server_for(DATA, SHA))  # fmt: skip
    assert dest.read_bytes() == DATA and not dest.with_name(dest.name + ".part").exists()
    assert seen[-1] == (len(DATA), len(DATA))


def test_an_interrupted_download_resumes_where_it_stopped(tmp_path):
    dest = tmp_path / "model.safetensors"
    dest.with_name(dest.name + ".part").write_bytes(DATA[:3000])  # what an earlier try left
    calls = []
    catalog.download("https://example.com/model.safetensors", dest, get=server_for(DATA, SHA, calls=calls))
    assert calls[0]["Range"] == "bytes=3000-" and dest.read_bytes() == DATA
    dest.unlink()  # a server that ignores the range sends it all again: lcode starts over
    dest.with_name(dest.name + ".part").write_bytes(DATA[:3000])
    catalog.download("https://example.com/model.safetensors", dest, get=server_for(DATA, SHA, ignore_range=True))
    assert dest.read_bytes() == DATA
    dest.unlink()  # a .part that's already whole
    dest.with_name(dest.name + ".part").write_bytes(DATA)
    catalog.download("https://example.com/model.safetensors", dest, get=server_for(DATA, SHA))
    assert dest.read_bytes() == DATA


def test_a_damaged_download_is_thrown_away(tmp_path):
    dest = tmp_path / "model.safetensors"
    with pytest.raises(catalog.CatalogError, match="damaged"):
        catalog.download("https://example.com/m", dest, get=server_for(DATA, "0" * 64))
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_a_gated_file_says_how_to_get_access(tmp_path, monkeypatch):
    with pytest.raises(catalog.CatalogError, match=r"accept its license on huggingface\.co, then set HF_TOKEN"):
        catalog.download("https://huggingface.co/x/m", tmp_path / "m", get=server_for(DATA, status=401))
    calls = []
    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    catalog.download("https://huggingface.co/x/m", tmp_path / "m", get=server_for(DATA, SHA, calls=calls))
    assert calls[0]["Authorization"] == "Bearer hf_test_token"
    calls.clear()
    catalog.download("https://example.com/m", tmp_path / "n", get=server_for(DATA, SHA, calls=calls))
    assert "Authorization" not in calls[0]  # the token goes to Hugging Face only


def fake_install(tmp_path) -> comfy.Install:
    root = tmp_path / "ComfyUI"
    (root / "comfy").mkdir(parents=True)
    (root / "main.py").write_text("")
    return comfy.Install(root, "python")


def serve_catalog(monkeypatch, files: dict[str, bytes]):
    """Downloads answer with these bytes per file name."""

    def get(url, headers=None, **kwargs):
        data = files[url.rsplit("/", 1)[1]]
        return server_for(data, hashlib.sha256(data).hexdigest())(url, headers or {}, **kwargs)

    monkeypatch.setattr(catalog, "http_get", get)


def test_installing_downloads_only_whats_missing(tmp_path, monkeypatch):
    install = fake_install(tmp_path)
    (install.path / "models" / "text_encoders").mkdir(parents=True)
    (install.path / "models" / "text_encoders" / "qwen_3_4b.safetensors").write_text("already here")
    serve_catalog(monkeypatch, {"z_image_turbo_int8_convrot.safetensors": b"z" * 50, "ae.safetensors": b"a" * 20})
    asked = []
    zimage = catalog.load().models["z-image-turbo"]
    saved = catalog.install(zimage, install=install, ask=lambda q: asked.append(q) or True)
    assert sorted(p.name for p in saved) == ["ae.safetensors", "z_image_turbo_int8_convrot.safetensors"]
    assert "6.5 GB into" in asked[0] and "qwen_3_4b" not in asked[0]
    assert catalog.chosen_files(zimage, catalog.lister(install=install))[1] == []
    assert catalog.install(zimage, install=install, ask=lambda q: pytest.fail("nothing to ask")) == []


def test_installing_checks_the_disk_and_asks_first(tmp_path, monkeypatch):
    install = fake_install(tmp_path)
    flux = catalog.load().models["flux2-klein-4b"]
    monkeypatch.setattr(catalog.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 5 * 10**9})())
    with pytest.raises(catalog.CatalogError, match=r"not enough disk space for FLUX.2 klein 4B: needs 12.4 GB"):
        catalog.install(flux, install=install, ask=lambda q: True)
    monkeypatch.setattr(catalog.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 500 * 10**9})())
    with pytest.raises(catalog.CatalogError, match="download declined"):
        catalog.install(flux, install=install, ask=lambda q: False)
    assert not (install.path / "models" / "diffusion_models").exists()


def test_ctrl_c_pauses_a_download_to_resume_later(tmp_path, monkeypatch):
    install = fake_install(tmp_path)

    def get(url, headers=None, **kwargs):
        response = server_for(b"x" * 100)(url, headers or {})

        def chunks(size):
            yield b"x" * 30
            raise KeyboardInterrupt

        response.iter_content = chunks
        return response

    monkeypatch.setattr(catalog, "http_get", get)
    flux = catalog.load().models["flux2-klein-4b"]
    with pytest.raises(catalog.CatalogError, match=r"Paused flux-2-klein-4b-fp8\.safetensors: install flux2-klein-4b"):
        catalog.install(flux, install=install)
    part = install.path / "models" / "diffusion_models" / "flux-2-klein-4b-fp8.safetensors.part"
    assert part.read_bytes() == b"x" * 30


def test_installing_into_a_running_comfyui_uses_its_folders(tmp_path, monkeypatch):
    serve_catalog(monkeypatch, {"z_image_turbo_int8_convrot.safetensors": b"z" * 50, "ae.safetensors": b"a" * 20,
                                "qwen_3_4b.safetensors": b"q" * 40})  # fmt: skip
    with StandIn(models_root=tmp_path / "models") as server:
        zimage = catalog.load().models["z-image-turbo"]
        catalog.install(zimage, server=comfy.Server(server.url))
        assert catalog.chosen_files(zimage, catalog.lister(comfy.Server(server.url)))[1] == []
    assert (tmp_path / "models" / "vae" / "ae.safetensors").read_bytes() == b"a" * 20


# ----------------------------------------------------------------------------- image mode, lcode media, settings


def test_choosing_the_image_model_in_image_mode(make_agent, monkeypatch, tmp_path):
    with StandIn() as server:  # FLUX.2 klein is installed there, Z-Image isn't
        agent = make_agent(comfyui_url=server.url)
        agent.settings.mode = "image"
        monkeypatch.setattr("builtins.input", lambda prompt: "2")
        handle_command(agent, "/models", LAPTOP)
    listing = text(agent)
    assert "1. ● FLUX.2 klein 4B (flux2-klein-4b)" in listing and "installed · fits" in listing
    assert "2. Z-Image Turbo (z-image-turbo)" in listing and "6.5 GB to download" in listing
    assert agent.settings.image_model == "z-image-turbo" and "Image model: Z-Image Turbo" in listing
    assert "Not installed yet: 6.5 GB download with your next image" in listing
    handle_command(agent, "/model flux2-klein-4b", LAPTOP)
    assert agent.settings.image_model == "flux2-klein-4b"
    handle_command(agent, "/model 7", LAPTOP)
    assert "There's no image model '7'" in text(agent)


def test_the_next_image_installs_a_new_model_when_you_agree(make_agent, monkeypatch, tmp_path):
    root = tmp_path / "models"
    (root / "text_encoders").mkdir(parents=True)
    (root / "text_encoders" / "qwen_3_4b.safetensors").write_text("here")
    serve_catalog(monkeypatch, {"z_image_turbo_int8_convrot.safetensors": b"z" * 50, "ae.safetensors": b"a" * 20})
    with StandIn(models_root=root) as server:
        agent = make_agent(comfyui_url=server.url, image_model="z-image-turbo")
        with pytest.raises(media.MediaError, match=r"lcode media install z-image-turbo"):
            media.generate_image(agent, "a fox")  # nobody to ask
        made = media.generate_image(agent, "a fox", ask=lambda q: True)
        graph = server.prompts[-1]
    assert made.model == "Z-Image Turbo" and "Installed Z-Image Turbo" in text(agent)
    assert graph["1"]["inputs"]["unet_name"] == "z_image_turbo_int8_convrot.safetensors"
    assert graph["2"]["inputs"]["type"] == "lumina2" and graph["8"]["inputs"]["steps"] == 8
    assert json.loads(made.files[0].with_suffix(".json").read_text())["model_key"] == "z-image-turbo"


def test_lcode_media_lists_and_installs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    serve_catalog(monkeypatch, {"z_image_turbo_int8_convrot.safetensors": b"z" * 50, "ae.safetensors": b"a" * 20,
                                "qwen_3_4b.safetensors": b"q" * 40})  # fmt: skip
    with StandIn(models_root=tmp_path / "models") as server:
        cli.main(["config", "set", "comfyui_url", server.url])
        cli.main(["media"])
        listing = " ".join(capsys.readouterr().out.split())
        assert "Image models" in listing and "z-image-turbo Z-Image Turbo · text-to-image" in listing
        assert "14.6 GB to download" in listing
        assert "wan22-ti2v-5b Wan 2.2 TI2V 5B · text-to-video, image-to-video" in listing
        cli.main(["media", "install", "z-image-turbo", "--yes"])
        assert "Z-Image Turbo is installed (3 file(s) downloaded)" in " ".join(capsys.readouterr().out.split())
        with pytest.raises(SystemExit):
            cli.main(["media", "install", "nothing"])


def test_the_image_model_setting(tmp_path, monkeypatch, make_agent):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    assert config.load()["image_model"] == "flux2-klein-4b"
    with pytest.raises(SystemExit):
        cli.main(["config", "set", "image_model", "dall-e"])
    cli.main(["config", "set", "image_model", "z-image-turbo"])
    assert config.load()["image_model"] == "z-image-turbo"
    agent = make_agent(image_model="z-image-turbo")
    agent.messages.append({"role": "user", "content": "hi"})
    agent.save()
    from lcode import sessions

    other = make_agent()
    other.load(sessions.list_sessions(agent.workspace)[0])
    assert other.settings.image_model == "z-image-turbo"  # a session keeps its image model
    assert Path(media.folder(other)).name == "media"
