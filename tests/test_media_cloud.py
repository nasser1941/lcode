"""Cloud models in image and video mode (Grok Imagine): the cost shown and asked about, the key sent only to the API,
results saved like local ones, and what a cloud model can't do made locally. Against a stand-in xAI API."""

import base64
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from comfy_standin import FLUX, StandIn, tiny_png
from conftest import output
from lcode import media, media_cloud
from lcode import media_catalog as catalog
from lcode.hardware import Hardware
from lcode.repl import handle_command

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg"


def mp4(width: int, height: int) -> bytes:
    """Just enough of an MP4 for its size: a ftyp box and a version-0 track header."""
    tkhd = b"\x00" * 76 + struct.pack(">II", width << 16, height << 16)  # version, flags, times… then the size
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8 + struct.pack(">I", 92) + b"tkhd" + tkhd


class FakeXAI(BaseHTTPRequestHandler):
    """xAI's image and video endpoints, as image and video mode use them."""

    calls: ClassVar[list] = []  # (method, path, body, had the key)
    polls: ClassVar[int] = 0
    video_ends: ClassVar[str] = "done"

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def record(self, body=None):
        type(self).calls.append((self.command, self.path, body, "Authorization" in self.headers))
        if self.path.startswith("/v1/") and self.headers.get("Authorization") != "Bearer good-key":
            self.send(401, {"error": {"message": "Incorrect API key provided"}})
            return False
        return True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.record(body):
            return
        if self.path == "/v1/images/generations":
            if "forbidden" in body["prompt"]:
                self.send(400, {"error": {"message": "Generated image rejected by content moderation."}})
                return
            self.send(200, {"data": [{"b64_json": base64.b64encode(tiny_png()).decode()}] * body["n"]})
        elif self.path == "/v1/images/edits":
            self.send(200, {"data": [{"url": f"http://{self.headers['Host']}/files/edited.jpg"}]})
        elif self.path == "/v1/videos/generations":
            self.send(200, {"request_id": "req-1"})
        else:
            self.send(404, {"error": "no such endpoint"})

    def do_GET(self):
        if not self.record():
            return
        if self.path == "/files/edited.jpg":
            self.send(200, JPEG, "image/jpeg")
        elif self.path == "/files/clip.mp4":
            self.send(200, mp4(1280, 720), "video/mp4")
        elif self.path == "/v1/videos/req-1":
            type(self).polls += 1
            if type(self).polls < 2:
                self.send(200, {"status": "pending"})
            elif type(self).video_ends == "failed":
                self.send(200, {"status": "failed", "error": {"message": "the video was rejected"}})
            elif type(self).video_ends == "never":
                self.send(200, {"status": "pending"})
            else:
                url = f"http://{self.headers['Host']}/files/clip.mp4"
                self.send(200, {"status": "done", "video": {"url": url, "duration": 6}})


@pytest.fixture
def xai(monkeypatch):
    FakeXAI.calls, FakeXAI.polls, FakeXAI.video_ends = [], 0, "done"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeXAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("XAI_API_KEY", "good-key")
    monkeypatch.setenv("XAI_API_BASE", f"http://127.0.0.1:{httpd.server_address[1]}/v1")
    monkeypatch.setattr(media_cloud, "POLL", 0.01)
    yield FakeXAI
    httpd.shutdown()


@pytest.fixture
def agent(make_agent, repo):
    agent = make_agent(image_model="grok-image", video_model="grok-video")
    agent.settings.mode = "image"
    agent.interactive = False
    (repo / "photo.png").write_bytes(tiny_png())
    return agent


class Asked(list):
    """An `ask` that says `answer` and remembers the questions."""

    def __init__(self, answer=True):
        super().__init__()
        self.answer = answer

    def __call__(self, question):
        self.append(question)
        return self.answer


def text(agent):
    return " ".join(output(agent).split())


def api_calls(xai):
    return [(method, path, body) for method, path, body, _ in xai.calls if path.startswith("/v1/")]


