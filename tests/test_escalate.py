"""Ask a stronger model when stuck (#117): opt-in, asked each time, with the cost shown."""

import http.server
import json
import threading

import pytest

from conftest import call, output, reply
from lcode import bench, escalate
from lcode.config import ConfigError, coerce

FAIL = "python3 -c \"import sys; print('FAIL: test_average (expected 4, got 3)'); sys.exit(1)\""
ADVICE = "1. The average divides by len(values) + 1 (stats.py:3).\n2. Divide by len(values).\n3. Run the tests."


def text(agent):
    return " ".join(output(agent).split())


@pytest.fixture
def advisor(monkeypatch):
    """The stronger model's answers, and what it was sent."""
    sent = []

    def advise(agent, target, brief):
        sent.append((target, brief))
        return ADVICE, {"in": 2400, "out": 120}

    monkeypatch.setattr(escalate, "advise", advise)
    return sent


# --------------------------------------------------------------------------------------------- settings


def test_which_model():
    t = escalate.target("anthropic/claude-sonnet-4-5")
    assert (t.provider, t.model, t.url, t.key_env) == (
        "anthropic",
        "claude-sonnet-4-5",
        "https://api.anthropic.com/v1",
        "ANTHROPIC_API_KEY",
    )
    assert t.cloud and t.where == "api.anthropic.com" and t.label == "claude-sonnet-4-5 (Anthropic)"
    local = escalate.target("ollama/qwen3.8-27b")
    assert not local.cloud and local.local and local.where == "this machine" and local.url == ""
    other = escalate.target("ollama/qwen3.8-27b", "http://gpu-box:11434")
    assert not other.cloud and other.where == "gpu-box:11434"
    assert escalate.target("qwen3.8-27b", "http://gpu-box:11434").provider == "ollama"
    assert escalate.target("custom/big", "https://llm.example.com/v1").cloud
    assert escalate.target("") is None and escalate.target("off") is None
    for bad, why in (("gpt-5", "needs a provider"), ("acme/x", "needs a provider"), ("custom/x", "escalate_url")):
        with pytest.raises(escalate.EscalateError, match=why):
            escalate.target(bad)


def test_the_settings_are_checked():
    assert coerce("escalate_model", "xai/grok-4") == "xai/grok-4"
    assert coerce("escalate_price", "3/15") == "3/15"
    assert coerce("escalate_url", "gpu-box:11434") == "http://gpu-box:11434"
    with pytest.raises(ConfigError, match="provider"):
        coerce("escalate_model", "acme/x")
    with pytest.raises(ConfigError, match="3/15"):
        coerce("escalate_price", "cheap")


def test_keys_come_from_the_environment(monkeypatch, make_agent):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(escalate.EscalateError, match="set XAI_API_KEY in your environment"):
        escalate.client(escalate.target("xai/grok-4"), None)
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-a-real-key")  # lcode-allow: a fake key in a test
    talk = escalate.client(escalate.target("xai/grok-4"), None)
    assert talk.host == "https://api.x.ai/v1" and talk.headers == {"Authorization": "Bearer xai-test-not-a-real-key"}
    agent = make_agent()
    assert escalate.client(escalate.target("ollama/qwen3.8-27b"), agent.ollama) is agent.ollama


def test_cost():
    cloud = escalate.target("anthropic/claude-sonnet-4-5")
    assert escalate.cost_line(cloud, 10_000, (3.0, 15.0)) == (
        "about $0.030 to $0.091 (10.0K tokens in at $3/M, up to 4.1K out at $15/M)"
    )
    assert "set escalate_price" in escalate.cost_line(cloud, 10_000, None)
    assert escalate.cost_line(escalate.target("ollama/qwen3.8-27b"), 10_000, None) == "free: it runs on this machine"
    assert escalate.parse_price("$1.25/$10") == (1.25, 10.0) and escalate.parse_price("") is None


# --------------------------------------------------------------------------------------------- what's sent


def test_the_brief_holds_what_the_advisor_needs_and_no_more(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[call("read_file", path="src/pkg/math.py")]),
            reply(tool_calls=[call("edit_file", path="src/pkg/math.py", old_string="a - b", new_string="a + b")]),
            reply("Done."),
        ]
    )
    agent.run_turn("make sub add")
    brief, names = escalate.brief(agent, "it kept failing", failure="FAIL: test_sub")
    assert "# What the user asked (latest last)\nmake sub add" in brief
    assert "# Why it's stuck\nit kept failing\n\nFAIL: test_sub" in brief
    assert "- edit_file(" in brief and "# The files involved (as they are now)" in brief
    assert "return a + b" in brief and names == ["src/pkg/math.py"]
    assert "README" not in brief  # files it didn't touch stay home
    assert agent.messages[0]["content"][:200] not in brief  # nor the system prompt


