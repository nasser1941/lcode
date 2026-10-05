import json

import pytest

from conftest import FakeOllama, call, reply
from lcode import api, cli, config
from lcode.hardware import Hardware
from lcode.ollama import OllamaError
from lcode.permissions import NOT_ASKED

HW = Hardware("linux", "x", 31, "GPU", 12)


@pytest.fixture
def server(repo, tmp_path_factory, monkeypatch):
    """lcode's config and a scripted model, for sessions opened the way programs and `lcode -p` open them."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    config.save({"model": "qwen3.6-35b", "context": 32768, "memory": "off", "lsp": "off"})
    fake = FakeOllama()
    monkeypatch.setattr(cli, "Ollama", lambda host: fake)
    monkeypatch.setattr(cli, "detect", lambda: HW)
    monkeypatch.setattr("lcode.hardware.detect", lambda: HW)
    monkeypatch.setattr(api, "check_server", lambda *args: "0.32.0")
    return fake


def test_run_reports_the_answer_tools_files_and_usage(server, repo):
    server.scripts = [
        reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]),
        reply(tool_calls=[call("read_file", path="README.md")]),
        reply(tool_calls=[call("edit_file", path="README.md", old_string="Demo", new_string="Demo app")]),
        reply("Done: wrote notes.md and renamed the README title."),
    ]
    with api.Session(repo, permission_mode="auto-edit") as session:
        result = session.run("Write notes and rename the title")
    assert result.ok and result.exit_code == 0 and result.text.startswith("Done: wrote notes.md")
    assert [c["name"] for c in result.tool_calls] == ["write_file", "read_file", "edit_file"]
    assert result.tool_calls[0]["arguments"] == {"path": "notes.md", "content": "hi\n"} and result.tool_calls[0]["id"]
    assert sorted((f["status"], f["path"]) for f in result.files_changed) == [("A", "notes.md"), ("M", "README.md")]
    assert result.usage == {"requests": 4, "prompt_tokens": 4000, "output_tokens": 200}
    data = result.to_json()
    assert data["type"] == "result" and data["schema"] == api.SCHEMA_VERSION and data["model"] == "lcode-qwen3.6-35b"
    json.dumps(data)  # serializable


def test_permission_requests_go_to_approve(server, repo):
    asked = []
    server.scripts = [
        reply(tool_calls=[call("bash", command="touch made-by-bash")]),
        reply(tool_calls=[call("bash", command="touch second")]),
        reply("ok"),
    ]
    with api.Session(repo, permission_mode="ask", approve=lambda r: asked.append(r) or len(asked) == 1) as session:
        result = session.run("make files")
    assert asked[0] == {
        "kind": "bash",
        "title": f"Run command (in {repo})",
        "target": "touch made-by-bash",
        "key": "bash:touch",
    }
    assert (repo / "made-by-bash").exists() and not (repo / "second").exists()
    assert result.tool_calls[1]["output"] == NOT_ASKED and result.tool_calls[1]["error"] is True
    assert result.tool_calls[0]["error"] is False
    server.scripts = [reply(tool_calls=[call("write_file", path="x.txt", content="x")]), reply("ok")]
    with api.Session(repo, permission_mode="ask") as session:  # no approve: refused
        assert session.run("write x").tool_calls[0]["output"] == NOT_ASKED and not (repo / "x.txt").exists()


def test_limits_and_failures(server, repo):
    server.scripts = [reply(tool_calls=[call("read_file", path="README.md")]) for _ in range(3)]
    with api.Session(repo, max_steps=2, allowed_tools=["read_file", "grep"]) as session:
        assert {s["function"]["name"] for s in session.agent.tool_schemas()} == {"read_file", "grep"}
        result = session.run("loop")
    assert result.status == "max_steps" and result.exit_code == 3 and len(result.tool_calls) == 2

    def broken(payload, on_open=None):
        raise OllamaError("model 'x' not found")
        yield

    server.chat_stream = broken
    with api.Session(repo) as session:
        result = session.run("hi")
    assert (result.status, result.error, result.exit_code) == ("error", "model 'x' not found", 1)
    with pytest.raises(api.SetupError, match=r"unknown tool\(s\) nope; the tools are: "):
        api.Session(repo, allowed_tools=["read_file", "nope"])
    with pytest.raises(api.SetupError, match="not a directory"):
        api.Session(repo / "missing")


def test_stream_yields_events_then_the_result(server, repo):
    server.scripts = [reply(tool_calls=[call("read_file", path="README.md")]), reply("It says Demo.")]
    seen = []
    with api.Session(repo, on_event=seen.append) as session:
        events = list(session.stream("What does the README say?"))
    assert [e["type"] for e in events] == ["assistant", "tool_result", "assistant", "result"]
    assert events[0]["tool_calls"][0]["name"] == "read_file" and "# Demo" in events[1]["output"]
    assert events[1]["id"] == events[0]["tool_calls"][0]["id"] and events[1]["error"] is False
    assert events[-1]["status"] == "success" and events[-1]["text"] == "It says Demo."
    assert [e["type"] for e in seen] == ["assistant", "tool_result", "assistant"]  # on_event saw them too
    with api.Session(repo) as session:  # a session is one conversation
        server.scripts = [reply("first"), reply("second")]
        session.run("one")
        session.run("two")
        assert [m["content"] for m in session.messages if m["role"] == "user"] == ["one", "two"]


# ----------------------------------------------------------------------------- lcode -p --output


def test_output_json(server, repo, capsys):
    server.scripts = [reply(tool_calls=[call("write_file", path="a.txt", content="a")]), reply("Wrote a.txt.")]
    cli.main(["-p", "write a.txt", "-r", str(repo), "--output", "json", "--auto-edit", "--no-mcp"])
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success" and data["text"] == "Wrote a.txt."
    assert data["files_changed"] == [{"status": "A", "path": "a.txt"}]


def test_output_stream_json_refuses_what_needs_asking(server, repo, capsys):
    server.scripts = [reply(tool_calls=[call("bash", command="touch y")]), reply("Couldn't run it.")]
    with pytest.raises(SystemExit) as stopped:
        cli.main(["-p", "touch y", "-r", str(repo), "--output", "stream-json", "--no-mcp", "--max-steps", "1"])
    assert stopped.value.code == 3  # stopped after one step
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in lines] == ["start", "assistant", "tool_result", "result"]
    assert lines[0]["model"] == "lcode-qwen3.6-35b" and lines[2]["output"] == NOT_ASKED
    assert lines[-1]["status"] == "max_steps" and not (repo / "y").exists()


def test_output_needs_a_request(server, repo, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--output", "json", "-r", str(repo)])
    assert "need a request" in capsys.readouterr().out
    monkey_cfg = config.CONFIG_PATH
    monkey_cfg.write_text('model = "nothing-like-this"\n')
    with pytest.raises(SystemExit) as stopped:
        cli.main(["-p", "hi", "-r", str(repo), "--output", "json", "--no-mcp"])
    assert stopped.value.code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error" and "nothing-like-this" in data["error"]
