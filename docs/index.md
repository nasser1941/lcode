---
title: lcode — a coding agent that runs on your own machine
hide:
  - navigation
  - toc
---

<div class="hero" markdown>

![lcode logo](assets/logo.svg){ .hero-logo }

# lcode

**A coding agent that runs entirely on your own machine.**
Open-weight models · up to 256K tokens of context · your NVIDIA GPU or Apple Silicon Mac · no API keys, no cloud.

[Get started](installation.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/nasser1941/lcode){ .md-button }

</div>

```bash
curl -fsSL https://nasser1941.github.io/lcode/install.sh | bash
```

![lcode fixing a failing test](assets/demo.svg){ .demo }

## What it does

Point lcode at a repository and talk to it in your terminal. It explores the code, answers questions
with `file:line` references, writes and edits files, runs your scripts and tests, and keeps going
until the task is done, asking before it changes anything.

<div class="grid cards" markdown>

-   :material-shield-lock-outline:{ .lg .middle } **Private by design**

    ---

    Your code never leaves your machine. The model runs locally through [Ollama](https://ollama.com);
    lcode sends no telemetry.

-   :material-robot-outline:{ .lg .middle } **A real agent, not autocomplete**

    ---

    Reads, searches, edits and runs commands in a loop, verifies its work, and plans multi-step
    tasks with a todo list.

-   :material-book-open-page-variant-outline:{ .lg .middle } **Long context**

    ---

    Up to 256K tokens with the default model (1M with Nemotron), so it can hold a large part of your
    repository in view. You choose the window size.

-   :material-chip:{ .lg .middle } **Hardware-aware**

    ---

    Detects your GPU or Mac and picks the best model and the largest context that fits:
    `lcode setup` does it in one step.

-   :material-check-decagram-outline:{ .lg .middle } **Safe by default**

    ---

    Every edit is shown as a diff and every non-read-only command needs your approval, unless you
    choose otherwise.

-   :material-swap-horizontal:{ .lg .middle } **Bring your own model**

    ---

    Eight curated open-weight models out of the box, and any other Ollama model with tool calling
    works too.

</div>

## Runs on

| Platform | Recommended hardware | Default choice |
|---|---|---|
| **Ubuntu / Linux** with NVIDIA GPU | 12 GB+ VRAM and 32 GB RAM | Qwen3.6 35B-A3B at 256K context |
| **macOS on Apple Silicon** (M1–M4) | 48 GB+ unified memory | Qwen3.6 35B-A3B at 256K context |
| Smaller machines (16–24 GB Mac, 8 GB GPU) | — | Qwen3.5 9B / 4B with a smaller context |

See [Models & context windows](models.md) for the full catalog and how lcode sizes models to your
hardware.

## Next steps

- [Install lcode](installation.md) on Ubuntu or macOS
- [Take the quickstart](quickstart.md): your first session in five minutes
- [Pick a model and context window](models.md)
