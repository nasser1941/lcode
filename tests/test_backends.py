import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from rich.console import Console

from lcode import backends, cli, codesearch, config
from lcode.backends import BackendError, OpenAICompatible

PNG = "iVBORw0KGgo" + "A" * 20
MODELS = ["qwen/qwen3.6-35b-a3b", "google/gemma-4-12b", "text-embedding-qwen3-embedding-0.6b"]


def sse(*chunks) -> list[dict]:
    return list(chunks)


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields}]}


def call_piece(index, call_id="", name="", arguments="") -> dict:
    piece = {"index": index, "function": {"arguments": arguments}}
    if call_id:
        piece["id"], piece["type"] = call_id, "function"
    if name:
        piece["function"]["name"] = name
    return delta(tool_calls=[piece])


USAGE = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8}}


class FakeServer(BaseHTTPRequestHandler):
    """LM Studio, llama-server and vLLM, as far as lcode sees them."""

    models: ClassVar[list] = []
    props: ClassVar[dict | None] = None  # llama-server's /props
    lmstudio: ClassVar[dict] = {}  # LM Studio's /api/v0/models/<id>
    replies: ClassVar[list] = []  # one per chat request: a list of stream chunks, or (status, text)
    requests: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def send(self, status: int, body) -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self.send(200, {"object": "list", "data": [{"id": m, **extra} for m, extra in self.models]})
        elif self.path == "/props" and self.props is not None:
            self.send(200, self.props)
        elif self.path.startswith("/api/v0/models/") and self.lmstudio:
            self.send(200, self.lmstudio)
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        if self.path == "/v1/embeddings":
            vectors = [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(body["input"])]
            self.send(200, {"data": list(reversed(vectors))})
            return
        reply = type(self).replies.pop(0)
        if isinstance(reply, tuple):
            self.send(*reply)
        elif not body.get("stream"):
            self.send(200, {"choices": [{"message": reply}]})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in reply:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def server(monkeypatch):
    monkeypatch.delenv("LCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    FakeServer.models = [(m, {}) for m in MODELS]
    FakeServer.props, FakeServer.lmstudio = None, {}
    FakeServer.replies, FakeServer.requests = [], []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


def chats() -> list[dict]:
    return [r["body"] for r in FakeServer.requests if r["path"] == "/v1/chat/completions"]


# ----------------------------------------------------------------------------- translation


def test_messages_become_openai_messages():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "what is in this?", "images": [PNG, "/9j/4AAQ"]},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
                {"id": "call_x", "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
            ],
        },
        {"role": "tool", "tool_name": "read_file", "content": "print(1)"},
        {"role": "tool", "tool_name": "bash", "content": "a.py"},
        {"role": "assistant", "content": "done"},
    ]
    out = backends.to_openai(messages)
    assert out[1]["content"][0] == {"type": "text", "text": "what is in this?"}
    assert out[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert out[1]["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    calls = out[2]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_2_0", "call_x"] and calls[0]["type"] == "function"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}
    assert calls[1]["function"]["arguments"] == '{"command": "ls"}'  # already a string: unchanged
    assert [(m["tool_call_id"], m["content"]) for m in out[3:5]] == [("call_2_0", "print(1)"), ("call_x", "a.py")]
    assert out[5] == {"role": "assistant", "content": "done"}


def test_requests_carry_tools_reasoning_switch_and_schemas(server, monkeypatch):
    monkeypatch.setenv("LCODE_API_KEY", "local-" + "secret")
    client = OpenAICompatible(server)
    FakeServer.replies = [{"content": '{"ok": true}'}, sse(delta(content="hi"), USAGE)]
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    reply = client.chat(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "think": False, "format": schema, "tools": tools}
    )
    assert reply == {"message": {"role": "assistant", "content": '{"ok": true}'}}
    payload = {"model": "m", "messages": [], "tools": tools, "options": {"num_ctx": 4096, "num_predict": 1}}
    list(client.chat_stream(payload))
    first, second = chats()
    assert first["response_format"]["json_schema"]["schema"] == schema and not first["stream"]
    assert first["chat_template_kwargs"] == {"enable_thinking": False} and "tools" not in first
    assert second["tools"] == tools and second["stream_options"] == {"include_usage": True}
    assert second["max_tokens"] == 1 and "chat_template_kwargs" not in second and "options" not in second
    assert FakeServer.requests[0]["auth"] == "Bearer local-secret"


# ----------------------------------------------------------------------------- streaming