def test_big_files_are_cut_to_the_budget(make_agent, monkeypatch):
    agent = make_agent()
    for i in range(3):
        (agent.cwd / f"big{i}.py").write_text("x = 1\n" * 4000)
        agent.tools.before[f"big{i}.py"] = ""
    monkeypatch.setattr(escalate, "BUDGET", 30_000)
    brief, names = escalate.brief(agent, "stuck")
    assert len(brief) <= 30_000 + 2000 and len(names) == 1 and "more characters]" in brief


# --------------------------------------------------------------------------------------------- asking


def test_nothing_goes_without_asking(make_agent, advisor):
    agent = make_agent(escalate_model="anthropic/claude-sonnet-4-5", escalate_price="3/15")
    agent.messages.append({"role": "user", "content": "fix the average"})
    agent.interactive = True
    answers = iter(["s", "n"])
    agent.perms.read_line = lambda question: next(answers)
    assert escalate.run(agent, "the checks kept failing") is None
    assert not advisor and not agent.escalations
    shown = text(agent)
    assert (
        "Ask claude-sonnet-4-5 for help?" in shown
        and "Where: claude-sonnet-4-5 (Anthropic), at api.anthropic.com" in shown
    )
    assert "Cost: about $" in shown and "What would be sent" in shown and "fix the average" in shown


def test_asked_in_any_permission_mode(make_agent, advisor):
    agent = make_agent(mode="yolo", escalate_model="ollama/qwen3.8-27b")
    agent.messages.append({"role": "user", "content": "fix it"})
    agent.interactive = True
    questions = []
    agent.perms.read_line = lambda q: questions.append(q) or "y"
    advice = escalate.run(agent, "it kept looping")
    assert len(questions) == 1 and advice.startswith("[lcode] You were stuck (it kept looping)")
    assert ADVICE in advice and "qwen3.8-27b (Ollama)" in advice
    assert agent.escalations == [
        {"model": "qwen3.8-27b (Ollama)", "why": "it kept looping", "tokens_in": 2400, "tokens_out": 120, "seconds": 0}
    ]
    assert "qwen3.8-27b answered in 0s (2.4K tokens in, 120 out). Its advice goes to the model." in text(agent)


def test_nobody_to_ask_means_no(make_agent, advisor):
    agent = make_agent(escalate_model="openai/gpt-5")
    agent.interactive = False
    assert escalate.run(agent, "stuck") is None and not advisor
    assert "nobody can be asked in this run" in text(agent)
    agent.escalate_approved = True  # lcode -p --escalate
    assert escalate.run(agent, "stuck") is not None and advisor


def test_a_failed_request_is_reported_and_the_work_goes_on(make_agent, monkeypatch):
    def advise(agent, target, brief):
        raise escalate.EscalateError("Anthropic error 401: invalid x-api-key")

    monkeypatch.setattr(escalate, "advise", advise)
    agent = make_agent(escalate_model="anthropic/claude-sonnet-4-5")
    agent.escalate_approved = True
    assert escalate.run(agent, "stuck") is None
    assert "Asking claude-sonnet-4-5 failed: Anthropic error 401" in text(agent)


# --------------------------------------------------------------------------------------------- when


def test_when_it_loops(make_agent, advisor):
    same = reply(tool_calls=[call("read_file", path="README.md")])
    # lcode warns the model at the third identical call and stops the request at the fifth
    agent = make_agent([*[same] * 5, reply("Fixed, following the advice.")], escalate_model="ollama/big")
    agent.escalate_approved = True
    agent.run_turn("fix it")
    assert (
        len(advisor) == 1
        and "it kept making the same call with the same result: read_file(README.md)" in (advisor[0][1])
    )
    assert any(m["role"] == "user" and m["content"].startswith("[lcode] You were stuck") for m in agent.messages)
    assert agent.turn_status == "success" and agent.messages[-1]["content"] == "Fixed, following the advice."


def test_without_a_stronger_model_a_loop_stops_as_before(make_agent, advisor):
    same = reply(tool_calls=[call("read_file", path="README.md")])
    agent = make_agent([same] * 5)
    agent.run_turn("fix it")
    assert agent.turn_status == "loop" and not advisor


def test_when_the_checks_keep_failing(make_agent, advisor):
    edit = reply(tool_calls=[call("write_file", path="stats.py", content="x = 1\n")])
    agent = make_agent(
        [edit, reply("Done."), edit, reply("Done again."), edit, reply("Done with the advice."), reply("Still red.")],
        checks=FAIL,
        escalate_model="ollama/big",
    )
    agent.escalate_approved = True
    agent.run_turn("fix the average")
    assert len(advisor) == 1
    why = advisor[0][1]
    assert "failed 2 times in a row" in why and "FAIL: test_average" in why
    checks_run = [c for c in agent.check_results if c["by"] == "lcode"]
    assert len(checks_run) == 3  # two rounds, then one more after the advice
    assert agent.messages[-1]["content"] == "Still red."  # no second escalation: the model says what's left


