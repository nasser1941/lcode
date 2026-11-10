# Troubleshooting & FAQ

Start with:

```bash
lcode doctor
```

It checks the hardware, Ollama, the configured model and the context size, and says what to fix.
Please include its output in [bug reports](https://github.com/nasser1941/lcode/issues/new/choose).

## Problems

### `cannot reach Ollama at http://localhost:11434`

Ollama isn't running (or isn't installed).

- Linux: `sudo systemctl start ollama` (check with `systemctl status ollama`).
- macOS: open the Ollama app, or `brew services start ollama`.
- Docker: lcode looks for the Ollama container that publishes port 11434, and says how to start it: with
  `docker compose up` when Docker Compose made it, or `docker start <container>`. If there's no such
  container any more (for example after `docker system prune`), create it again; when its volume was removed
  as well (`docker volume prune -a`), so were the models: download them again with `lcode setup`.
- Remote server: `lcode config set ollama_host http://host:11434`.

### Docker: `all predefined address pools have been fully subnetted`

Docker Compose can't create a network for the Ollama container. A VPN that routes all traffic (for example
with routes `0.0.0.0/1` and `128.0.0.0/1`) covers every address range Docker would use. Put the
container on Docker's default network instead: add `network_mode: bridge` to the Ollama service in the
compose file, and run `docker compose up -d` again.

### `lcode isn't set up yet` / `… is not installed yet`

Run `lcode setup` (or `lcode setup <model>`) to download a model.

### `lcode: command not found`

The installer puts lcode in `~/.local/bin`. Open a new terminal, or add it to your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Out of memory, or `CUDA error: an illegal memory access`

The model plus its context don't fit. lcode handles the common cases itself: if a model fails to load
because of GPU memory, it first retries with a prompt batch of 512 (if you raised it), then with half
the context, until it loads. The context that worked is remembered in `~/.local/state/lcode/limits.json`,
so the next session starts there; `lcode doctor` shows it. Delete that file to let lcode try larger
contexts again (for example after a GPU upgrade, or if another program was using the GPU at the time).

If it still fails:

1. Lower the context: `/ctx 128k` in a session or `lcode config set context 128k`.
2. If you raised the prompt batch, lower it again: `lcode config unset num_batch`. (When the GPU runs
   out of memory before answering, lcode already retries once with a batch of 512.)
3. Make sure no other model is loaded (`ollama ps`, then `ollama stop <name>`) and that other GPU
   apps are closed.
4. Use a smaller model: `lcode models`.

If you use the original tag of a model with a vision encoder (e.g. `qwen3.6:35b-a3b-coding`) instead
of the text-only variant, run `lcode setup <key>` once to create the variant.

### Web search doesn't work

