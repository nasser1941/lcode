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

The model plus its context don't fit.

1. Lower the context: `/ctx 128k` in a session or `lcode config set context 128k`.
2. Lower the prompt batch: `lcode config set num_batch 512`.
3. Make sure no other model is loaded (`ollama ps`, then `ollama stop <name>`) and that other GPU
   apps are closed.
4. Use a smaller model: `lcode models`.

If you use the original tag of a model with a vision encoder (e.g. `qwen3.6:35b-a3b-coding`) instead
of the text-only variant, run `lcode setup <key>` once to create the variant.

### It's slow

- The first request loads the model (10–45 s). lcode starts loading in the background as soon as it
  starts, and `keep_alive` keeps it loaded between requests.
- Reading many large files takes a while the first time (~500 tokens/s on a 12 GB GPU); follow-up
  turns reuse the cache.
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
No. lcode talks only to the Ollama server you configure (your own machine by default) and has no
telemetry.

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
