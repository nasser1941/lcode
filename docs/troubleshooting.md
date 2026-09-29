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
- Docker: `docker start <container>`, and make sure port 11434 is published.
- Remote server: `lcode config set ollama_host http://host:11434`.

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

### It's slow

- The first request loads the model (10–45 s). lcode starts loading in the background as soon as it
  starts, and `keep_alive` keeps it loaded between requests.
- Reading many large files takes a while the first time (~280 tokens/s on a 12 GB GPU, ~500 with
  `lcode config set num_batch 1024` if it fits); follow-up turns reuse the cache.
- `ollama ps` shows how much of the model is on the GPU. Dense models are much slower when split;
  prefer the MoE models in `lcode models` on smaller GPUs.
- `--no-think` skips reasoning for simple requests.

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