def test_pictures_are_made_at_xai_after_the_cost_is_shown(agent, xai):
    ask = Asked()
    made = media.request(agent, "a red fox in the snow --n 2 --ar wide", ask=ask)
    assert ask == ["Send it to xAI (about $0.08)?"]  # asked once for both
    shown = text(agent)
    assert "Grok Imagine Image 2.0 (grok-imagine-image-2.0) at xAI, 127.0.0.1" in shown
    assert "Cost: about $0.08 (2 images × $0.04)" in shown and "prices as of 2026-10-24" in shown
    assert "Your XAI_API_KEY goes with it, to 127.0.0.1" in shown and "good-key" not in shown
    assert api_calls(xai) == [("POST", "/v1/images/generations", {
        "model": "grok-imagine-image-2.0", "prompt": "a red fox in the snow", "n": 2, "response_format": "b64_json",
        "aspect_ratio": "16:9"})]  # fmt: skip
    assert [m.numbers for m in made] == [[1], [2]] and agent.media_results[1]["model"] == "Grok Imagine Image 2.0"
    first = media.Path(agent.media_results[0]["path"])
    assert first.parent.name == "media" and first.suffix == ".png" and first.name.endswith("-1.png")
    sidecar = json.loads(first.with_suffix(".json").read_text())
    assert sidecar["provider"] == "xAI" and sidecar["cost_usd"] == 0.04 and sidecar["width"] == 1
    assert "good-key" not in first.with_suffix(".json").read_text()
    assert "about $0.08 at xAI (this session: about $0.08)" in shown and agent.cloud_spent == pytest.approx(0.08)


def test_saying_no_sends_nothing(agent, xai):
    with pytest.raises(media.MediaError, match="Nothing was sent to xAI"):
        media.request(agent, "a red fox", ask=Asked(False))
    assert xai.calls == [] and agent.media_results == [] and agent.cloud_spent == 0


def test_no_key_or_nobody_to_ask_sends_nothing(agent, xai, monkeypatch):
    with pytest.raises(media.MediaError, match="nobody can be asked here: run lcode in a terminal"):
        media.request(agent, "a red fox")  # lcode -p: no one to say yes to the cost
    monkeypatch.delenv("XAI_API_KEY")
    with pytest.raises(media.MediaError, match=r"export XAI_API_KEY=… \(from https://console.x.ai\)"):
        media.request(agent, "a red fox", ask=Asked())
    assert xai.calls == []


def test_an_edit_sends_its_picture_and_comes_back_by_address(agent, xai):
    ask = Asked()
    media.request(agent, "@photo.png make it a pencil sketch", ask=ask)
    assert "Cost: about $0.05 (1 image × $0.04 + 1 picture sent × $0.01)" in text(agent)
    assert "the prompt and 1 picture (photo.png)" in text(agent)
    (_, path, body), *_ = api_calls(xai)
    assert path == "/v1/images/edits" and body["image"]["type"] == "image_url"
    assert body["image"]["url"] == "data:image/png;base64," + base64.b64encode(tiny_png()).decode()
    download = next(c for c in xai.calls if c[1] == "/files/edited.jpg")
    assert download[3] is False  # the key goes to the API only, not to where results are downloaded from
    entry = agent.media_results[-1]
    assert entry["kind"] == "edit" and entry["path"].endswith(".jpg") and entry["sources"][0].endswith("photo.png")


def test_what_the_cloud_model_cant_do_is_made_locally(make_agent, repo, xai):
    with StandIn(models=FLUX) as comfy:
        agent = make_agent(image_model="grok-image", comfyui_url=comfy.url)
        agent.settings.mode = "image"
        agent.interactive = False
        (repo / "photo.png").write_bytes(tiny_png())
        (repo / "hat.png").write_bytes(tiny_png())
        media.request(agent, "@photo.png @hat.png the cat wears the hat", ask=Asked())
    assert "Grok Imagine Image 2.0 doesn't do several pictures at once here: FLUX.2 klein 4B makes this one on " \
        "your GPU" in text(agent)  # fmt: skip
    assert xai.calls == [] and len(comfy.prompts) == 1


