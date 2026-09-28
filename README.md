<p align="center">
  <img src="docs/assets/logo.svg" width="96" alt="lcode logo">
</p>

<h1 align="center">lcode</h1>

<p align="center">
  <b>A coding agent that runs entirely on your own machine.</b><br>
  Open-weight models · up to 256K tokens of context · your NVIDIA GPU or Apple Silicon Mac · no API keys, no cloud
</p>

<p align="center">
  <a href="https://github.com/nasser1941/lcode/actions/workflows/ci.yml"><img src="https://github.com/nasser1941/lcode/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://nasser1941.github.io/lcode/"><img src="https://github.com/nasser1941/lcode/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20(Apple%20Silicon)-lightgrey.svg" alt="Linux and macOS">
</p>

<p align="center">
  <a href="https://nasser1941.github.io/lcode/"><b>Documentation</b></a> ·
  <a href="https://nasser1941.github.io/lcode/installation/">Install</a> ·
  <a href="https://nasser1941.github.io/lcode/models/">Models</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img src="docs/assets/demo.svg" alt="lcode finding and fixing a bug, then running the tests" width="820">
</p>

Point lcode at a repository and talk to it in your terminal. It explores the code, answers
questions with `file:line` references, writes and edits files, runs your scripts and tests, and
keeps going until the task is done, asking before it changes anything. The model runs locally
through [Ollama](https://ollama.com), so your code never leaves your machine.

## Features

- **A real agent.** Reads, searches, edits and runs commands in a loop, checks its own work, and
  plans multi-step tasks with a visible todo list.
- **Long context.** 256K tokens with the default model (1M with Nemotron). You choose the window.
- **Hardware-aware.** Detects your NVIDIA GPU or Apple Silicon Mac and picks the best model and the
  largest context that fits: `lcode setup` does it in one step.
- **Safe by default.** Every edit is shown as a diff and every command that isn't read-only needs
  your approval. `auto-edit` and `yolo` modes when you want speed.
- **Bring your own model.** Eight curated open-weight models, or any Ollama model with tool calling.
- **Private.** No telemetry, no accounts, no API keys.

## Install

**Ubuntu / Linux** (NVIDIA GPU recommended) and **macOS** on Apple Silicon (M1–M4):

```bash
curl -fsSL https://nasser1941.github.io/lcode/install.sh | bash
```

The installer sets up lcode with its own Python via [uv](https://docs.astral.sh/uv/), checks for
[Ollama](https://ollama.com) (0.30+) and runs `lcode setup`, which picks a model for your hardware
and downloads it. Prefer manual steps? See the
[installation guide](https://nasser1941.github.io/lcode/installation/), or:

```bash
uv tool install git+https://github.com/nasser1941/lcode
lcode setup
```

## Quickstart

```bash
cd ~/code/your-project
lcode
```

```text
❯ /init                                   # study the repo and write AGENTS.md for future sessions
❯ How does authentication work here? Cite files.
❯ Write scripts/dedupe.py that removes duplicate rows from @data/users.csv by email, and run it
❯ The tests in tests/test_parser.py fail. Find out why and fix it.
```

| | |
|---|---|
| `lcode -p "…"` | one request, no interaction (scripts, hooks) |
| `lcode -c` | continue the last session in this directory |
| `lcode --model qwen3.5-9b --context 128k` | pick a model and context window for this session |
| `lcode models` / `lcode doctor` | what fits this machine / check the installation |
| `/model`, `/ctx 128k`, `/compact`, `/help` | switch model, resize context, summarize, list commands |

## Models

| Key | Model | Download | Max context | |
|---|---|---|---|---|
| `qwen3.6-35b` | Qwen3.6 35B-A3B Coding (MoE, 3B active) | 22.6 GB | 256K | **default**, tested |
| `qwen3.8-27b` | Qwen3.8 27B (dense) | 17.7 GB | 256K | |
| `qwen3.6-27b` | Qwen3.6 27B Coding (dense) | 17.8 GB | 256K | |
| `laguna-xs-2.1` | Poolside Laguna XS 2.1 (MoE, 3B active) | 20.3 GB | 256K | |
| `nemotron-3.5-lightning` | NVIDIA Nemotron 3.5 Lightning (hybrid MoE) | 25.4 GB | 1M | |
| `gpt-oss-20b` | OpenAI gpt-oss 20B (MoE) | 13.8 GB | 128K | |
| `qwen3.5-9b` | Qwen3.5 9B (dense) | 6.6 GB | 256K | tested |
| `qwen3.5-4b` | Qwen3.5 4B (dense) | 3.4 GB | 256K | tested |

What `lcode setup` picks for common machines:

| Machine | Model | Context |
|---|---|---|
| Mac with M4, 16 GB | qwen3.5-9b | 64K |
| Mac with M4 / M4 Pro, 24 GB | qwen3.5-9b | 256K |
| Mac with M4 Pro / M4 Max, 36 GB | qwen3.6-35b | 64K |
| Mac with M4 Pro, 48 GB · M4 Max, 64 GB+ | qwen3.6-35b | 256K |
| NVIDIA 8–24 GB + 32 GB RAM | qwen3.6-35b | 256K |
| NVIDIA 8 GB + 16 GB RAM | qwen3.5-4b | 64K |

On an RTX 4080 Laptop GPU (12 GB) the default model generates 50–60 tokens/s at 256K context, and
qwen3.5-9b 64 tokens/s at 128K. See
[Models & context windows](https://nasser1941.github.io/lcode/models/) for memory estimates and
tuning.

## How it works

lcode sends your request, the repository layout and a set of tool definitions to the model; runs the
tools the model calls (read, edit, grep, glob, bash, todo); feeds the results back; and repeats until
the model answers. It sizes models to your memory from each model's KV-cache footprint, uses
text-only model variants to free GPU memory, and summarizes the conversation when the context window
fills up. Details: [How it works](https://nasser1941.github.io/lcode/how-it-works/).

## Roadmap

- More models in the catalog, with community test reports ([request one](https://github.com/nasser1941/lcode/issues/new?template=model_request.yml))
- Measured Apple Silicon performance numbers
- MCP (Model Context Protocol) tool servers
- Web fetch and search tools
- Packaging on PyPI and Homebrew

## Contributing

Contributions are welcome: bug reports, model test results, docs and code. See
[CONTRIBUTING.md](CONTRIBUTING.md). Please follow the [code of conduct](CODE_OF_CONDUCT.md), and
report security issues privately as described in [SECURITY.md](SECURITY.md).

## Acknowledgements

lcode stands on [Ollama](https://ollama.com) and [llama.cpp](https://github.com/ggml-org/llama.cpp),
the open-weight models from Qwen, Poolside, NVIDIA and OpenAI,
[Rich](https://github.com/Textualize/rich) and
[prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit).

## License

[MIT](LICENSE) © 2026 Naser Derakhshan