def test_at_most_once_per_request(make_agent, advisor):
    same = reply(tool_calls=[call("read_file", path="README.md")])
    agent = make_agent([same] * 10, escalate_model="ollama/big")
    agent.escalate_approved = True
    agent.run_turn("fix it")
    assert len(advisor) == 1 and agent.turn_status == "loop"


def test_not_for_subagents_or_plans(make_agent):
    agent = make_agent(escalate_model="ollama/big")
    assert agent.can_escalate()
    agent.allowed_tools = {"read_file"}
    assert not agent.can_escalate()
    agent.allowed_tools = None
    agent.perms.mode = "plan"
    assert not agent.can_escalate()
    from lcode import repl

    repl.handle_command(agent, "/escalate", None)
    assert "Leave plan mode first" in text(agent)
    agent.perms.mode = "ask"
    agent.settings.escalate_model = "off"
    assert not agent.can_escalate()


def test_the_escalate_command(make_agent, advisor, monkeypatch):
    from lcode import repl

    agent = make_agent()
    repl.handle_command(agent, "/escalate", None)
    assert "No stronger model is set" in text(agent)
    agent.settings.escalate_model = "ollama/big"
    agent.escalate_approved = True
    repl.handle_command(agent, "/escalate", None)
    assert "Nothing to ask about yet" in text(agent)
    agent.messages.append({"role": "user", "content": "the window still grows"})
    ran = []
    monkeypatch.setattr(repl, "run_safely", lambda agent, request: ran.append(request))
    repl.handle_command(agent, "/escalate it still grows", None)
    assert ran and ran[0].startswith("it still grows\n\n[lcode] You were stuck (the user says: it still grows)")


# --------------------------------------------------------------------------------------------- the request itself


@pytest.fixture
def openai_server():
    """An OpenAI-compatible server that records the requests it gets."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            data = json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": ADVICE}}],
                    "usage": {"prompt_tokens": 3100, "completion_tokens": 85},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    server.shutdown()


def test_the_request_to_an_openai_api(make_agent, openai_server, monkeypatch):
    url, seen = openai_server
    monkeypatch.setenv("LCODE_ESCALATE_API_KEY", "test-key")  # lcode-allow: a fake key in a test
    agent = make_agent(escalate_model="custom/big-model", escalate_url=url)
    advice, usage = escalate.advise(agent, escalate.target("custom/big-model", url), "the brief")
    assert advice == ADVICE and usage == {"in": 3100, "out": 85}
    request = seen[0]
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer test-key"
    body = request["body"]
    assert body["model"] == "big-model" and body["max_tokens"] == escalate.MAX_OUTPUT
    assert [m["role"] for m in body["messages"]] == ["system", "user"] and body["messages"][1]["content"] == "the brief"
    assert "tools" not in body and "chat_template_kwargs" not in body  # cloud APIs refuse unknown fields


# --------------------------------------------------------------------------------------------- the benchmark


def test_a_retry_in_the_benchmark_with_advice(make_agent, advisor, monkeypatch):
    from conftest import FakeOllama

    tries = []

    def check(folder, answer):
        tries.append(answer)
        return bench.Check(len(tries) > 1, "fixed" if len(tries) > 1 else "still broken")

    task = bench.Task("t", "a task", "fix it", {"a.py": "x = 1\n"}, check)
    settings = make_agent(escalate_model="ollama/big").settings
    ollama = FakeOllama([reply("Done."), reply("Done, with the advice.")])
    result = bench.run_task(ollama, settings, task, 60, make_agent().console, False, {}, None, retry=True)
    assert result.passed and result.first_passed is False and result.escalations == 1
    sent = [m["content"] for m in ollama.payloads[1]["messages"] if m["role"] == "user"][-1]
    assert sent.startswith(bench.RETRY) and ADVICE in sent


def test_a_local_model_is_found_by_its_catalog_name(make_agent):
    agent = make_agent(escalate_model="ollama/qwen3.6-35b")
    agent.ollama.chat_replies = [ADVICE]
    advice, _ = escalate.advise(agent, escalate.target("ollama/qwen3.6-35b"), "the brief")
    asked = agent.ollama.chats[-1]
    assert advice == ADVICE and asked["model"] == "lcode-qwen3.6-35b"  # the installed tag of the catalog key
    assert asked["options"]["num_predict"] == escalate.MAX_OUTPUT and asked["think"] is True
    with pytest.raises(escalate.EscalateError, match="not installed"):
        escalate.advise(agent, escalate.target("ollama/qwen9-999b"), "the brief")
