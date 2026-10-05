# Benchmarking models

`lcode bench` answers the most common question about local models: *which one works best for
coding on my machine?* It runs a fixed set of small coding tasks against one or more models and
reports how many they solved, how fast they ran and how much memory they used.

```bash
lcode bench                           # the configured model
lcode bench qwen3.6-35b qwen3.5-9b    # compare models
lcode bench --tasks fix-bug,rename    # run some of the tasks
lcode bench --json results.json       # also save machine-readable results
lcode bench --markdown                # also print a table to paste into a test report
```

`lcode bench` also runs against [other model servers](servers.md), such as llama-server or LM
Studio: `LCODE_BACKEND=llama.cpp lcode bench`. The server keeps the model loaded and doesn't report
its memory use, so those columns stay empty.

```text
lcode bench · NVIDIA GeForce RTX 4080 Laptop GPU (12 GB VRAM), 31 GB RAM · Ollama 0.32.15

lcode-qwen3.6-35b · 32K context · reasoning on · 8 tasks
  Loaded in 16s · 22.3 GB, 45% on the GPU · reads prompts at 855 tok/s
  ✓ fix-bug       Fix a bug so the tests pass         22s  the tests pass
  ✓ find-code     Answer with file:line                9s  next_wait at backoff.py:13
  ✓ write-script  Write a script from a spec          13s  correct output, also on data it hadn't seen
  ✓ rename        Rename a function across files      26s  its tests and extra checks pass
  ✓ precise-edit  One-line edit in a long file        10s  changed only service_087's timeout
  ✓ recover       Recover from failing commands       30s  used --source and dropped the invalid line
  ✓ spec-fix      Fix a function to match its spec    55s  follows the whole docstring
  ✓ feature       Add a feature across files          71s  priorities work, including old todos
```

