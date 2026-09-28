# Models & context windows

lcode runs open-weight models through Ollama. It ships a curated catalog of models it knows how to
size, and it works with any other Ollama model that supports tool calling.

## The catalog

| Key | Model | Type | Download | Max context | Status |
|---|---|---|---|---|---|
| `qwen3.6-35b` | Qwen3.6 35B-A3B Coding | MoE, 3B active | 22.6 GB | 256K | **default**, tested |
| `qwen3.8-27b` | Qwen3.8 27B | dense | 17.7 GB | 256K | untested |
| `qwen3.6-27b` | Qwen3.6 27B Coding | dense | 17.8 GB | 256K | untested |
| `laguna-xs-2.1` | Poolside Laguna XS 2.1 | MoE, 3B active | 20.3 GB | 256K | untested |
| `nemotron-3.5-lightning` | NVIDIA Nemotron 3.5 Lightning | hybrid MoE, 3B active | 25.4 GB | 1M | untested |
| `gpt-oss-20b` | OpenAI gpt-oss 20B | MoE, 3.6B active | 13.8 GB | 128K | untested |
| `qwen3.5-9b` | Qwen3.5 9B | dense | 6.6 GB | 256K | tested |
| `qwen3.5-4b` | Qwen3.5 4B | dense | 3.4 GB | 256K | tested |