def test_a_clip_is_made_waited_for_and_downloaded(agent, xai):
    agent.settings.mode = "video"
    ask = Asked()
    made = media.request(agent, "@photo.png the waves crash --seconds 6 --quality fast --camera zoom-in", ask=ask)
    assert ask == ["Send it to xAI (about $0.49)?"]
    assert "Cost: about $0.49 (6 s at 480p × $0.08/s + 1 picture sent × $0.01)" in text(agent)
    (_, path, body), *_ = api_calls(xai)
    assert path == "/v1/videos/generations" and body["model"] == "grok-imagine-video-1.5"
    assert body["duration"] == 6 and body["resolution"] == "480p" and body["aspect_ratio"] == "16:9"
    assert body["prompt"] == "the waves crash Camera: zoom in." and body["image"]["url"].startswith("data:image/png")
    assert xai.polls == 2
    [clip] = made
    assert clip.files[0].suffix == ".mp4" and (clip.width, clip.height) == (1280, 720)
    sidecar = json.loads(clip.files[0].with_suffix(".json").read_text())
    assert sidecar["seconds"] == 6 and sidecar["request_id"] == "req-1" and sidecar["start_image"] == ["photo.png"]
    assert agent.media_results[-1]["kind"] == "video"


def test_a_clip_between_two_frames_is_made_locally(agent, xai, monkeypatch):
    agent.settings.mode = "video"
    called = {}
    monkeypatch.setattr(media, "generate_video", lambda agent, job, *a, **k: called.setdefault("job", job))
    (agent.cwd / "end.png").write_bytes(tiny_png())
    media.request(agent, "@photo.png @end.png the sun sets", ask=Asked())
    assert len(called["job"]["sources"]) == 2 and xai.calls == []
    assert catalog.load().for_task("video", "first-last-frame").key == "wan22-fun-inp-5b"  # never a cloud model


def test_xai_saying_no_is_explained(agent, xai, monkeypatch):
    with pytest.raises(media.MediaError, match="xAI didn't make it \\(400\\): Generated image rejected by content"):
        media.request(agent, "something forbidden", ask=Asked())
    monkeypatch.setenv("XAI_API_KEY", "wrong-key")
    with pytest.raises(media.MediaError, match=r"refused the request \(401: Incorrect API key provided\)\. Check XAI"):
        media.request(agent, "a red fox", ask=Asked())
    agent.settings.mode = "video"
    monkeypatch.setenv("XAI_API_KEY", "good-key")
    xai.video_ends = "failed"
    with pytest.raises(media.MediaError, match="couldn't make the video \\(failed\\): the video was rejected"):
        media.request(agent, "the waves crash", ask=Asked())
    assert agent.media_results == [] and agent.cloud_spent == 0


def test_stopping_says_the_clip_may_still_be_billed(agent, xai):
    agent.settings.mode = "video"
    xai.video_ends = "never"
    calls = iter(range(10**6))
    with pytest.raises(media.MediaError, match=r"Stopped waiting: xAI may still make the video and bill it \(request"):
        media.request(agent, "the waves crash", cancelled=lambda: next(calls) > 3, ask=Asked())


def test_cloud_models_are_marked_and_chosen(agent, xai, monkeypatch):
    handle_command(agent, "/model 3", LAPTOP)
    assert "Image model: Grok Imagine Image 2.0." in text(agent)
    assert "runs at xAI: each request shows what goes there and about what it costs ($0.04 per image)" in text(agent)
    line = catalog.describe(catalog.load().models["grok-video"], LAPTOP, None)
    assert line == (
        "text-to-video, image-to-video · cloud: xAI · $0.08–0.25 per second (480p–1080p) · XAI_API_KEY is set"
    )
    monkeypatch.delenv("XAI_API_KEY")
    assert media_cloud.describe(catalog.load().models["grok-image"])[-1] == "needs XAI_API_KEY"
    assert "Set your key first: export XAI_API_KEY=…" in media_cloud.ready_note(catalog.load().models["grok-image"])
    assert media.warm(agent) is None  # no ComfyUI to start for a cloud model


def test_shapes_the_provider_knows():
    assert media_cloud.ratio("wide", media_cloud.IMAGE_RATIOS) == "16:9"
    assert media_cloud.ratio("1344x768", media_cloud.IMAGE_RATIOS) == "16:9"
    assert media_cloud.ratio("21:9", media_cloud.VIDEO_RATIOS) == "16:9"  # the nearest a clip can have
    assert media_cloud.ratio("5:4", media_cloud.IMAGE_RATIOS) == "4:3"
    assert media.mp4_size(media.Path(__file__)) == (0, 0)  # "tkhd" in a file that isn't an MP4
