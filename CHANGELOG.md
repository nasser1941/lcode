# Changelog

All notable changes to lcode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Homebrew: `brew install nasser1941/tap/lcode` on macOS (Apple Silicon and Intel) and Linux. The
  [tap](https://github.com/nasser1941/homebrew-tap) tests the formula on both and updates it
  automatically for each release, about a day after it's published.
- Python 3.14 is supported and tested.

## [0.4.0] - 2026-10-01

### Added

- MCP (Model Context Protocol) servers: the model can use tools from Jira, GitHub, AWS, databases and
  more, with your approval for every call.
  - `lcode mcp add <name>` sets up a ready-made server and tests it: `atlassian`, `aws`,
    `aws-knowledge`, `google-drive`, `grafana`, `gcp`, `github`, `playwright`, `context7`, `sentry`,
    `postgres`, `kubernetes`, `linear` and `notion` (`lcode mcp catalog` lists them).
  - Any other server works too, local (stdio) or remote (Streamable HTTP), configured in
    `~/.config/lcode/mcp.json` in the standard `mcpServers` format, or per project in `.mcp.json`
    (used only after you approve it).
  - Browser sign-in (OAuth 2.1 with PKCE and dynamic client registration) with automatic token
    refresh; `lcode mcp login/logout`.
  - Speaks both the current MCP protocol (2026-07-28) and the earlier `initialize`-based versions.
  - When the tool definitions would take more than 15% of the context window, the model finds tools
    on demand instead (`mcp_tools` setting). `/mcp` shows servers, tools and their context cost;
    `lcode --no-mcp` starts without them.

### Fixed

- `lcode bench`: stopping a run with Ctrl+C no longer leaves the running task's temporary folder
  behind, and the summary marks the model as stopped.
- `lcode bench` no longer lists the model it's about to benchmark as "already loaded".

## [0.3.1] - 2026-09-30

### Added

- `lcode bench` scores models on this machine with eight small coding tasks: fixing a bug, finding
  code, writing a script from a spec, renaming across files, a one-line edit in a long file,
  recovering from failing commands, fixing a function to match its spec and adding a feature across
  files. Each task runs in a temporary folder and has an automatic check, some with hidden tests.
  It reports tasks passed, time, generation and prompt speed, tool-call errors and memory use.
  `lcode bench model-a model-b` compares models, `--json` saves the results (a documented, versioned
  format) and `--markdown` prints a table for a model test report.

## [0.3.0] - 2026-09-30

### Added

- `/undo` takes back the file changes of the last request: lcode saves a checkpoint before the model
  first changes files in a request, and restores edited and deleted files and removes new ones,
  including changes made by shell commands. `/undo` again goes further back, `/rewind N` goes back
  to before request N (optionally removing those requests from the conversation too), and
  `/checkpoints` lists them. Checkpoints live in a separate git repository in lcode's state folder,
  so your own repository is never touched, and work in folders that aren't git repositories.
  Turn them off with `lcode config set checkpoints false`.

## [0.2.2] - 2026-09-30

### Added

- `/context` now lets you change the context window mid-session: it lists the sizes the model
  supports with the memory each needs and whether it fits on the GPU, and you pick one (or type
  `/context 128k`). If the conversation is too long for a smaller size, lcode summarizes it first.
  `/ctx` is a shortcut for the same command.

## [0.2.1] - 2026-09-29

### Fixed

- File edits and writes failed with "IndexError: list index out of range" in the default `ask`
  permission mode, a regression in 0.2.0. `auto-edit` and `yolo` modes were not affected.
- Slow models no longer look stuck: the status line keeps counting the elapsed time while the model
  thinks, answers or silently writes a long file into a tool call, and reminds you that Ctrl+C stops it.

## [0.2.0] - 2026-09-29

### Added

- Web access for the model: `web_search` finds current information (latest releases, docs, error
  messages) through Ollama web search, Brave Search, Tavily or a self-hosted SearXNG, and `web_fetch`
  reads pages as clean text. On by default; `lcode config set web ask|off` or `lcode --no-web` limits
  it. Search API keys are read from environment variables only. Closes #7.

## [0.1.2] - 2026-09-29

### Added

- All eight catalog models are now tested end to end (`qwen3.8-27b`, `qwen3.6-27b`, `laguna-xs-2.1`,
  `nemotron-3.5-lightning` and `gpt-oss-20b` joined the three tested before), with a results table
  in the models guide. `laguna-xs-2.1`'s cache size is now measured rather than estimated.
- When a model fails to load because its context doesn't fit in GPU memory, lcode retries with half the
  context and remembers the size that worked for that model (`~/.local/state/lcode/limits.json`, shown
  by `lcode doctor`), so later sessions don't fail first. Found with `nemotron-3.5-lightning`, whose 1M
  context doesn't fit on a 12 GB GPU.
- Named sessions and a session picker: `/rename <name>` names the current session, `/resume` lists
  saved sessions and resumes the one you pick (by number, name, id or title), `/resume all` shows
  every folder, and `lcode --resume [SESSION]` does the same from the shell. Resuming shows a short
  recap of where you left off.

### Changed

- `lcode setup` recommends `gpt-oss-20b` at 64K (instead of `qwen3.5-4b`) for 8 GB GPUs with 16 GB of
  RAM, and prefers `qwen3.5-9b` over `gpt-oss-20b` where both fit.
- lcode is on PyPI as `lcode-cli`; the installer now installs the latest release from PyPI instead of
  the `main` branch.

### Fixed

- `qwen3.6-35b` could crash Ollama with "CUDA error: an illegal memory access" on 12 GB GPUs: its
  prompt batch of 1024 left too little VRAM for the model's speculative-decoding context. The default
  is now Ollama's 512 (1024 remains available with `lcode config set num_batch 1024`), and when the GPU
  runs out of memory before answering, lcode retries automatically with a batch of 512.
- A tool call with invalid JSON arguments no longer aborts the request: lcode tells the model what
  was wrong and lets it try again. Unknown or missing tool arguments are reported with the list of
  valid arguments.
- `lcode -c` continues the most recently *used* session in the folder, not the most recently created
  one. Empty sessions are no longer saved.
- `/rename` saves the session right away, so a session named before its first request shows up in
  `/resume`.
- Typing `exit` or `quit` (without a slash) quits instead of being sent to the model as a request.
- Compacted sessions keep their original title instead of showing the summary header.

## [0.1.1] - 2026-09-28

### Added

- `qwen3.5-9b` and `qwen3.5-4b` are now tested end to end, with measured speed and memory on a 12 GB
  GPU in the docs. `lcode setup` now recommends only tested models on common machines.
- Social preview image for the repository and link previews for the docs site.
- Release workflow that publishes to PyPI with Trusted Publishing.

### Changed

- Memory estimates use ~1 GB of runtime overhead instead of 1.5 GB, matching measurements, so small
  models get larger context windows (e.g. `qwen3.5-9b` at 128K on a 12 GB GPU).

### Fixed

- `lcode --model X` no longer reuses the context window saved for the default model; it picks the
  largest window that fits X. `/model` does the same inside a session.
- `lcode models` fits in 80-column terminals.

## [0.1.0] - 2026-09-28

First public release.

### Added

- Interactive terminal agent with streaming markdown output, reasoning display, `@file` attachments,
  slash commands, a status bar and resumable sessions (`lcode -c`).
- One-shot mode for scripts: `lcode -p "..."`.
- Tools for the model: `read_file`, `write_file`, `edit_file` (with diff previews and a
  whitespace-tolerant fallback), `list_dir`, `glob`, `grep` (ripgrep with a pure-Python fallback),
  `bash` (live output, timeouts, persistent working directory) and `todo_write`.
- Permission modes `ask`, `auto-edit` and `yolo` (Shift+Tab cycles), with a read-only command
  allowlist and "always allow" per command.
- Model catalog with eight open-weight models, hardware detection for Linux + NVIDIA and Apple
  Silicon Macs, and memory-fit estimates that pick the largest context window that fits.
- `lcode setup` (recommend, download and configure a model), `lcode models`, `lcode doctor` and
  `lcode config`.
- Choice of context window per run (`--context 128k`), per session (`/ctx`) or in the config file.
- Text-only model variants that drop unused vision projectors to free GPU memory.
- Automatic conversation compaction when the context window is 85% full, plus `/compact`.
- `AGENTS.md` project instructions and `/init` to generate them.
- One-line installer for Linux and macOS.

[Unreleased]: https://github.com/nasser1941/lcode/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/nasser1941/lcode/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/nasser1941/lcode/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/nasser1941/lcode/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/nasser1941/lcode/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/nasser1941/lcode/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/nasser1941/lcode/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/nasser1941/lcode/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nasser1941/lcode/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nasser1941/lcode/releases/tag/v0.1.0
