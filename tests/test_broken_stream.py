"""A model server that stops in the middle of a reply: `lcode update` recreating Ollama's container, a crash, a
network that drops. lcode says what happened and what to do, and the session carries on instead of ending in a
traceback."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FakeOllama, output, reply
from lcode import api, cli, config, subagents, trace_view
from lcode.backends import BackendError, OpenAICompatible
from lcode.hardware import Hardware
from lcode.ollama import Ollama, OllamaError
from lcode.repl import repl

HW = Hardware("linux", "x", 31, "GPU", 12)
STOPPED = "Ollama stopped in the middle of a reply"


class StopsHalfway(BaseHTTPRequestHandler):
    """Ollama, or a server with an OpenAI-compatible API, that starts a reply and goes away: the connection closes
    before the reply's last chunk."""

    protocol_version = "HTTP/1.1"  # a reply streamed in chunks, as Ollama sends it

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if self.path.startswith("/v1/"):
            line = b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "Let me "}}]}).encode()
        else:
            line = json.dumps({"message": {"role": "assistant", "content": "Let me "}, "done": False}).encode()
        self.wfile.write(b"%x\r\n%s\n\r\n" % (len(line) + 1, line))
        self.then()
        self.close_connection = True  # without the last chunk

    def then(self) -> None:
        pass


class KeepsAnswering(StopsHalfway):
    """A model still writing its reply: the connection stays open until the client goes."""

    def then(self) -> None:
        self.rfile.read(1)  # returns when the client closes the connection


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def host():
    yield from serve(StopsHalfway)


@pytest.fixture
def answering():
    yield from serve(KeepsAnswering)


class Model(FakeOllama):
    """The scripted model. A script that's an address is a real reply: the Ollama client streams it from there."""

    def chat_stream(self, payload, on_open=None):
        if self.scripts and isinstance(self.scripts[0], str):
            self.payloads.append(payload)
            yield from Ollama(self.scripts.pop(0)).chat_stream(payload, on_open=on_open)
        else:
            yield from super().chat_stream(payload, on_open)


def test_the_clients_say_the_server_stopped_halfway(host):
    stream = Ollama(host).chat_stream({"model": "m", "messages": []})
    assert next(stream)["message"]["content"] == "Let me "
    with pytest.raises(OllamaError) as stopped:
        next(stream)
    message = str(stopped.value)
    assert message.startswith(f"{STOPPED}: the connection to {host} broke (")
    assert "send the request again" in message and "`lcode doctor`" in message
    server = OpenAICompatible(f"{host}/v1", name="LM Studio")
    stream = server.chat_stream({"model": "m", "messages": []})
    assert next(stream)["message"]["content"] == "Let me "
    with pytest.raises(BackendError) as stopped:
        next(stream)
    assert str(stopped.value).startswith(f"LM Studio stopped in the middle of a reply: the connection to {host}/v1 ")


def test_the_session_carries_on_after_ollama_stops_halfway(make_agent, host, monkeypatch):
    """The real run: `lcode update` recreated Ollama's container while a reply streamed, and lcode ended in a
    traceback. Now it says so, the trace records the failure, and the next request gets its answer."""
    agent = make_agent(trace=True)
    agent.ollama = Model([host, reply("add and sub.")])
    lines = iter(["what's in math.py?", "and now?"])

    class Session:
        def prompt(self, *args, **kwargs):
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr("lcode.repl.build_session", lambda agent: Session())
    monkeypatch.setattr("lcode.repl.preload", lambda agent, session: None)
    monkeypatch.setattr("lcode.repl.start_steering", lambda agent: None)
    repl(agent, prompt=None, hardware=HW)
    shown = " ".join(output(agent).split())
    assert f"{STOPPED}: the connection to {host} broke" in shown and "lcode doctor" in shown
    assert agent.messages[-1] == {"role": "assistant", "content": "add and sub."} and shown.endswith("Bye.")
    stopped, answered = sorted(trace_view.list_traces(), key=lambda info: info.id)
    assert (stopped.text, stopped.status, answered.status) == ("what's in math.py?", "error", "success")
    assert stopped.end["error"].startswith(STOPPED)
    [model] = [e for e in trace_view.load(stopped.path) if e["type"] == "model"]
    assert model["error"].startswith(STOPPED) and model["reply"]["content"] == "Let me "
    assert "stopped" not in model  # the server stopped it, not lcode


@pytest.fixture
def model(repo, tmp_path_factory, monkeypatch):
    """lcode's config and a scripted model, for `lcode -p`."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    config.save({"model": "qwen3.6-35b", "context": 32768, "memory": "off", "lsp": "off"})
    fake = Model()
    monkeypatch.setattr(cli, "Ollama", lambda host: fake)
    monkeypatch.setattr(cli, "detect", lambda: HW)
    monkeypatch.setattr("lcode.hardware.detect", lambda: HW)
    monkeypatch.setattr(api, "check_server", lambda *args: "0.32.0")
    return fake


def test_lcode_p_says_ollama_stopped_halfway(model, host, repo, capsys):
    model.scripts = [host]
    cli.main(["-p", "what's in math.py?", "-r", str(repo), "--no-mcp"])
    assert f"{STOPPED}: the connection to {host} broke" in " ".join(capsys.readouterr().out.split())
    model.scripts = [host]
    with pytest.raises(SystemExit) as stopped:
        cli.main(["-p", "what's in math.py?", "-r", str(repo), "--no-mcp", "--output", "json"])
    assert stopped.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error" and result["error"].startswith(f"{STOPPED}: the connection to {host} broke")


def test_a_reply_lcode_stops_stays_a_stop(make_agent, answering):
    """An editor's cancel (ACP) or a stopped subagent cuts the reply's connection from another thread
    (subagents.abort), and the stream breaks as it does when Ollama goes away. That's the stop working: the request
    is interrupted, and the trace says lcode stopped the reply."""
    agent = make_agent(trace=True)
    agent.ollama = Model([answering])
    agent.cancel = threading.Event()

    def cancel() -> None:  # as an editor does, while the model answers
        while agent.response is None:
            time.sleep(0.01)
        time.sleep(0.1)  # the first words are in
        agent.cancel.set()
        subagents.abort(agent.response)

    threading.Thread(target=cancel, daemon=True).start()
    result = api.run_request(agent, "what's in math.py?")
    assert (result.status, result.error) == ("interrupted", "interrupted before the request was done")
    assert "Interrupted" in output(agent) and STOPPED not in output(agent)
    [info] = trace_view.list_traces()
    [model] = [e for e in trace_view.load(info.path) if e["type"] == "model"]
    assert info.status == "interrupted" and model["stopped"] and "error" not in model
