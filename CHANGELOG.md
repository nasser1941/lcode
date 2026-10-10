# Changelog

All notable changes to lcode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- lcode checks for secrets before the model runs `git commit` or `git push`: passwords, keys and
  tokens in code or configuration, credentials in URLs (`rtsp://user:pass@…`), private keys,
  tokens with a known form, and `.env` files, in the lines a commit adds or the commits a push
  sends. In `ask` mode you decide; in `auto-edit` and `yolo` the command doesn't run, and the model
  is told to move the secret out of the code. `/commit` and `/pr` show the findings before you
  confirm, and the GitHub Action doesn't commit at all. `lcode: allow-secret` in a comment allows a
  line; `secret_check = false` turns the check off. See
  [Secrets in commits](https://nasser1941.github.io/lcode/usage/#secrets-in-commits).

## [0.25.0] - 2026-10-10

### Changed

- The model is told what counts as checking its work: run what it changed the way it will be used,
  reproduce each reported problem before fixing it, check what it can't see (GUIs, web pages,
  layouts) without a screen, for example with Qt's `offscreen` platform, a headless browser or a
  rendered image, and never call something tested or done without having run it. See
  [Checking its own work](https://nasser1941.github.io/lcode/how-it-works/#checking-its-own-work).

### Added

- `lcode bench --tasks runtime-bug`: a bug that shows only when the code runs, a camera wall whose
  window grows with every video frame. Reading the code suggests band-aids that fail its hidden
  test. It isn't in the default set.

## [0.24.0] - 2026-10-10

### Added

- Ctrl+V in lcode's prompt pastes a screenshot: lcode reads the clipboard (with `wl-paste` on
  Wayland, `xclip` on X11, `pngpaste` or `osascript` on macOS), saves the image and attaches it as
  `@path`. Other clipboard content is pasted as text. `lcode doctor` shows whether it works here.
- Image paths in a message are attached without `@`: absolute and `~` paths, quoted paths with
  spaces, and `file://` paths dragged into the terminal. `@"path with spaces"` works for any file.

### Fixed