def test_a_stream_becomes_ollama_chunks(server):
    FakeServer.replies = [
        sse(
            delta(role="assistant", reasoning_content="Let me look"),
            delta(content="Reading "),
            delta(content="it."),
            call_piece(0, "call_a", "read_file", '{"pa'),
            call_piece(0, arguments='th": "README.md"}'),
            call_piece(1, "call_b", "bash", ""),
            call_piece(1, arguments='{"command": "ls"}'),
            USAGE,
        )
    ]
    chunks = list(OpenAICompatible(server).chat_stream({"model": "m", "messages": []}))
    assert chunks[0]["message"] == {"thinking": "Let me look"}
    assert "".join(c["message"].get("content", "") for c in chunks) == "Reading it."
    [calls] = [c["message"]["tool_calls"] for c in chunks if c["message"].get("tool_calls")]
    assert calls == [
        {"id": "call_a", "function": {"name": "read_file", "arguments": {"path": "README.md"}}},
        {"id": "call_b", "function": {"name": "bash", "arguments": {"command": "ls"}}},
    ]
    final = chunks[-1]
    assert final["done"] and final["prompt_eval_count"] == 120 and final["eval_count"] == 8
    assert final["prompt_eval_duration"] > 0 and final["eval_duration"] > 0


@pytest.mark.parametrize("cut", [1, 3, 7, 100])
def test_thinking_sent_as_text_is_moved_out_of_the_answer(cut):
    text = "\n<think>\nThe bug is in clamp.</think>\n\nFixed clamp; <think> stays here."
    splitter = backends.ThinkSplitter()
    pieces = []
    for start in range(0, len(text), cut):
        pieces += splitter.feed(text[start : start + cut])
    pieces += splitter.flush()
    thinking = "".join(p for kind, p in pieces if kind == "thinking")
    answer = "".join(p for kind, p in pieces if kind == "content")
    assert thinking == "\nThe bug is in clamp." and answer == "Fixed clamp; <think> stays here."
    assert backends.strip_thinking("<thi") == "<thi" and backends.strip_thinking("No tags.") == "No tags."
    assert backends.strip_thinking("<think>unfinished") == ""


def test_llama_server_timings_replace_the_estimates(server):
    timings = {"cache_n": 2000, "prompt_n": 3000, "prompt_ms": 1500.0, "predicted_n": 40, "predicted_ms": 800.0}
    FakeServer.replies = [
        sse(delta(content="OK"), {"choices": [], "timings": timings}),
        sse(delta(content="OK"), {**USAGE, "timings": timings}),
    ]
    client = OpenAICompatible(server)
    final = list(client.chat_stream({"model": "m", "messages": []}))[-1]
    assert (final["prompt_eval_count"], final["prompt_eval_duration"]) == (5000, 1_500_000_000)  # with the cache
    assert (final["eval_count"], final["eval_duration"]) == (40, 800_000_000)
    final = list(client.chat_stream({"model": "m", "messages": []}))[-1]
    assert (final["prompt_eval_count"], final["eval_count"]) == (120, 8)  # the usage counts, when given


def test_a_schema_the_server_cant_use_is_asked_for_in_words(server):
    FakeServer.replies = [
        (400, '{"error": {"message": "Failed to initialize samplers: failed to parse grammar"}}'),
        {"content": 'Here: {"ok": true}'},
    ]
    payload = {"model": "m", "messages": [], "format": {"type": "object"}, "tools": [{"type": "function"}]}
    assert OpenAICompatible(server).chat(payload)["message"]["content"] == 'Here: {"ok": true}'
    retry = chats()[1]
    assert "response_format" not in retry and "tools" not in retry


def test_server_errors(server):
    client = OpenAICompatible(server)
    FakeServer.replies = [
        (400, '{"error": {"message": "the request exceeds the available context size, try increasing it"}}'),
        sse(call_piece(0, "c", "bash", '{"command": "ls"')),
        sse({"error": {"message": "model crashed"}}),
    ]
    with pytest.raises(BackendError, match="no longer fits the server's context window"):
        list(client.chat_stream({"model": "m", "messages": []}))
    with pytest.raises(BackendError, match=r"^error parsing tool call: raw="):  # the agent asks for a retry
        list(client.chat_stream({"model": "m", "messages": []}))
    with pytest.raises(BackendError, match="model crashed"):
        list(client.chat_stream({"model": "m", "messages": []}))
    with pytest.raises(BackendError, match="cannot reach the model server"):
        OpenAICompatible("http://127.0.0.1:9/v1").version()


# ----------------------------------------------------------------------------- models and context


def test_choosing_a_served_model(server):
    client = OpenAICompatible(server, name="LM Studio")
    assert client.chat_models() == ["google/gemma-4-12b", "qwen/qwen3.6-35b-a3b"]  # not the embedding model
    assert backends.pick_model(client, "google/gemma-4-12b") == "google/gemma-4-12b"
    assert backends.pick_model(client, "qwen3.6-35b") == "qwen/qwen3.6-35b-a3b"  # a catalog key matches its id
    with pytest.raises(BackendError, match=r"doesn't serve 'llama'\. Its models: google/gemma-4-12b, qwen/"):
        backends.pick_model(client, "llama")
    FakeServer.models = [("qwen3.6-35b-q4_k_m.gguf", {})]  # llama-server: one model, whatever the name
    assert backends.pick_model(client, "anything") == "qwen3.6-35b-q4_k_m.gguf"
    FakeServer.models = []
    assert backends.pick_model(client, "mlx-community/Qwen3-8B-4bit") == "mlx-community/Qwen3-8B-4bit"