At the end it prints a table comparing the models (see [results](#results-on-a-12-gb-laptop) below).

A model usually takes 4 to 12 minutes; each task stops after 5 minutes (`--timeout`).

### More tasks and features

`--tasks find-concept` adds a task in a larger repository: 40 files of a small backend where the
model has to find a function by what it does. It isn't in the default set, so results stay
comparable with earlier versions. `--repo-map` gives the model the [repository map](search.md) and
`--code-search` indexes each task's folder for [semantic search](search.md#semantic-code-search);
both are off in `lcode bench` by default, for the same reason.

### Long sessions

Each task normally starts a new conversation. `--session` runs them all in **one** conversation
instead, as a long working session would, so the context fills up and lcode's
[context management](how-it-works.md#context-management) has to work: the summary shows how often
it removed old tool output and how often it summarized the conversation. `--no-prune` turns the
removal off, to compare.

```bash
lcode bench --context 32k --session --rounds 3
lcode bench --context 32k --session --rounds 3 --no-prune
```

`--rounds 3` runs the tasks three times, 24 in one conversation, which fills a 32K window several
times over. With qwen3.6-35b on a 12 GB laptop (two runs each; runs vary a lot from one to the
next):

| | Removing old tool output first | Summarizing only (`--no-prune`) |
|---|---|---|
| Conversation summarized | 1 and 2 times | 1 and 3 times |
| Old tool output removed | 3 times in each run | — |
| Tasks solved | 21 and 22 of 24 | 23 and 21 of 24 |
| Time | 26 and 28 min | 21 and 30 min |

Removing old tool output means fewer summaries, so more of the conversation stays word for word,
at no measurable cost in tasks solved or time. The bench tasks are independent of each other,
which is where summaries hurt least; in a real session, where later requests build on earlier
ones, keeping the conversation intact matters more.

## The tasks

Every task runs in a new temporary folder, with every permission granted (`yolo` mode) and web
access off, and ends with an automatic check. The folders are deleted afterwards unless you pass
`--keep`, which also keeps each task's transcript (`lcode-bench.log`).

| Task | The model has to | Passes when |
|---|---|---|
| `fix-bug` | Find and fix a bug so the failing unit tests pass | The tests pass and the test file is unchanged |
| `find-code` | Answer a question about a small codebase | It names the right function and cites its `file:line` |
| `write-script` | Write a script from a spec and run it | The script prints the right output, also for data it never saw |
| `rename` | Rename a function across several files and tests | The old name is gone and extra hidden tests pass |
| `precise-edit` | Change one value in a 900-line file of near-identical entries | Exactly that one line changed |
| `recover` | Run a command that fails twice (a removed option, a bad input line) and work around it | The report is correct and only the bad line was dropped |
| `spec-fix` | Fix a function from a one-line bug report so it does everything its docstring says | Hidden tests of every rule in the docstring pass, edge cases included |
| `feature` | Add priorities to a small todo app (a new option, sorting, a marker in the output) | Its tests pass, and a scripted session gives the exact expected output, including for todos saved before the change |

The first six tasks are everyday work that good models get right; the last two need careful
reading and are where smaller models tend to slip. The checks are deterministic and can't be passed
by hard-coding: for example, `write-script` reruns the script on new data, and `rename`, `spec-fix`
and `feature` are checked with tests the model never saw.

## What's measured

| | |
|---|---|
| **Passed** | Tasks solved within the time limit |
| **Time** | Total time for the tasks (loading the model is reported separately) |
| **Generation** | Output tokens per second, across all the model's responses |
| **Prompt reading** | Prompt tokens per second: how fast the model reads files and tool output. Measured per model with fresh 4,000-token prompts (the faster of two readings, since the first one after loading includes warm-up), because during the tasks Ollama serves most of each request from its prompt cache |
| **Tool-call errors** | Tool calls that failed (wrong arguments, editing without reading first, unmatched edits) or that the model wrote as invalid JSON |
| **Memory** | The model's memory use and how much of it is on the GPU, from Ollama |

Results depend on the context window, so `lcode bench` uses **32K** by default on every machine,
which makes results comparable. When comparing models, each one is unloaded before the next starts
so every model gets the whole GPU; lcode warns if other models are already loaded. Use `--context 128k` to measure a larger window, and `--no-think`
to measure a model without reasoning.

## Results on a 12 GB laptop

RTX 4080 Laptop GPU (12 GB) with 31 GB of RAM, Ollama 0.32.15, 32K context, reasoning on:

| | `qwen3.6-35b` | `qwen3.5-9b` | `qwen3.5-4b` |
|---|---|---|---|
| fix-bug | ✓ 22s | ✓ 16s | ✓ 11s |
| find-code | ✓ 9s | ✓ 6s | ✓ 5s |
| write-script | ✓ 13s | ✓ 44s | ✗ 18s |
| rename | ✓ 26s | ✓ 28s | ✓ 30s |
| precise-edit | ✓ 10s | ✓ 12s | ✓ 8s |
| recover | ✓ 30s | ✗ 29s | ✓ 24s |
| spec-fix | ✓ 55s | ✓ 4m 40s | ✗ 2m 57s |
| feature | ✓ 1m 11s | ✗ 5m 00s | ✓ 47s |
| **Passed** | **8/8** | **6/8** | **6/8** |
| Time | 3m 57s | 11m 55s | 5m 20s |
| Generation | 71.4 tok/s | 60.1 tok/s | 91.3 tok/s |
| Prompt reading | 855 tok/s | 3,507 tok/s | 5,186 tok/s |
| Tool-call errors | 0 | 9 | 1 |
| Memory | 22.3 GB, 45% on GPU | 6.6 GB, 100% on GPU | 4.2 GB, 100% on GPU |

The small models fit entirely on the GPU and read prompts much faster, but the larger mixture-of-
experts model solves more and needs fewer attempts. The small models' failures were real mistakes:
copying `read_file`'s line numbers into a data file, ignoring negative numbers, missing an edge case
of the spec, and running out of time.

Models sample their answers, so a task near the edge of a model's ability can pass in one run and
fail in the next (`qwen3.6-35b` scored 7/8 in an earlier run). Compare totals rather than single
tasks, and run twice before drawing conclusions.

## Sharing results

Results from other machines, especially Apple Silicon Macs and other GPUs, decide which models lcode
recommends. Run

```bash
lcode bench qwen3.6-35b --markdown
```

and paste the table into a
[model test report](https://github.com/nasser1941/lcode/issues/new?template=model_request.yml).

## JSON format

`--json FILE` writes one document per run. The schema is versioned: fields may be added, but
existing fields keep their meaning until `schema` changes.

```json
{
  "schema": 1,
  "lcode": "0.3.1",
  "ollama": "0.32.15",
  "server": "Ollama 0.32.15",
  "date": "2026-09-30T19:09:09+02:00",
  "hardware": {
    "os": "linux", "cpu": "13th Gen Intel(R) Core(TM) i9-13980HX", "ram_gib": 30.96,
    "gpu": "NVIDIA GeForce RTX 4080 Laptop GPU", "vram_gib": 11.99, "unified": false,
    "description": "NVIDIA GeForce RTX 4080 Laptop GPU (12 GB VRAM), 31 GB RAM"
  },
  "tasks": [{"id": "fix-bug", "title": "Fix a bug so the tests pass"}, …],
  "runs": [
    {
      "model": "qwen3.6-35b",
      "ollama_model": "lcode-qwen3.6-35b",
      "context": 32768,
      "num_batch": null,
      "think": true,
      "load_seconds": 16.2,
      "prompt_tps": 854.6,
      "memory_gb": 22.3,
      "gpu_percent": 45,
      "passed": 8,
      "total": 8,
      "seconds": 237.3,
      "generation_tps": 71.4,
      "tool_errors": 0,
      "usage": {"requests": 56, "prompt_tokens": 221522, "prompt_ns": 73377987000,
                "output_tokens": 11490, "output_ns": 161017657999},
      "tasks": [
        {"id": "fix-bug", "passed": true, "seconds": 21.9, "detail": "the tests pass",
         "steps": 5, "tool_errors": 0, "error": null},
        …
      ],
      "error": null,
      "interrupted": false
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `ollama`, `server` | The Ollama version, or `null` with [another model server](servers.md); `server` names the server either way |
| `runs[].model` | The name you gave (catalog key or Ollama tag); `ollama_model` is the model that ran |
| `runs[].context`, `num_batch`, `think` | The settings used. lcode lowers the batch size or context if the GPU runs out of memory, and reports what it ended up using |
| `runs[].memory_gb`, `gpu_percent` | From Ollama's `/api/ps`; `null` if unavailable |
| `runs[].generation_tps` | Output tokens per second across the tasks; `null` if Ollama reported no timings |
| `runs[].prompt_tps` | Prompt tokens per second for a fresh 4,000-token prompt (the faster of two readings); `null` if it couldn't be measured |
| `runs[].usage` | Totals over the tasks as Ollama reports them. `prompt_tokens` includes tokens served from the prompt cache |
| `runs[].tasks[].steps` | Model responses in the task |
| `runs[].tasks[].error` | Why the task stopped early (`timed out after 300s`, an Ollama error), otherwise `null` |
| `runs[].error` | Why the model didn't run at all (for example, not installed), otherwise `null` |
| `runs[].interrupted` | You pressed ++ctrl+c++; the results cover the tasks that finished |