- An image path that doesn't exist is no longer ignored: lcode says so, and tells the model it can't
  see that image, so it asks instead of guessing what the picture shows. See
  [Images](https://nasser1941.github.io/lcode/usage/#images).

## [0.23.0] - 2026-10-10

### Added

- lcode notices when the model goes round in circles, making the same tool call with the same result
  again and again, as small models sometimes do. The third time in a request (the second, for a call
  that failed) the result tells the model so; two more, and lcode ends the request (status `loop`,
  exit code `4` with `lcode -p`). Results that change, and checks run after an edit, don't count.
  `repeat_limit` changes the threshold; `0` turns it off. See
  [When the model repeats itself](https://nasser1941.github.io/lcode/how-it-works/#when-the-model-repeats-itself).

### Fixed

- A tool call Ollama can't parse because its XML is broken (`XML syntax error … unexpected EOF`, from
  models such as Qwen3.6 that write their calls in XML) is now sent back to the model to make again,
  like a call with broken JSON, instead of ending the request with an error.

## [0.22.0] - 2026-10-06

### Added

- `lcode mcp add xai`: images, image edits, videos and speech with xAI's Grok Imagine and text to
  speech APIs, in the cloud and billed to your xAI account. It asks for your xAI API key, and runs
  `lcode-mcp-xai`, a new MCP server that ships with lcode, with the tools `generate_image`,
  `edit_image`, `generate_video` (waits for the video and downloads it), `video_status` and
  `text_to_speech`. Files are saved in `generated/` in the project or where you ask. See
  [Images, videos and speech with xAI](https://nasser1941.github.io/lcode/mcp/#images-videos-and-speech-with-xai).

## [0.21.0] - 2026-10-06

### Added

- `lcode update` updates lcode the way it was installed (uv tool, pipx, Homebrew, pip, or
  `git pull` for a checkout), and Ollama when it runs in Docker. The newest Ollama release comes
  from Docker Hub, not a local `latest` tag, and is pulled by its version tag (such as
  `ollama/ollama:0.35.1`) so `docker ps` shows the version. A container from Docker Compose gets
  the new tag in its compose file and is recreated with `docker compose up`; any other container is
  recreated with the same settings, and the old one comes back if the new one doesn't start.
  `lcode update --check` only shows which versions are out. See
  [Updating](https://nasser1941.github.io/lcode/installation/#updating).

## [0.20.0] - 2026-10-06

### Added

- Background commands: the model starts a dev server, a watcher or another process that keeps
  running with `bash` and `background: true`, reads its new output with `bash_output` and stops it
  with `bash_stop`. `/jobs` lists them, and they all stop when the session ends.
- Desktop notifications (`notify-send` on Linux, `osascript` on macOS, else the terminal bell)
  when a request that ran longer than 30 seconds is done, or lcode waits for your answer during
  one. Settings: `notify` and `notify_after`.

### Fixed

- A command that starts something with `&` no longer hangs until its timeout while that process
  holds the output open. What it left running is stopped when it ends, and the model is told to
  use a background command instead.

## [0.19.0] - 2026-10-06

### Added

- `lcode acp`: lcode inside editors that speak the Agent Client Protocol (Zed, JetBrains IDEs,
  Neovim, Emacs and others). The editor shows the answers and thinking as they stream, each tool
  call with the files it touches and a diff for edits, and asks permission questions and plan
  approvals in its own dialog. lcode's permission modes are the session's modes, the todo list is
  the editor's plan, and saved sessions reopen with their conversation. The model reads the
  editor's buffers (unsaved changes included) and edits through them, and MCP servers the editor
  passes join lcode's own. See [Editors](https://nasser1941.github.io/lcode/editors/) for setup.

### Fixed

- Cancelling a request whose answer was cut off mid-stream no longer ends it as if it were done.

## [0.18.0] - 2026-10-06

### Added

- `lcode -p "…" --output json` prints one result object (status, answer, tool calls, files
  changed, token usage); `--output stream-json` prints an event per model step and tool result,
  then the result. `--max-steps` and `--allowed-tools` limit a run, and the exit code says how it
  ended. Without a person to ask, anything that needs permission is refused.
- A Python API: `from lcode import Session`, with `run()`, `stream()` and an `approve` callback
  for permission requests. See `examples/api/fix_tests.py`.
- A GitHub Action for self-hosted runners (`uses: nasser1941/lcode@v0.18.0`). It answers `@lcode`
  in issues and pull requests from the repository's owners, members and collaborators, proposes
  changes as a pull request (or pushes them to the pull request's branch), and can review pull
  requests. See [Automation](https://nasser1941.github.io/lcode/automation/).

## [0.17.0] - 2026-10-06

### Added

- `/commit` writes a commit message in the repository's style, from the diff, the conversation
  and recent commits, and commits once you approve it. It commits what you staged, or every
  change except files the model thinks are unrelated and files that may hold secrets.
- `/review [base]` reviews the uncommitted changes, or the branch against a base, and reports
  findings with `path:line`. The model can read but not change anything during a review.
- `/pr [base]` pushes the branch and opens a GitHub pull request with `gh` after you approve the
  title and description; commits made on `main` itself go to a new branch.
- A command of your own called `commit`, `review` or `pr` replaces the built-in one.
- `lcode --worktree [NAME]` (`-w`) runs the session in a git worktree on its own branch, so
  several sessions can work on one repository at once. It's removed at the end if nothing
  changed. See [Git workflow](https://nasser1941.github.io/lcode/git/).

### Fixed

- With another model server, `/ctx` no longer says the model reloads: it changes how much of the
  server's context window lcode uses.

## [0.16.0] - 2026-10-05

### Added

- Other model servers: LM Studio, llama.cpp's llama-server, vLLM, MLX's server, or any other
  server with an OpenAI-compatible API, instead of Ollama. `lcode config set backend lmstudio` (or
  `llama.cpp`, `vllm`, `mlx`, `openai` with `base_url`); a key can come from `LCODE_API_KEY`. Tool
  calls, streaming, reasoning (on and off), images and structured answers work as with Ollama.
  lcode reads the context window from the server, matches the model setting to the models it
  serves, and moves thinking that a server sends as part of the answer out of it. `lcode doctor`,
  `lcode models`, `lcode bench` and `lcode index` work with every backend. See
  [Other model servers](https://nasser1941.github.io/lcode/servers/), with setup for each.

## [0.15.0] - 2026-10-05

### Added

- Hooks: shell commands that run on lcode's events (`before_tool`, `after_tool`, `after_request`,
  `session_start`, `notification`), filtered by tool and file, with the event as JSON on stdin. A
  `before_tool` hook that exits with code 2 blocks the call and tells the model why; an
  `after_tool` hook's output reaches the model when it fails (a linter) or with `feedback = true`.
  Notification hooks run when lcode waits for you and when a long request is done.
- Permission rules: `[permissions]` allow and deny lists for shell commands, file edits, web
  domains and MCP tools, checked before lcode asks. Deny always wins, even in `yolo` mode and for
  read-only commands, and is enforced for subagents too.
- Both go in `config.toml` or a repository's `.lcode/settings.toml`, which is used only once
  approved (and approved again after any change). `lcode doctor` lists them. See
  [Hooks and permission rules](https://nasser1941.github.io/lcode/hooks/), with recipes for ruff,
  prettier, blocking force-pushes, running tests and desktop notifications.

### Fixed

- `lcode config set` no longer drops tables you added to `config.toml` by hand.

## [0.14.0] - 2026-10-05

### Added

- A repository map: the important files with their classes and functions, signatures and line
  numbers, ranked by how much of the code uses them and cut to a token budget. Small repositories
  (up to 300 source files) get it in the model's instructions; larger ones get a `repo_map` tool,
  which can zoom into a folder. Python is read with its own parser; TypeScript, JavaScript, Go,
  Rust, Java and Kotlin by their declarations. Measured with qwen3.6-35b, it halved the steps the
  model took to find code and cut the tokens it read by 39%. The `repo_map` setting turns it off.
- Semantic code search: with an embedding model in Ollama (qwen3-embedding, nomic-embed-text, …),
  `lcode index` indexes the repository, and the model gets a `search_code` tool that finds code by
  what it does. Changed files are embedded again before each search. During a session the
  embedding model runs on the CPU, so it never pushes lcode's model off the GPU. The
  `embed_model` setting picks the model or turns it off.
- `lcode bench --tasks find-concept` (a task in a larger repository), `--repo-map` and
  `--code-search`. See [Repository map and code search](https://nasser1941.github.io/lcode/search/).

## [0.13.0] - 2026-10-05

### Added

- Code intelligence from language servers. With one installed (basedpyright or pyright for Python,
  typescript-language-server or TypeScript 7's `tsc --lsp`, gopls, rust-analyzer, clangd), the model
  gets an `lsp` tool for a symbol's definition, references and type, and a file's or the workspace's
  symbols; and after every edit to a file in that language, the errors the edit introduced are
  added to the result, so the model fixes them right away. Servers start on demand, one per
  language; a project's own `node_modules/.bin` comes first. Without any server installed nothing
  changes. `lcode doctor` shows what it found; the `lsp` setting and `~/.config/lcode/lsp.json`
  configure it. See [Code intelligence](https://nasser1941.github.io/lcode/lsp/).

## [0.12.0] - 2026-10-05

### Added

- `/context` shows what uses the context window, by category: the system prompt and its parts
  (`AGENTS.md`, memory notes, skills, MCP servers), tool definitions, your messages, attached files,
  the model's replies and each tool's results.
- `lcode bench --session` runs all tasks in one conversation, to test long sessions and context
  management; `--rounds N` repeats the tasks and `--no-prune` turns the removal of old tool output
  off, to compare.

### Changed

- Smarter context management, which matters most at 32K. When the window is 85% full, lcode first
  replaces old tool output (file contents, command output, search results from before your last
  two requests, and earlier copies of files that were read again) with one-line notes saying what
  was there, then the output of your previous request if needed; it summarizes the conversation
  only if that doesn't free enough room. Measured on 24-task sessions at 32K: fewer summaries, with
  about as many tasks solved. The `prune` setting turns it off.
- Long command and MCP output keeps its beginning, its end and the lines that look like errors;
  the full output is saved to a file the model can read.

## [0.11.1] - 2026-10-05

### Fixed

- `lcode mcp add encord` failed with "needs the Encord SDK" when lcode itself was installed with
  `uv tool install`: `uvx` reused that installation, which doesn't have the SDK. The Encord and
  Valohai presets now run `uvx --isolated`. If you added either server with 0.11.0, add it again
  (`lcode mcp add encord`) or put `--isolated` first in its `args` in `mcp.json`.

## [0.11.0] - 2026-10-05

### Added

- Ready-made MCP servers for data and ML platforms:
  - `lcode mcp add metabase` connects to the MCP server built into Metabase 60 and later, with browser
    sign-in, so the model sees your data with your Metabase permissions.
  - `lcode mcp add encord` and `lcode mcp add valohai`: those platforms have no MCP server of their own,
    so lcode now ships `lcode-mcp-encord` (projects, datasets, ontologies, labeling progress, tasks and
    labels, built on the Encord SDK) and `lcode-mcp-valohai` (executions with their logs, metrics and
    outputs, pipelines). Both are read-only unless started with `--allow-writes`, which adds
    assigning tasks and setting priorities (Encord) or starting and stopping executions (Valohai), and
    both work with other MCP clients too. See [MCP servers](https://nasser1941.github.io/lcode/mcp/#encord).
- Catalog questions that ask for an address now accept it without `https://`.

## [0.10.0] - 2026-10-05

### Added

- Custom commands: prompt templates in `.lcode/commands/<name>.md` or
  `~/.config/lcode/commands/`, run as `/name args`, with `$ARGUMENTS` and `$1`…`$9`, and an
  optional frontmatter for the description, an argument hint and the tools allowed for the
  request.
- Skills in the [Agent Skills](https://agentskills.io) format, from `.lcode/skills/`,
  `.agents/skills/` and `.claude/skills/` (in the repository and in your home folder). Only their
  names and descriptions are in the system prompt; the model loads a skill's instructions with the
  new `skill` tool when a task matches, and its scripts and reference files when the instructions
  call for them. `/name` starts a skill yourself. `/help` lists commands and skills and where each
  comes from. The `skills` setting limits them to lcode's own folders (`lcode`) or turns them off.
  An example command and skill are in `examples/`. See
  [Commands and skills](https://nasser1941.github.io/lcode/commands/).

### Changed

- A repository's own commands, skills and agents are used only after you approve them; lcode asks
  the first time and again when they change, like a project's `.mcp.json`. Custom agents from a
  repository (new in 0.8.0) used to load without asking.

## [0.9.0] - 2026-10-05

### Added

- Plan mode: a `plan` permission mode (Shift+Tab from `ask`, `/mode plan`, `lcode --plan`, or
  `/plan <request>`) in which the model can read, search, run read-only commands, use the web and
  read-only subagents, but can't change anything. It presents a plan with the new `present_plan`
  tool, and you approve it (in `ask` or `auto-edit` mode), edit it, save it to `.lcode/plans/`, or
  send it back with feedback. An approved plan fills the todo list and is kept through compaction;
  `/plan` shows it. See [Plan mode](https://nasser1941.github.io/lcode/usage/#plan-mode).

### Fixed

- `lcode mcp add` shows the default value in its questions again, such as `AWS region
  [us-east-1]`; the terminal formatting swallowed it.
- The end-of-session "Worth remembering?" list shows each note's type (`[feedback]`, `[project]`, …),
  which the same problem hid.

## [0.8.0] - 2026-10-05

### Added

- Subagents: the model can hand a task to a subagent with its own fresh context through the new
  `agent` tool, and only the subagent's report comes back, so exploring a large codebase or reading
  long output doesn't fill the main context. Built-in types: `explore` and `plan` (read-only) and
  `worker` (all tools). Custom agents are markdown files in `.lcode/agents/` or
  `~/.config/lcode/agents/`; `@name` asks for one. Subagents use the session's model and context
  window (no reload), permissions (prompts are labelled with the agent), sandbox and checkpoints
  (`/undo` covers their changes). With `max_parallel_agents` above 1 and Ollama's
  `OLLAMA_NUM_PARALLEL`, several run at the same time; workers that do get their own git worktree
  and their changes come back as a diff to approve. `/agents` shows the types and what each
  subagent did; `lcode doctor` estimates how many could run at once. See
  [Subagents](https://nasser1941.github.io/lcode/agents/).

### Changed

- A shell command that starts with `cd <folder> &&` counts as read-only when the rest of it does,
  so `cd src && grep -n foo *.py` runs without asking. A `cd` with `$(…)` or backticks still asks.

## [0.7.0] - 2026-10-05

### Added

- Memory across sessions: lcode keeps short notes that later sessions load, only key knowledge and
  not whole conversations. The model saves them with its new `memory` tool (and updates or deletes
  them), you add them with `/remember`, and when a session ends (on quit, `/clear` or before
  compaction) one quick question asks the model what from it is worth remembering. Notes are
  per repository, shared by its git worktrees, plus a few for every repository; they're plain
  markdown files, managed with `/memory`. The index in the system prompt is capped at about 2,000
  tokens. Notes that look like they contain secrets are never saved. The `memory` setting is `ask`
  (confirm each note, the default), `auto` or `off`; `--no-memory` turns it off for a session.
  See [Memory](https://nasser1941.github.io/lcode/memory/).

## [0.6.1] - 2026-10-01

### Fixed

- Tools marked `free_gpu` (such as ComfyUI's `run_workflow`) now always run to completion
  (`wait: true`): a job left running in the background competed with lcode's reloading model for
  the GPU.

## [0.6.0] - 2026-10-01

### Added

- lcode can look at images: attach a screenshot, mockup or diagram with `@path`, and the model can
  open images itself with the new `view_image` tool. A model that can see describes the image in
  detail (all text transcribed, with your question in mind): your model itself if it can see, or
  the original model behind lcode's text-only variant, which `lcode setup` already downloaded
  (`vision_model` setting; `lcode doctor` shows which). Screenshots returned by MCP tools, such as
  Playwright's, are described too.
- `lcode mcp add comfyui`: generate and edit images with models you run locally in ComfyUI (FLUX,
  SDXL, Stable Diffusion 1.5, Qwen-Image), through ComfyUI's official MCP server, with only its local
  tools enabled.
- MCP servers can mark tools that need the GPU to themselves (`free_gpu` in `mcp.json`): lcode
  unloads its own model before they run and reloads it afterwards. On for ComfyUI's generation tools,
  so image models work on a 12 GB GPU next to a large coding model.

## [0.5.0] - 2026-10-01

### Added

- An optional sandbox for shell commands: with `lcode --sandbox` (or `sandbox = "docker"` /
  `"podman"`), the model's commands run in a container that only sees the project folder, as your
  user, with no network access unless you allow it (`/sandbox network on`, `sandbox_network`), no
  Linux capabilities and a process limit. The file tools are limited to the project too. lcode builds
  a default image with Python, Node.js, git and build tools on first use (`sandbox_image` for your
  own). In `auto-edit` mode, sandboxed commands run without asking. If the sandbox can't start, lcode
  stops instead of running commands without it.

## [0.4.1] - 2026-10-01

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

[Unreleased]: https://github.com/nasser1941/lcode/compare/v0.25.0...HEAD
[0.25.0]: https://github.com/nasser1941/lcode/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/nasser1941/lcode/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/nasser1941/lcode/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/nasser1941/lcode/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/nasser1941/lcode/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/nasser1941/lcode/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/nasser1941/lcode/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/nasser1941/lcode/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/nasser1941/lcode/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/nasser1941/lcode/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/nasser1941/lcode/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/nasser1941/lcode/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/nasser1941/lcode/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/nasser1941/lcode/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/nasser1941/lcode/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/nasser1941/lcode/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/nasser1941/lcode/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/nasser1941/lcode/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/nasser1941/lcode/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/nasser1941/lcode/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/nasser1941/lcode/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/nasser1941/lcode/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/nasser1941/lcode/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/nasser1941/lcode/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/nasser1941/lcode/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/nasser1941/lcode/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/nasser1941/lcode/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/nasser1941/lcode/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/nasser1941/lcode/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/nasser1941/lcode/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/nasser1941/lcode/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nasser1941/lcode/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nasser1941/lcode/releases/tag/v0.1.0