*Tested* means the maintainers verified multi-step tool use (exploring a repo, editing files,
running commands) end to end. Untested models are expected to work; please
[report how they do](https://github.com/nasser1941/lcode/issues/new?template=model_request.yml).

Run `lcode models` to see the same list with what fits on **your** machine:

```text
Models for this machine: NVIDIA GeForce RTX 4080 Laptop GPU (12 GB VRAM), 31 GB RAM
 Key                     Size      Max ctx  Fits here  Speed                  Status
 qwen3.6-35b             22.6 GB   256K     256K       good (experts in RAM)  installed, recommended, current, tested
 qwen3.8-27b             17.7 GB   256K     128K       slow (split across GPU and CPU)  untested
 …
 qwen3.5-9b              6.6 GB    256K     128K       fast                   installed, tested
```

### Why the default is a Mixture-of-Experts model

A **Mixture-of-Experts (MoE)** model like Qwen3.6 35B-A3B has 35B parameters but uses only about
3B for each token. When the model doesn't fit in GPU memory, its expert weights can live in system
RAM and it still runs fast, because each token touches only a few experts. A **dense** model of
similar size uses all its weights for every token and slows down several times when split between
GPU and CPU. That's why lcode prefers MoE models on GPUs with less than 24 GB, and dense models only
when they fit entirely in GPU (or Apple unified) memory.

## Choosing a model

=== "Once, as the default"

    ```bash
    lcode setup qwen3.5-9b
    ```

    Downloads the model (if needed), prepares it and makes it the default.

=== "For one session"

    ```bash
    lcode --model qwen3.5-9b
    ```

=== "Inside a session"

    ```text
    ❯ /models
    ❯ /model qwen3.5-9b
    ```

### Any other Ollama model

```bash
ollama pull some-model:tag
lcode --model some-model:tag --context 64k
```

The model must support **tools** (check its page on [ollama.com](https://ollama.com/search?c=tools)).
lcode can't estimate the memory needs of models outside its catalog, so it defaults to a 32K
context; set `--context` yourself. If a model prints tool calls as text instead of calling tools,
lcode tries to recover them, but results are usually worse. To propose a model for the catalog, see
[Contributing](contributing.md#adding-a-model-to-the-catalog).

## Choosing a context window

The **context window** is how much text the model can see at once: the conversation, files it has
read and command output. Bigger windows let it work with more of your repository, but need more
memory and make the first read of large files slower.

By default lcode uses the **largest window that fits your memory** (up to the model's maximum).
Change it:

```bash
lcode setup qwen3.6-35b --context 128k   # save as the default
lcode --context 64k                      # this session only
lcode config set context 128k            # change the saved default
```

```text
❯ /ctx 128k      # inside a session (the model reloads on the next request)
❯ /ctx           # show the current window and the model maximum
```

Sizes accept `k` and `m` suffixes (`128k` = 131,072 tokens, `1m` = 1,048,576). When the window is
85% full, lcode summarizes the conversation automatically so you can keep working; `/compact` does it
on demand.

### How memory is estimated

```
memory ≈ model download size + KV cache per token × context window + ~1 GB overhead
```

The KV cache is what grows with context. Hybrid models only keep it in a few layers, which is what
makes long windows affordable:

| Model | KV cache per token | 32K | 128K | 256K |
|---|---|---|---|---|
| qwen3.6-35b | 22 KiB | 23 GB | 25 GB | 28 GB (measured: 26 GB) |
| qwen3.8-27b / qwen3.6-27b | 68 KiB | 20 GB | 26 GB | 35 GB |
| laguna-xs-2.1 | ~40 KiB (estimated) | 21 GB | 25 GB | 30 GB |
| nemotron-3.5-lightning | 7 KiB | 25 GB | 26 GB | 26 GB (1M: 32 GB) |
| gpt-oss-20b | 24 KiB | 15 GB | 17 GB | — |
| qwen3.5-9b | 32 KiB | 8 GB | 11 GB (measured: 9.8 GB) | 15 GB (measured: 16 GB) |
| qwen3.5-4b | 32 KiB | 5 GB | 8 GB (measured: 8.0 GB) | 12 GB |

The **memory available** for a model is:

- **Apple Silicon:** about two thirds of unified memory (three quarters above 36 GB), the part macOS
  lets the GPU use by default.
- **Linux with NVIDIA:** GPU memory plus system RAM minus ~8 GB for the OS and your apps.

## Hardware guide

What `lcode setup` recommends on common machines: the tested default when it fits, otherwise the
strongest model with a useful context window. These are estimates from the formula above (with
0.5 GB kept free);
results on your machine are welcome in the
[model reports](https://github.com/nasser1941/lcode/issues/new?template=model_request.yml).

| Machine | Memory for models | Recommended | Context | Speed |
|---|---|---|---|---|
| Mac with M4, 16 GB | ~11 GB | qwen3.5-9b | 64K | fast |
| Mac with M4 / M4 Pro, 24 GB | ~16 GB | qwen3.5-9b | 256K | fast |
| Mac with M4 Pro / M4 Max, 36 GB | ~24 GB | qwen3.6-35b | 64K | fast |
| Mac with M4 Pro, 48 GB | ~36 GB | qwen3.6-35b | 256K | fast |
| Mac with M4 Max, 64–128 GB | 48–96 GB | qwen3.6-35b | 256K | fast |
| NVIDIA 8 GB + 16 GB RAM | ~16 GB | qwen3.5-4b | 64K | fast |
| NVIDIA 8–16 GB + 32 GB RAM | 32–40 GB | qwen3.6-35b | 256K | good (experts in RAM) |
| NVIDIA 24 GB + 64 GB RAM | ~80 GB | qwen3.6-35b | 256K | good (experts in RAM) |
| CPU only, 32 GB RAM | ~24 GB | qwen3.5-4b | 256K | slow |

### Measured performance

On an RTX 4080 Laptop GPU (12 GB) with an i9-13980HX and 32 GB RAM, `qwen3.6-35b` at 256K context:

| | |
|---|---|
| Generation | 50–60 tokens/s on code (speculative decoding with the model's multi-token prediction) |
| Prompt reading | ~500 tokens/s: a 60K-token chunk of code takes about 2 minutes the first time |
| Follow-up turns | start in 1–2 s: Ollama reuses the cached prompt |
| A real task | "explain X with file:line citations, then write and run a script" in about 2 minutes |

The small models on the same GPU (a bug-fix task: read, run the failing tests, edit, re-run):

| Model | Context | Memory | Placement | Generation | Task time |
|---|---|---|---|---|---|
| qwen3.5-9b | 128K | 9.8 GB | 100% GPU | 64 tokens/s | 19 s |
| qwen3.5-9b | 256K | 16 GB | 63% GPU / 37% CPU | 21 tokens/s | 42 s |
| qwen3.5-4b | 128K | 8.0 GB | 100% GPU | 97 tokens/s | 16 s |

Dense models slow down about 3x once they no longer fit in VRAM, which is why lcode sizes their
context to stay on the GPU.

Apple Silicon numbers are not measured yet; please share yours.

### Tips for Macs

- Close memory-hungry apps before long sessions: unified memory is shared with everything else.
- macOS limits how much memory the GPU may use. On a Mac dedicated to models you can raise it
  (until reboot), e.g. to 40 GB on a 48 GB Mac: `sudo sysctl iogpu.wired_limit_mb=40960`.
  lcode's estimates assume the default limit.

## Text-only variants

Some models (the Qwen3.6 family, for example) ship with a vision encoder that a coding agent doesn't
use. `lcode setup` creates a text-only variant named `lcode-<key>` that reuses the downloaded
weights, so it takes no extra disk space, and frees about 1 GB of GPU memory. On a 12 GB GPU that
memory is what allows a larger prompt batch, which reads prompts ~1.8x faster.