def test_the_context_window_comes_from_the_server(server):
    client = OpenAICompatible(server, name="LM Studio")
    model = "qwen/qwen3.6-35b-a3b"
    ctx, note = backends.context_for(client, model, None)
    assert ctx == backends.DEFAULT_CONTEXT and "doesn't say how large its context window is" in note
    FakeServer.lmstudio = {"id": model, "state": "loaded", "loaded_context_length": 40960}
    assert backends.context_for(client, model, None) == (40960, "")
    assert backends.context_for(client, model, 16384) == (16384, "")  # less is fine: lcode summarizes sooner
    ctx, note = backends.context_for(client, model, 131072)
    assert ctx == 40960 and "capped at the server's context window of 40,960" in note
    FakeServer.lmstudio, FakeServer.props = {}, {"default_generation_settings": {"n_ctx": 65536}}
    assert client.max_context(model) == 65536  # llama-server
    FakeServer.models = [(model, {"max_model_len": 32768})]
    assert client.max_context(model) == 32768  # vLLM


def test_embeddings_and_code_search_model(server):
    client = OpenAICompatible(server)
    assert codesearch.pick_model(client, "auto") == "text-embedding-qwen3-embedding-0.6b"
    assert client.embed("e", ["ab", "abcd"]) == [[2.0, 1.0], [4.0, 1.0]]  # back in input order


# ----------------------------------------------------------------------------- settings


def test_backend_settings(tmp_path):
    assert config.coerce("backend", "LM-Studio") == "lmstudio"
    assert config.coerce("backend", "llama-server") == "llama.cpp"
    with pytest.raises(config.ConfigError, match="backend must be one of ollama, lmstudio"):
        config.coerce("backend", "kobold")
    assert config.coerce("base_url", "localhost:1234") == "http://localhost:1234/v1"
    assert config.coerce("base_url", "https://gpu-box:8000/v1/") == "https://gpu-box:8000/v1"
    client = backends.connect({"backend": "vllm", "base_url": None, "ollama_host": "x"})
    assert (client.name, client.host, client.root) == ("vLLM", "http://localhost:8000/v1", "http://localhost:8000")
    assert backends.connect({"backend": "ollama", "ollama_host": "http://h:11434"}).kind == "ollama"
    with pytest.raises(BackendError, match="needs the server's address"):
        backends.connect({"backend": "openai", "base_url": None, "ollama_host": "x"})


# ----------------------------------------------------------------------------- end to end


def test_a_session_uses_tools_through_the_server(make_agent, server, repo):
    agent = make_agent()
    agent.ollama = OpenAICompatible(server, name="LM Studio")
    FakeServer.replies = [
        sse(call_piece(0, "call_1", "read_file", '{"path": "README.md"}'), USAGE),
        sse(delta(content="The README says "), delta(content="Demo."), USAGE),
    ]
    agent.run_turn("what does the readme say?")
    first, second = chats()
    assert first["model"] == "lcode-qwen3.6-35b" and first["messages"][0]["role"] == "system"
    assert any(t["function"]["name"] == "read_file" for t in first["tools"])
    tool_result = second["messages"][-1]
    assert tool_result["role"] == "tool" and tool_result["tool_call_id"] == "call_1"
    assert "# Demo" in tool_result["content"]
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_1"
    assert agent.messages[-1] == {"role": "assistant", "content": "The README says Demo."}


def test_lcode_runs_against_a_configured_server(server, repo, tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(f'backend = "lmstudio"\nbase_url = "{server}"\nmodel = "gemma-4"\nmemory = "off"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    out = Console(file=io.StringIO(), width=200)
    monkeypatch.setattr(cli, "console", out)
    FakeServer.lmstudio = {"loaded_context_length": 24576}
    cli.main(["models"])
    assert "Models served by LM Studio at" in out.file.getvalue() and "google/gemma-4-12b" in out.file.getvalue()
    FakeServer.replies = [sse(delta(content="Hello from the server."), USAGE)]
    cli.main(["-p", "say hello", "-r", str(repo), "--no-mcp"])
    [request] = chats()
    assert request["model"] == "google/gemma-4-12b"  # "gemma-4" picked the served model it names
    assert "Hello from the server." in out.file.getvalue()
    with pytest.raises(SystemExit, match="1"):
        cli.main(["setup"])
    assert "lcode setup downloads models into Ollama, but lcode uses LM Studio" in out.file.getvalue()
