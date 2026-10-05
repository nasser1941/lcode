import base64
import json
import os
import queue
import threading
import time

import pytest
from rich.console import Console

from conftest import FakeOllama, call, reply
from lcode import acp, api, cli, config
from lcode.hardware import Hardware

HW = Hardware("linux", "x", 31, "GPU", 12)


class Editor:
    """The editor's side of the connection: sends requests, answers lcode's."""

    def __init__(self, server_in, server_out):
        self.to_server = server_in
        self.messages: queue.Queue = queue.Queue()
        self.updates: list[dict] = []
        self.requests: list[dict] = []  # lcode's requests to the editor
        self.choice = "allow"  # the option picked in permission dialogs
        self.buffers: dict[str, str] = {}
        self.next_id = 0
        threading.Thread(target=self._read, args=(server_out,), daemon=True).start()

    def _read(self, stream):
        for line in stream:
            message = json.loads(line)
            if message.get("method") == "session/update":
                self.updates.append(message["params"]["update"])
            elif "method" in message:
                self.requests.append(message)
                self.send({"id": message["id"], **self.answer(message["method"], message["params"])})
            else:
                self.messages.put(message)

    def answer(self, method, params):
        if method == "session/request_permission":
            return {"result": {"outcome": {"outcome": "selected", "optionId": self.choice}}}
        if method == "fs/read_text_file":
            if params["path"] in self.buffers:
                return {"result": {"content": self.buffers[params["path"]]}}
            return {"error": {"code": -32002, "message": "not open"}}
        if method == "fs/write_text_file":
            self.buffers[params["path"]] = params["content"]
            return {"result": {}}
        return {"error": {"code": -32601, "message": "no"}}

    def send(self, message):
        self.to_server.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        self.to_server.flush()

    def request(self, method, params, timeout=20):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        reply = self.messages.get(timeout=timeout)
        assert reply["id"] == self.next_id
        if "error" in reply:
            raise acp.RpcError(reply["error"]["code"], reply["error"]["message"])
        return reply["result"]

    def kinds(self):
        return [u["sessionUpdate"] for u in self.updates]


@pytest.fixture
def editor(repo, tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(config, "STATE_DIR", home / "state")
    config.save({"model": "qwen3.6-35b", "context": 32768, "memory": "off", "lsp": "off"})
    model = FakeOllama()
    monkeypatch.setattr(cli, "Ollama", lambda host: model)
    monkeypatch.setattr("lcode.hardware.detect", lambda: HW)
    monkeypatch.setattr(api, "check_server", lambda *args: "0.32.0")
    to_server_r, to_server_w = os.pipe()
    to_editor_r, to_editor_w = os.pipe()
    conn = acp.Connection(os.fdopen(to_server_r, "rb"), os.fdopen(to_editor_w, "wb"))
    server = acp.Server(conn, Console(file=open(os.devnull, "w")))  # noqa: SIM115
    threading.Thread(target=conn.serve, args=(server.handle,), daemon=True).start()
    client = Editor(os.fdopen(to_server_w, "wb"), os.fdopen(to_editor_r, "rb"))
    client.model, client.server = model, server
    client.request(
        "initialize",
        {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}}},
    )
    yield client
    client.to_server.close()


def new_session(editor, repo, mode="ask"):
    session = editor.request("session/new", {"cwd": str(repo), "mcpServers": []})
    editor.request("session/set_mode", {"sessionId": session["sessionId"], "modeId": mode})
    return session["sessionId"]


def prompt(editor, sid, text):
    return editor.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})


def test_initialize_and_sessions(editor, repo):
    info = editor.server.initialize({"protocolVersion": 1})
    assert info["protocolVersion"] == 1 and info["agentInfo"]["name"] == "lcode"
    assert info["agentCapabilities"]["loadSession"] and info["agentCapabilities"]["promptCapabilities"]["image"]
    session = editor.request("session/new", {"cwd": str(repo), "mcpServers": []})
    assert [m["id"] for m in session["modes"]["availableModes"]] == ["ask", "auto-edit", "plan", "yolo"]
    assert session["modes"]["currentModeId"] == "ask"
    commands = next(u for u in editor.updates if u["sessionUpdate"] == "available_commands_update")
    assert commands["availableCommands"][0]["name"] == "review"
    with pytest.raises(acp.RpcError, match="doesn't support session/fork"):
        editor.request("session/fork", {})
    with pytest.raises(acp.RpcError, match="no session nope"):
        prompt(editor, "nope", "hi")
    with pytest.raises(acp.RpcError, match="cwd must be an absolute path"):
        editor.request("session/new", {"cwd": "relative", "mcpServers": []})


