"""Ollama's cloud models (tags like minimax-m3.1-flash:cloud): lcode says where they run and what they cost,
skips what only makes sense for a local model, and says how to sign in when Ollama refuses them."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import output
from lcode import cloud, escalate, residency
from lcode.hardware import Hardware
from lcode.ollama import Ollama, OllamaError
from lcode.repl import handle_command, preload

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def test_cloud_tags_are_recognized():
    for name in ("minimax-m3.1-flash:cloud", "deepseek-v4-pro:0813-cloud", "mistral-large-3:675b-cloud", "X:CLOUD"):
        assert cloud.is_cloud(name), name
    for name in ("qwen3.6:35b-a3b-coding", "lcode-qwen3.6-35b", "my-cloud-model:latest", "cloud", None, ""):
        assert not cloud.is_cloud(name), name


class Refuses(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = b'{"error":"Unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def refusing_ollama(monkeypatch):
    monkeypatch.setattr(cloud, "signin_command", lambda host: "docker exec -it my-ollama ollama signin")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Refuses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Ollama(f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()


def test_an_ollama_that_isnt_signed_in_says_how_to_fix_it(refusing_ollama):
    payload = {"model": "minimax-m3.1-flash:cloud", "messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(OllamaError) as streamed:
        list(refusing_ollama.chat_stream(payload))
    with pytest.raises(OllamaError) as asked:
        refusing_ollama.chat(payload)
    for error in (streamed.value, asked.value):
        assert "runs in Ollama's cloud (ollama.com)" in str(error)
        assert "`docker exec -it my-ollama ollama signin`" in str(error)
    with pytest.raises(OllamaError, match="Ollama error 401"):  # a local model keeps the plain error
        list(refusing_ollama.chat_stream({**payload, "model": "qwen3.6:35b-a3b-coding"}))


def test_the_stronger_model_panel_tells_where_a_cloud_model_runs_and_what_it_costs():
    t = escalate.target("ollama/minimax-m3.1-flash:cloud")
    assert t.label == "minimax-m3.1-flash:cloud (Ollama cloud)" and t.cloud
    assert t.where == "ollama.com, through this machine's Ollama"
    assert escalate.cost_line(t, 12000, None).startswith("unknown: set escalate_price")
    assert escalate.cost_line(t, 12000, (0.15, 0.6)).startswith("about $0.002 to $0.004")
    remote = escalate.target("ollama/glm-5.3-flash:cloud", "http://gpu-box:11434")
    assert remote.where == "ollama.com, through gpu-box:11434"
    local = escalate.target("ollama/qwen3.8-27b")
    assert not local.cloud and local.where == "this machine"
    assert escalate.cost_line(local, 12000, None) == "free: it runs on this machine"


def test_a_cloud_model_skips_what_only_a_local_model_needs(make_agent):
    agent = make_agent(model="minimax-m3.1-flash:cloud")
    agent.ollama.load = lambda *a, **k: pytest.fail("nothing to load: it runs on ollama.com")
    preload(agent)
    assert agent.before_request(progress=None) is None
    assert residency.dense_warning(agent.ollama, "minimax-m3.1-flash:cloud", 262144, LAPTOP) == ""


def test_switching_to_a_cloud_model_says_where_the_code_goes(agent, monkeypatch):
    agent.ollama.installed_names = lambda: {"lcode-qwen3.6-35b", "minimax-m3.1-flash:cloud"}
    handle_command(agent, "/model minimax-m3.1-flash:cloud", LAPTOP)
    text = " ".join(output(agent).split())
    assert "Switched to minimax-m3.1-flash:cloud" in text
    assert "runs in Ollama's cloud: what lcode sends (your requests and the files it reads) goes to ollama.com" in text
