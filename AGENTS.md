# AGENTS.md

Guidance for coding agents (including lcode itself) working on this repository.

## Overview

lcode is a terminal coding agent that runs open-weight models locally through Ollama. Python 3.10+,
packaged with hatchling, managed with uv. Runtime dependencies: requests, rich, prompt_toolkit
(and tomli on Python 3.10).

## Commands

```bash
uv sync --group dev                 # set up .venv
uv run pytest                       # tests (no Ollama needed)
uv run ruff check . && uv run ruff format --check .
uv run --group docs mkdocs build --strict   # docs
uv run lcode doctor                 # check a local install against a running Ollama
```

## Architecture

- `src/lcode/cli.py` — entry point `lcode.cli:main`; chat by default, subcommands `setup`, `models`,
  `doctor`, `config`. Resolves catalog keys to installed Ollama models (`resolve_model`) and picks
  the context window (`choose_context`).
- `src/lcode/repl.py` — prompt_toolkit session, slash commands (`handle_command`), status bar.
- `src/lcode/agent.py` — `Agent.run_turn` loop: stream a response (`assistant_step`), run tool calls,
  append `role: tool` messages, repeat. Also system prompt, sessions and compaction.
- `src/lcode/tools.py` — tool JSON schemas (`SCHEMAS`) and implementations (`Toolbox.t_<name>`).
  Tools return strings; errors start with `Error:` and never raise into the loop.
- `src/lcode/catalog.py` + `src/lcode/models.toml` — model specs and memory estimates;
  `src/lcode/hardware.py` detects NVIDIA VRAM or Apple unified memory.

## Conventions

- Line length 120, ruff rules in `pyproject.toml`. Keep modules dependency-light.
- User-facing text: plain, specific, and actionable (say what to run to fix a problem).
- Every behavior change needs a test; use `FakeOllama` and the `make_agent` fixture from
  `tests/conftest.py` to script model responses.
- Update `docs/` and `CHANGELOG.md` (*Unreleased*) for user-facing changes.
- Never commit secrets, personal paths or real user data.
