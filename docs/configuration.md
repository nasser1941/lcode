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
| `num_batch` | per model | Prompt batch size. Larger reads prompts faster but needs more GPU memory |
| `keep_alive` | `30m` | How long Ollama keeps the model in memory after the last request |
| `ollama_host` | `http://localhost:11434` | Ollama server URL |
| `permission_mode` | `ask` | `ask`, `auto-edit` or `yolo` |
| `think` | `true` | Let the model reason before answering |

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
| `LCODE_HOME` | where sessions and prompt history are stored |

## Files lcode creates

| Path | Contents |
|---|---|
| `~/.config/lcode/config.toml` | Settings |
| `~/.local/state/lcode/sessions/` | Saved conversations (for `lcode -c`) |
| `~/.local/state/lcode/history` | Prompt history (++up++ in the prompt) |

Sessions contain everything the model read, including file contents. Delete the folder to clear them.

## Tuning for speed and memory

- **Out-of-memory errors:** lower the context (`/ctx 128k`) or the prompt batch
  (`lcode config set num_batch 512`).
- **Slow first answers on big files:** a larger `num_batch` (1024–2048) reads prompts faster if your
  GPU has room. lcode uses 1024 for `qwen3.6-35b`, the most that fits a 12 GB GPU next to a 256K cache.
- **Faster answers, less accuracy:** `--no-think` or `/think off`.
- **Keep the model warm:** `keep_alive = "2h"` avoids reload delays between sessions but holds the memory.
