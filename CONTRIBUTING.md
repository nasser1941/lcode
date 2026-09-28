# Contributing to lcode

Thanks for helping make local coding agents better! Bug reports, model test results, docs fixes and
code are all welcome.

## Ways to contribute

- **Report a bug** — open an [issue](https://github.com/nasser1941/lcode/issues/new/choose) and
  include the output of `lcode doctor`.
- **Test a model** — try an untested catalog model (or a new one) on your hardware and report how it
  went with the *Model request / report* issue form. Real-world results are the most valuable
  contribution right now.
- **Improve the docs** — every page on the [docs site](https://nasser1941.github.io/lcode/) has an
  edit button.
- **Write code** — look for issues labelled
  [`good first issue`](https://github.com/nasser1941/lcode/labels/good%20first%20issue) or
  [`help wanted`](https://github.com/nasser1941/lcode/labels/help%20wanted). For larger changes,
  please open an issue first so we can agree on the approach.

## How changes get in

`main` is protected: nobody pushes to it directly. Fork the repository, work on a branch, and open a
pull request. CI must pass and a maintainer reviews and merges it (squash merge).

```bash
git clone https://github.com/<you>/lcode && cd lcode
git checkout -b fix-something
```

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.10+. Ollama is only needed to try lcode
against a real model; the test suite doesn't need it.

```bash
uv sync --group dev          # creates .venv with lcode (editable) + pytest + ruff
uv run lcode --version       # run your working copy
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
```

Optional: `uvx pre-commit install` runs the linters and a secret scanner before each commit.

Docs live in `docs/` and use [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/):

```bash
uv run --group docs mkdocs serve   # http://127.0.0.1:8000
```

## Project layout

| Path | What it is |
|---|---|
| `src/lcode/cli.py` | Command-line entry point and the `setup`, `models`, `doctor`, `config` subcommands |
| `src/lcode/repl.py` | Interactive prompt, slash commands, status bar |
| `src/lcode/agent.py` | The agent loop: system prompt, streaming, tool dispatch, context compaction |
| `src/lcode/tools.py` | Tools the model can call (read/edit/write files, grep, glob, bash, todos) |
| `src/lcode/permissions.py` | Permission prompts and the read-only command allowlist |
| `src/lcode/catalog.py` + `models.toml` | Model catalog and memory-fit estimates |
| `src/lcode/hardware.py` | GPU / unified-memory detection (Linux + NVIDIA, macOS Apple Silicon) |
| `src/lcode/ollama.py` | Minimal Ollama HTTP client |
| `src/lcode/config.py` | `~/.config/lcode/config.toml` handling |

## Adding a model to the catalog

Models live in [`src/lcode/models.toml`](https://github.com/nasser1941/lcode/blob/main/src/lcode/models.toml). To add one:

1. Make sure it exists in the [Ollama library](https://ollama.com/library) and supports **tools**.
2. Fill in every field. `kv_kib_per_token` is the KV-cache size per token of context at f16:

   ```
   attention layers × KV heads × (key_length + value_length) × 2 bytes ÷ 1024
   ```

   Read the numbers from `ollama show <tag> -v` (or the GGUF metadata). Count only layers that keep
   a full KV cache: hybrid models (Mamba, linear attention, sliding-window) have far fewer. Set
   `kv_estimated = true` if you had to assume the layout.
3. Test it: `lcode setup <key>` then a real multi-step task (explore a repo, edit a file, run a
   command). Check `ollama ps` for how much landed on the GPU.
4. Set `tested = true` only if tool calling works reliably, and describe your hardware and results
   in the pull request.

Catalog order matters: `lcode setup` recommends the first *tested* model that fits a machine, and
otherwise the first model in catalog order that fits with a useful context window.

## Pull request checklist

- [ ] `uv run pytest` and `uv run ruff check .` pass
- [ ] New behavior has tests (the agent loop can be tested with the scripted `FakeOllama` in `tests/conftest.py`)
- [ ] User-facing changes are documented in `docs/` and noted under *Unreleased* in `CHANGELOG.md`
- [ ] No secrets, tokens, personal paths or private data in code, tests, docs or screenshots

Commit messages: short imperative summary (`Add laguna-xs-2.1 to the catalog`), details in the body
if needed. [Conventional Commits](https://www.conventionalcommits.org/) prefixes are welcome but not
required.

## Code of conduct

This project follows the [Contributor Covenant](https://github.com/nasser1941/lcode/blob/main/CODE_OF_CONDUCT.md). By participating you agree to
uphold it.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](https://github.com/nasser1941/lcode/blob/main/LICENSE).
