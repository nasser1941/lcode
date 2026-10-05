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
| `model` | `qwen3.6-35b` | Catalog key (see `lcode models`) or any installed Ollama model tag |
| `context` | largest that fits | Context window in tokens; accepts `128k`, `1m` |
| `num_batch` | 512 | Prompt batch size. Larger reads prompts faster but needs more GPU memory |
| `keep_alive` | `30m` | How long Ollama keeps the model in memory after the last request |
| `ollama_host` | `http://localhost:11434` | Ollama server URL |
| `permission_mode` | `ask` | `ask`, `auto-edit` or `yolo` |
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
| `checkpoints` | `true` | Save a checkpoint before the model changes files, so [`/undo`](usage.md#undo-and-checkpoints) can restore them |

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
