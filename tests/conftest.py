from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from lcode.agent import Agent, Settings


class FakeOllama:
    """Replays scripted chat responses and records what the agent sent."""

    def __init__(self, scripts: list[list[dict]] | None = None, max_ctx: int = 262144):
        self.scripts = list(scripts or [])
        self.payloads: list[dict] = []
        self.max_ctx = max_ctx
        self.host = "http://fake"

    def chat_stream(self, payload: dict):
        self.payloads.append(payload)
        yield from self.scripts.pop(0)

    def max_context(self, model: str) -> int:
        return self.max_ctx

    def installed_names(self) -> set[str]:
        return {"lcode-qwen3.6-35b", "qwen3.6:35b-a3b-coding", "custom:7b"}

    def load(self, *args, **kwargs) -> None:
        pass

    def unload(self, model: str) -> None:
        pass

    def running(self) -> list[dict]:
        return [{"name": "lcode-qwen3.6-35b:latest", "size": 23_000_000_000, "size_vram": 11_500_000_000}]


def reply(content: str = "", tool_calls: list[dict] | None = None, thinking: str = "") -> list[dict]:
    message: dict = {"role": "assistant", "content": content}
    if thinking:
        message["thinking"] = thinking
    if tool_calls:
        message["tool_calls"] = tool_calls
    return [
        {"message": message, "done": False},
        {"message": {}, "done": True, "prompt_eval_count": 1000, "eval_count": 50, "eval_duration": 1_000_000_000},
    ]


def call(name: str, **arguments) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "math.py").write_text(
        "def add(a, b):\n    return a + b   \n\n\ndef sub(a, b):\n    return a - b\n"
    )
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "add.py").write_text("def add(): pass\n")
    (tmp_path / "README.md").write_text("# Demo\n")
    return tmp_path.resolve()


@pytest.fixture
def make_agent(repo: Path, monkeypatch, tmp_path_factory):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path_factory.mktemp("state"))

    def factory(scripts=None, mode: str = "yolo", **settings) -> Agent:
        console = Console(file=io.StringIO(), width=100, force_terminal=False)
        opts = {"model": "lcode-qwen3.6-35b", "context": 65536, "permission_mode": mode, **settings}
        return Agent(FakeOllama(scripts), Settings(**opts), repo, console=console)

    return factory


@pytest.fixture
def agent(make_agent) -> Agent:
    return make_agent()


def output(agent: Agent) -> str:
    return agent.console.file.getvalue()