def test_edits_ask_in_the_editor_with_diffs(editor, repo):
    sid = new_session(editor, repo)
    editor.model.scripts = [
        reply(tool_calls=[call("read_file", path="README.md")]),
        reply(tool_calls=[call("edit_file", path="README.md", old_string="Demo", new_string="Demo app")]),
        reply("Renamed it.", thinking="The title should say app."),
    ]
    assert prompt(editor, sid, "rename the title") == {"stopReason": "end_turn"}
    [ask] = [r for r in editor.requests if r["method"] == "session/request_permission"]
    tool_call = ask["params"]["toolCall"]
    assert tool_call["title"] == "Edit README.md" and tool_call["kind"] == "edit"
    assert tool_call["content"][0] == {
        "type": "diff",
        "path": str(repo / "README.md"),
        "oldText": "# Demo\n",
        "newText": "# Demo app\n",
    }
    assert [o["kind"] for o in ask["params"]["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert (repo / "README.md").read_text() == "# Demo app\n"
    assert editor.buffers[str(repo / "README.md")] == "# Demo app\n"  # written through the editor
    started = [u for u in editor.updates if u["sessionUpdate"] == "tool_call"]
    assert [(u["title"], u["kind"], u["status"]) for u in started] == [
        ("Read README.md", "read", "pending"),
        ("Edit README.md", "edit", "pending"),
    ]
    assert started[0]["locations"] == [{"path": str(repo / "README.md")}]
    done = [u for u in editor.updates if u["sessionUpdate"] == "tool_call_update" and u.get("status") == "completed"]
    assert done[-1]["content"][0]["type"] == "diff" and done[-1]["content"][0]["newText"] == "# Demo app\n"
    assert "in_progress" in [u.get("status") for u in editor.updates if u["sessionUpdate"] == "tool_call_update"]
    kinds = editor.kinds()
    assert "agent_thought_chunk" in kinds and kinds.count("agent_message_chunk") == 1
    usage = next(u for u in editor.updates if u["sessionUpdate"] == "usage_update")
    assert usage["size"] == 32768 and usage["used"] > 0


def test_rejecting_and_always_allowing(editor, repo):
    sid = new_session(editor, repo)
    editor.choice = "reject"
    editor.model.scripts = [reply(tool_calls=[call("bash", command="touch made")]), reply("ok")]
    prompt(editor, sid, "make a file")
    result = next(u for u in reversed(editor.updates) if u["sessionUpdate"] == "tool_call_update" and u.get("content"))
    assert (
        result["status"] == "failed" and "The user denied this in the editor" in result["content"][0]["content"]["text"]
    )
    assert not (repo / "made").exists()
    editor.choice = "always"
    editor.model.scripts = [
        reply(tool_calls=[call("bash", command="touch one")]),
        reply(tool_calls=[call("bash", command="touch two")]),
        reply("ok"),
    ]
    prompt(editor, sid, "make two files")
    asked = [r for r in editor.requests if r["method"] == "session/request_permission"]
    assert len(asked) == 2 and (repo / "one").exists() and (repo / "two").exists()  # asked once for touch


def test_the_model_reads_unsaved_changes(editor, repo):
    sid = new_session(editor, repo)
    editor.buffers[str(repo / "README.md")] = "# Demo\n\nNot saved yet.\n"
    editor.model.scripts = [reply(tool_calls=[call("read_file", path="README.md")]), reply("It says not saved.")]
    prompt(editor, sid, "read the readme")
    result = next(u for u in reversed(editor.updates) if u["sessionUpdate"] == "tool_call_update" and u.get("content"))
    assert "Not saved yet." in result["content"][0]["content"]["text"]
    assert (repo / "README.md").read_text() == "# Demo\n"


def test_plans_are_approved_in_the_editor(editor, repo):
    sid = new_session(editor, repo, mode="plan")
    editor.choice = "auto-edit"
    editor.model.scripts = [
        reply(tool_calls=[call("todo_write", todos=[{"content": "Rename the title", "status": "pending"}])]),
        reply(tool_calls=[call("present_plan", title="Rename", plan="1. Edit README.md")]),
        reply("Approved; starting."),
    ]
    prompt(editor, sid, "plan the rename")
    [ask] = [r for r in editor.requests if r["method"] == "session/request_permission"]
    assert ask["params"]["toolCall"]["kind"] == "switch_mode" and ask["params"]["toolCall"]["title"] == "Plan: Rename"
    assert ask["params"]["toolCall"]["content"][0]["content"]["text"] == "1. Edit README.md"
    plan = next(u for u in editor.updates if u["sessionUpdate"] == "plan")
    assert plan["entries"] == [{"content": "Rename the title", "priority": "medium", "status": "pending"}]
    mode = next(u for u in editor.updates if u["sessionUpdate"] == "current_mode_update")
    assert mode["currentModeId"] == "auto-edit" and editor.server.sessions[sid].agent.perms.mode == "auto-edit"


def test_cancelling_stops_the_request(editor, repo):
    sid = new_session(editor, repo)

    def slow():
        yield {"message": {"content": "Once upon"}, "done": False}
        agent = editor.server.sessions[sid].agent
        for _ in range(100):
            if agent.cancel.is_set():
                break
            time.sleep(0.05)
        yield {"message": {"content": " a time"}, "done": False}

    editor.model.scripts = [slow()]
    editor.next_id += 1
    editor.send(
        {
            "id": editor.next_id,
            "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "story"}]},
        }
    )
    time.sleep(0.3)
    editor.send({"method": "session/cancel", "params": {"sessionId": sid}})
    assert editor.messages.get(timeout=10)["result"] == {"stopReason": "cancelled"}


def test_prompts_with_files_images_and_commands(editor, repo):
    sid = new_session(editor, repo)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    editor.model.scripts = [reply("ok")]
    editor.request("session/prompt", {"sessionId": sid, "prompt": [
        {"type": "text", "text": "compare these"},
        {"type": "resource_link", "uri": (repo / "README.md").as_uri(), "name": "README.md"},
        {"type": "resource", "resource": {"uri": "file:///tmp/notes.txt", "text": "remember the milk"}},
        {"type": "image", "data": png, "mimeType": "image/png"},
    ]})  # fmt: skip
    sent = [m["content"] for m in editor.model.payloads[0]["messages"] if m["role"] == "user"][-1]
    assert sent.startswith("compare these\n\n[The user attached README.md]\n```\n# Demo\n```")
    assert "[The user attached /tmp/notes.txt]\n```\nremember the milk\n```" in sent
    saved = list((config.STATE_DIR / "acp-images").glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes().startswith(b"\x89PNG")
    before = len(editor.updates)
    assert prompt(editor, sid, "/review") == {"stopReason": "end_turn"}  # not a git repository
    assert "Nothing to review: this isn't a git repository" in "".join(
        u["content"]["text"] for u in editor.updates[before:] if u["sessionUpdate"] == "agent_message_chunk"
    )


def test_loading_a_session_replays_it(editor, repo):
    sid = new_session(editor, repo)
    editor.model.scripts = [reply(tool_calls=[call("read_file", path="README.md")]), reply("It says Demo.")]
    prompt(editor, sid, "what does the readme say?")
    editor.updates.clear()
    loaded = editor.request("session/load", {"sessionId": sid, "cwd": str(repo), "mcpServers": []})
    assert loaded["modes"]["currentModeId"] == "ask"
    kinds = editor.kinds()
    assert kinds[:4] == ["user_message_chunk", "tool_call", "tool_call_update", "agent_message_chunk"]
    assert editor.updates[0]["content"]["text"] == "what does the readme say?"
    assert editor.updates[3]["content"]["text"] == "It says Demo."
    with pytest.raises(acp.RpcError, match="no saved lcode session nope"):
        editor.request("session/load", {"sessionId": "nope", "cwd": str(repo), "mcpServers": []})
