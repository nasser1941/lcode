# Configuration

Settings live in `~/.config/lcode/config.toml` (or `$XDG_CONFIG_HOME/lcode/config.toml`).
`lcode setup` writes the model and context; change anything else with `lcode config`:

```bash
lcode config                           # show every setting, its value and where it comes from
lcode config set permission_mode auto-edit
lcode config set context 128k
lcode config unset context             # back to the default
lcode config path                      # print the file location
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `model` | `qwen3.6-35b` | Catalog key (see `lcode models`) or any installed Ollama model tag; with another [backend](servers.md), a model the server serves |
| `context` | largest that fits | Context window in tokens; accepts `128k`, `1m` |
| `num_batch` | 512 | Prompt batch size. Larger reads prompts faster but needs more GPU memory |
| `keep_alive` | `30m` | How long Ollama keeps the model in memory after the last request |
| `ollama_host` | `http://localhost:11434` | Ollama server URL |
| `backend` | `ollama` | The model server: `ollama`, `lmstudio`, `llama.cpp`, `vllm`, `mlx` or `openai` (any OpenAI-compatible API); see [Other model servers](servers.md) |
| `base_url` | the backend's | The server's OpenAI-compatible address, e.g. `http://localhost:1234/v1` |
| `permission_mode` | `ask` | `ask`, `plan`, `auto-edit` or `yolo` |
| `think` | `true` | Let the model reason before answering |
| `web` | `on` | Web search and page fetching: `on`, `ask` (before each search/website) or `off` |
| `search_backend` | `auto` | `auto`, `ollama`, `brave`, `tavily` or `searxng`; `auto` uses the first one configured |
| `searxng_url` | | Your SearXNG instance, e.g. `http://localhost:8888` |
| `sandbox` | `off` | Run shell commands in a container: `docker` or `podman` ([Sandbox](sandbox.md)) |
| `sandbox_image` | lcode's image | Container image for the sandbox; any image with bash and setsid |
| `sandbox_network` | `false` | Let commands in the sandbox use the network |
| `vision_model` | `auto` | The model that [looks at images](usage.md#images): `auto`, `off` or an Ollama model that can see |
| `mcp_tools` | `auto` | How [MCP](mcp.md#context) tool definitions reach the model: `auto`, `direct` or `search` (on demand) |
| `memory` | `ask` | [Notes that carry over](memory.md) to later sessions: `ask` (confirm each), `auto` or `off` |
| `skills` | `all` | Which [skills](commands.md#skills) the model is offered: `all` (lcode's folders and the ones other agents share), `lcode` (only `.lcode/skills/` and `~/.config/lcode/skills/`) or `off` |
| `repo_map` | `true` | Give the model a ranked [map of the repository](search.md) |
| `embed_model` | `auto` | Embedding model for [semantic code search](search.md#semantic-code-search): `auto`, `off` or a model name |
| `lsp` | `auto` | Use installed [language servers](lsp.md) for code navigation and errors after edits: `auto` or `off` |
| `prune` | `true` | When the context is 85% full, first remove old tool output, and summarize the conversation only if that's not enough ([how](how-it-works.md#context-management)) |
| `subagents` | `true` | Let the model hand tasks to [subagents](agents.md) with their own context |
| `max_parallel_agents` | `1` | Subagents that run at the same time; more needs `OLLAMA_NUM_PARALLEL` on the Ollama server ([details](agents.md#several-at-once)) |
| `notify` | `true` | A desktop [notification](usage.md#notifications) when a long request is done or waits for your answer |
| `notify_after` | `30` | Seconds a request runs before it notifies |
| `repeat_limit` | `3` | Identical tool calls with identical results before lcode [steps in](how-it-works.md#when-the-model-repeats-itself); `0` turns it off |
| `secret_check` | `true` | Before `git commit` and `git push`, look for passwords, keys and tokens they'd add ([details](usage.md#secrets-in-commits)) |
| `checkpoints` | `true` | Save a checkpoint before the model changes files, so [`/undo`](usage.md#undo-and-checkpoints) can restore them |

Besides these settings, `config.toml` can hold [hooks](hooks.md) (`[[hooks]]`) and
[permission rules](hooks.md#permission-rules) (`[permissions]`); a repository can have its own in
`.lcode/settings.toml`. `lcode config set` keeps them when it rewrites the file.

Example file:

```toml
# ~/.config/lcode/config.toml
model = "qwen3.6-35b"
context = 262144
permission_mode = "ask"
keep_alive = "1h"
```

## Environment variables

Environment variables override the file, which is useful for one-off runs and CI:

| Variable | Setting |
|---|---|
| `LCODE_MODEL` | `model` |
| `LCODE_CONTEXT` | `context` |
| `LCODE_NUM_BATCH` | `num_batch` |
| `LCODE_KEEP_ALIVE` | `keep_alive` |
| `OLLAMA_HOST` | `ollama_host` (same variable the Ollama CLI uses) |
| `LCODE_BACKEND` | `backend` |
| `LCODE_BASE_URL` | `base_url` |
| `LCODE_API_KEY` or `OPENAI_API_KEY` | API key for an [OpenAI-compatible server](servers.md) that needs one (never stored in the config file) |
| `LCODE_WEB` | `web` |
| `LCODE_SANDBOX` | `sandbox` |
| `LCODE_MEMORY` | `memory` |
| `OLLAMA_API_KEY`, `BRAVE_API_KEY`, `TAVILY_API_KEY` | API key for that [search provider](usage.md#web-search) (never stored in the config file) |
| `LCODE_HOME` | where sessions and prompt history are stored |

## Files lcode creates

| Path | Contents |
|---|---|
| `~/.config/lcode/config.toml` | Settings |
| `~/.local/state/lcode/sessions/` | Saved conversations (for `lcode -c`) |
| `~/.local/state/lcode/history` | Prompt history (++up++ in the prompt) |
| `~/.config/lcode/mcp.json` | [MCP servers](mcp.md) (readable only by you; may contain tokens) |
| `~/.local/state/lcode/mcp-auth/` | Sign-in tokens for remote MCP servers (readable only by you) |
| `~/.local/state/lcode/mcp-logs/` | Error output of local MCP servers |
| `~/.local/state/lcode/checkpoints/` | Checkpoints for `/undo` (copies of your project's files; deleted after 14 days) |
| `~/.local/state/lcode/memory/` | [Memory](memory.md) notes: `user/` for every repository, `projects/<repository>/` for each one |
| `~/.config/lcode/commands/`, `~/.config/lcode/skills/` | Your [commands and skills](commands.md) (a repository's go in `.lcode/`) |
| `~/.local/state/lcode/extensions-approved.json` | Repositories whose commands, skills and agents you approved |
| `~/.config/lcode/agents/` | Your [custom agents](agents.md#custom-agents) (a repository's go in `.lcode/agents/`) |
| `~/.config/lcode/lsp.json` | Which [language server](lsp.md#settings) to use for a language, or none |
| `~/.local/state/lcode/lsp-logs/` | Language servers' own output |
| `~/.local/state/lcode/index/` | [Code search](search.md#semantic-code-search) indexes, one per repository (`lcode index`) |
| `~/.local/state/lcode/outputs/` | The full text of long command output that was cut short for the model (kept a week) |
| `~/.local/state/lcode/limits.json` | Context sizes that ran out of GPU memory on this machine (safe to delete) |

Sessions contain everything the model read, including file contents. Delete the folder to clear them.

## Tuning for speed and memory

- **Out-of-memory errors:** lower the context (`/ctx 128k`), close other programs using the GPU, or
  pick a smaller model (`/models`).
- **Slow first answers on big files:** a larger prompt batch reads prompts faster if your GPU has
  room: `lcode config set num_batch 1024` makes `qwen3.6-35b` read prompts ~1.8x faster (~500 instead
  of ~280 tokens/s on a 12 GB GPU). On a 12 GB GPU at 256K context it only fits when little else uses
  VRAM; if the GPU runs out of memory before answering, lcode retries automatically with 512.
- **Faster answers, less accuracy:** `--no-think` or `/think off`.
- **Keep the model warm:** `keep_alive = "2h"` avoids reload delays between sessions but holds the memory.