- `lcode doctor` shows the web status. "page fetching only" means no search provider is set up:
  set `OLLAMA_API_KEY` (or another provider, see [Web search](usage.md#web-search)) in the shell
  where you start lcode.
- `rejected the request (HTTP 401)`: the API key is wrong or expired.
- SearXNG `didn't return JSON`: add `json` to `search.formats` in its `settings.yml` and restart it.
- Some sites block automated downloads or need JavaScript; with `OLLAMA_API_KEY` set, lcode retries
  those through Ollama's fetch service.

### An MCP server doesn't work

Run `lcode mcp list`: it connects to every server and shows why one fails. A local server's error
output is in `~/.local/state/lcode/mcp-logs/<name>.log`. Remote servers that say *needs sign-in*
need `lcode mcp login <name>`. More in [MCP servers: Troubleshooting](mcp.md#troubleshooting).

### It's slow

- The first request loads the model (10–45 s). lcode starts loading in the background as soon as it
  starts, and `keep_alive` keeps it loaded between requests.
- Reading many large files takes a while the first time (~280 tokens/s on a 12 GB GPU, ~500 with
  `lcode config set num_batch 1024` if it fits); follow-up turns reuse the cache.
- `lcode doctor` shows how much of the loaded model is on the GPU, for example
  `Loaded ~23 GB, 42% on the GPU and the rest in system RAM · 32K context`. `ollama ps` leaves out the
  part of a Mixture-of-Experts model that's in system RAM, so it can say 98%. Dense models are much
  slower when split; prefer the MoE models in `lcode models` on smaller GPUs.
- `--no-think` skips reasoning for simple requests.

### It starts fast, then gets about three times slower

The machine is short of RAM. A model that doesn't fit on the GPU keeps the rest in RAM, and Ollama's
server keeps a prompt cache there that grows to 8 GB in a long session. When those don't fit next to
your other programs, Linux drops the model from memory and keeps reading it back from disk.

Measured with `qwen3.6-35b` at 32K on an RTX 4080 Laptop GPU (12 GB) with 31 GB of RAM: generation fell
from about 65 to about 20 tokens/s and prompt reading from about 200 to about 55, with the GPU at 2–14%
and the disk reading about 1.9 GB/s. A short test right after loading doesn't show it: the slowdown
starts as the prompt cache fills, within about an hour.

`lcode doctor` checks this on Linux, when Ollama runs on the same machine (in Docker too):

```text
! RAM        ~15 GB available now, ~21 GB needed in a long session: ~11 GB of the model that doesn't
             fit on the GPU, up to 10 GB for Ollama's server and prompt cache
```

`lcode setup` gives the same warning, and a session says it once when replies fall below half of their
earlier speed and RAM is short. `ollama ps` doesn't show any of this: for a Mixture-of-Experts model it
leaves out the experts mapped from the model's file, ~12 GB for `qwen3.6-35b`.

What helps:

1. Close other programs before a long session: browsers, IDEs, containers.
2. Use a smaller context: `/ctx 64k` in a session or `lcode config set context 64k`. This helps most when
   the window is large: going from 256K to 32K saves ~5 GB with `qwen3.6-35b`.
3. Pick a smaller model that fits on the GPU: `lcode models`.
4. To empty the prompt cache right away, unload the model: `ollama stop <model>`. The next request
   reloads it, and the cache fills up again over time.

On a Mac, `lcode doctor` doesn't check this yet: see [below](#on-a-mac-a-model-that-should-fit-gets-very-slow).

### The model prints tool calls as text instead of running them

The model doesn't support Ollama's tool calling well. lcode recovers common formats, but results
are better with a model from the catalog. Check that the model page on ollama.com lists **tools**.

### Edits fail with `old_string not found`

The model tried to replace text that doesn't match the file exactly. It normally re-reads the file
and retries by itself. If it keeps failing, ask it to re-read the file, or to rewrite the function
with `write_file`.

### On a Mac, a model that should fit gets very slow

Other apps are using unified memory. Close them, or pick a smaller context. See
[Tips for Macs](models.md#tips-for-macs).

## FAQ

**Does my code leave my machine?**
The model runs on the Ollama server you configure (your own machine by default), and lcode has no
telemetry and never uploads your files. With web access on (the default), search queries go to your
search provider and pages are downloaded from their websites; the model writes those queries, so they
can mention names from your code. For sensitive work use `lcode --no-web` or
`lcode config set web off`.

**Can I use it without a GPU?**
Yes, with small models (`qwen3.5-4b`), but expect a few tokens per second.

**Which model is best?**
The default, `qwen3.6-35b`, has the best tool calling among local models that run well on consumer
hardware. On a large Mac or a 24 GB+ GPU, the dense 27B models are worth trying. Please share
results.

**Is Windows supported?**
Through WSL2 with Ubuntu.

**How is this different from cloud coding agents?**
The workflow is similar: an agent in your terminal that reads, edits and runs code. The difference
is that everything runs locally: no API keys, no per-token cost, no code leaving your machine, and
you choose the model. Local models are smaller than frontier cloud models, so expect more guidance
to be needed on hard tasks.
