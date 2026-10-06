import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from lcode.mcp import catalog
from lcode.servers import xai

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg"
MP4 = b"\x00\x00\x00\x18ftypmp42video"
MP3 = b"ID3" + b"sound"


class FakeXAI(BaseHTTPRequestHandler):
    """xAI's image, video and speech endpoints, as far as lcode-mcp-xai uses them."""

    requests: ClassVar[list] = []
    polls: ClassVar[int] = 0
    video_ready_after: ClassVar[int] = 1

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("Authorization") != "Bearer good-key":
            self.send(401, {"error": {"message": "Incorrect API key provided"}})
            return False
        return True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, body))
        if not self.authorized():
            return
        if self.path == "/v1/images/generations":
            if "forbidden" in body["prompt"]:
                self.send(400, {"error": {"message": "Generated image rejected by content moderation."}})
                return
            images = [{"b64_json": base64.b64encode(PNG).decode()} for _ in range(body.get("n", 1))]
            self.send(200, {"data": images})
        elif self.path == "/v1/images/edits":
            self.send(200, {"data": [{"url": f"http://{self.headers['Host']}/files/edited.jpg"}]})
        elif self.path == "/v1/videos/generations":
            self.send(200, {"request_id": "req-1"})
        elif self.path == "/v1/tts":
            self.send(200, {"audio": base64.b64encode(MP3).decode(), "content_type": "audio/mpeg", "duration": 1.5})
        else:
            self.send(404, {"error": "no such endpoint"})

    def do_GET(self):
        if self.path == "/files/edited.jpg":
            self.send(200, JPEG, "image/jpeg")
        elif self.path == "/files/video.mp4":
            self.send(200, MP4, "video/mp4")
        elif self.path == "/v1/videos/req-1" and self.authorized():
            type(self).polls += 1
            if type(self).polls <= type(self).video_ready_after:
                self.send(200, {"status": "pending"})
            else:
                url = f"http://{self.headers['Host']}/files/video.mp4"
                video = {"url": url, "duration": 6}
                self.send(200, {"status": "done", "video": video, "model": "grok-imagine-video-1.5"})


@pytest.fixture
def api(tmp_path, monkeypatch):
    FakeXAI.requests, FakeXAI.polls, FakeXAI.video_ready_after = [], 0, 1
    monkeypatch.setattr(xai, "POLL", 0.01)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeXAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield xai.XAI("good-key", f"http://127.0.0.1:{httpd.server_address[1]}/v1", tmp_path / "generated")
    httpd.shutdown()


def call(server, name, **arguments):
    result = server.call(name, arguments)
    return result["content"][0]["text"], result["isError"]


def test_images_are_generated_and_saved(api, tmp_path):
    server = xai.build(api)
    assert [t.name for t in server.listed()] == [
        "generate_image", "edit_image", "generate_video", "video_status", "text_to_speech"]  # fmt: skip
    text, error = call(server, "generate_image", prompt="A red fox in the snow", n=2, aspect_ratio="16:9")
    assert not error and text.startswith("Saved 2 image(s) from grok-imagine-image-2.0:")
    saved = sorted((tmp_path / "generated").glob("a-red-fox-in-the-snow-*.png"))
    assert len(saved) == 2 and all(p.read_bytes() == PNG for p in saved)
    path, body = FakeXAI.requests[0]
    assert path == "/v1/images/generations" and body == {
        "model": "grok-imagine-image-2.0", "prompt": "A red fox in the snow", "n": 2,
        "response_format": "b64_json", "aspect_ratio": "16:9"}  # fmt: skip
    text, _ = call(server, "generate_image", prompt="logo", path=str(tmp_path / "assets" / "logo"))
    assert text.endswith(f"- {tmp_path / 'assets' / 'logo.png'}")  # the right extension is added
    text, _ = call(server, "generate_image", prompt="logo", path=str(tmp_path / "assets" / "logo.png"))
    assert text.endswith(f"- {tmp_path / 'assets' / 'logo-2.png'}")  # nothing is overwritten


def test_an_image_is_edited_from_a_local_file(api, tmp_path):
    source = tmp_path / "photo.png"
    source.write_bytes(PNG)
    text, error = call(xai.build(api), "edit_image", image=str(source), prompt="Make it a pencil sketch")
    assert not error and text.startswith("Saved the edited image:") and text.endswith(".jpg")
    _, body = FakeXAI.requests[0]
    assert body["image"] == {"url": "data:image/png;base64," + base64.b64encode(PNG).decode(), "type": "image_url"}
    text, error = call(xai.build(api), "edit_image", image=str(tmp_path / "missing.png"), prompt="x")
    assert error and "doesn't exist" in text


def test_a_video_is_generated_waited_for_and_downloaded(api, tmp_path, monkeypatch):
    start = tmp_path / "first.jpg"
    start.write_bytes(JPEG)
    server = xai.build(api)
    text, error = call(server, "generate_video", prompt="The fox runs away", image=str(start), duration=6)
    assert not error and text.startswith("Saved the video (6 s, grok-imagine-video-1.5):") and text.endswith(".mp4")
    _, body = FakeXAI.requests[0]
    assert body["duration"] == 6 and body["image"]["url"].startswith("data:image/jpeg;base64,")
    assert next((tmp_path / "generated").glob("*.mp4")).read_bytes() == MP4 and FakeXAI.polls == 2
    FakeXAI.polls, FakeXAI.video_ready_after = 0, 10**6
    monkeypatch.setattr(xai, "VIDEO_WAIT", 0)
    text, _ = call(server, "generate_video", prompt="slow")
    assert "isn't ready yet (request req-1)" in text and 'video_status(request_id="req-1")' in text
    FakeXAI.video_ready_after = 0
    text, _ = call(server, "video_status", request_id="req-1", path=str(tmp_path / "clip.mp4"))
    assert text.endswith(str(tmp_path / "clip.mp4")) and (tmp_path / "clip.mp4").read_bytes() == MP4


def test_speech_is_saved_as_audio(api, tmp_path):
    text, error = call(xai.build(api), "text_to_speech", text="Hello from lcode", voice="Leo", language="en")
    assert not error and text.startswith("Saved the speech (Leo, 1.5 s):") and text.endswith(".mp3")
    _, body = FakeXAI.requests[0]
    assert body == {"text": "Hello from lcode", "voice_id": "leo", "language": "en", "output_format": {"codec": "mp3"},
                    "speed": 1.0}  # fmt: skip
    assert next((tmp_path / "generated").glob("hello-from-lcode-*.mp3")).read_bytes() == MP3


def test_xai_errors_reach_the_model(api, tmp_path):
    text, error = call(xai.build(api), "generate_image", prompt="something forbidden")
    assert error and text == "xAI error 400: Generated image rejected by content moderation."
    bad = xai.XAI("wrong", api.base, tmp_path)
    text, error = call(xai.build(bad), "text_to_speech", text="hi")
    assert error and "xAI refused the request (401): Incorrect API key provided" in text and "XAI_API_KEY" in text


def test_the_preset_asks_for_the_key():
    preset = catalog.load()["xai"]
    [key] = preset.inputs
    assert key.var == "XAI_API_KEY" and key.secret
    server = catalog.build(preset, {"XAI_API_KEY": "typed-" + "key"})
    assert server["env"] == {"XAI_API_KEY": "typed-key"} and server["args"][-1] == "lcode-mcp-xai"
    assert catalog.build(preset, {"XAI_API_KEY": None})["env"] == {"XAI_API_KEY": "${XAI_API_KEY}"}
    assert server["timeout"] == 900
