from __future__ import annotations

import io
import os
import subprocess
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
        self.capabilities: dict[str, list[str]] = {}  # model -> Ollama capabilities, e.g. ["vision"]
        self.chats: list[dict] = []  # non-streaming requests (image descriptions)
        self.loaded = [{"name": "lcode-qwen3.6-35b:latest", "size": 23_000_000_000, "size_vram": 11_500_000_000}]
        self.unloaded: list[str] = []
        self.description = "A login form with a red 'Sign in' button."
        self.chat_replies: list[str] = []  # answers to non-streaming requests, before falling back to description

    def show(self, model: str) -> dict:
        return {"capabilities": self.capabilities.get(model, ["completion", "tools"])}

    def chat(self, payload: dict) -> dict:
        self.chats.append(payload)
        content = self.chat_replies.pop(0) if self.chat_replies else self.description
        return {"message": {"role": "assistant", "content": content}}

    def chat_stream(self, payload: dict, on_open=None):
        self.payloads.append(payload)
        yield from self.scripts.pop(0)

    def max_context(self, model: str) -> int:
        return self.max_ctx

    def installed_names(self) -> set[str]:
        return {"lcode-qwen3.6-35b", "qwen3.6:35b-a3b-coding", "custom:7b"}

    def load(self, *args, **kwargs) -> None:
        pass

    def unload(self, model: str) -> None:
        self.unloaded.append(model)

    def running(self) -> list[dict]:
        return self.loaded


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
        opts = {
            "model": "lcode-qwen3.6-35b",
            "context": 65536,
            "permission_mode": mode,
            "trust_project": True,
            "skills": "all",
        }
        opts.update(settings)
        return Agent(FakeOllama(scripts), Settings(**opts), repo, console=console)

    return factory


@pytest.fixture
def agent(make_agent) -> Agent:
    return make_agent()


def output(agent: Agent) -> str:
    return agent.console.file.getvalue()


def run(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_repo(repo, tmp_path_factory, monkeypatch):
    """The demo repository under git, with one commit on main and git isolated from the user's settings."""
    home = tmp_path_factory.mktemp("home")
    (home / "gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (repo / ".gitignore").write_text("node_modules/\n")
    run(repo, "init", "-q", "-b", "main")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "fix: start the demo")
    return repo


@pytest.fixture
def github(git_repo, tmp_path_factory, monkeypatch):
    """A bare repository as origin, and a stand-in gh that logs its arguments."""
    remote = tmp_path_factory.mktemp("remote") / "demo.git"
    run(remote.parent, "init", "-q", "--bare", "-b", "main", str(remote))
    run(git_repo, "remote", "add", "origin", str(remote))
    run(git_repo, "push", "-q", "-u", "origin", "main")
    bin_dir = tmp_path_factory.mktemp("bin")
    log = bin_dir / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n'
        'if [ "$1 $2" = "pr view" ]; then exit 1; fi\n'
        'if [ "$1 $2" = "pr create" ]; then echo https://github.com/acme/demo/pull/7; fi\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return remote, log
