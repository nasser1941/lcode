# Benchmarking models

`lcode bench` answers the most common question about local models: *which one works best for
coding on my machine?* It runs a fixed set of coding tasks against one or more models, three times
each, and reports how many they solved, how fast they ran and how much memory they used.

To compare with other agents on public benchmarks (Terminal-Bench 2.0 and SWE-bench Verified),
see [Public benchmarks](public-benchmarks.md).

```bash
lcode bench                           # the configured model
lcode bench qwen3.6-35b qwen3.5-9b    # compare models
lcode bench --tasks fix-bug,rename    # run some of the tasks
lcode bench --quick                   # only the eight quick tasks, once each
lcode bench --attempts 1              # each task once: quicker, but within the noise
lcode bench --json results.json       # also save machine-readable results
lcode bench --markdown                # also print a table to paste into a test report
```

`lcode bench` also runs against [other model servers](servers.md), such as llama-server or LM
Studio: `LCODE_BACKEND=llama.cpp lcode bench`. The server keeps the model loaded and doesn't report
its memory use, so those columns stay empty.

```text
lcode bench · NVIDIA GeForce RTX 4080 Laptop GPU (12 GB VRAM), 31 GB RAM · Ollama 0.40.13

lcode-qwen3.6-35b · 32K context · reasoning on · 8 tasks · 3 attempts each
  Loaded in 18s · ~23 GB, 42% on the GPU and the rest in system RAM · reads prompts at 905 tok/s
  Attempt 1 of 3
  ✓ fix-bug       Fix a bug so the tests pass         22s  the tests pass
  ✓ find-code     Answer with file:line                8s  next_wait at backoff.py:13
  ✓ write-script  Write a script from a spec          12s  correct output, also on data it hadn't seen
  ✓ rename        Rename a function across files      22s  its tests and extra checks pass
  ✓ precise-edit  One-line edit in a long file        10s  changed only service_087's timeout
  ✓ recover       Recover from failing commands       22s  used --source and dropped the invalid line
  ✓ spec-fix      Fix a function to match its spec    78s  follows the whole docstring
  ✓ feature       Add a feature across files          86s  priorities work, including old todos
  Attempt 2 of 3
  …
```

At the end it prints a table comparing the models (see [results](#results-on-a-12-gb-laptop) below).

A model usually takes 4 to 12 minutes per attempt at the quick tasks; each stops after 5 minutes,
and the [longer tasks](#longer-tasks) after 10 (`--timeout` sets one limit for all).

### Three attempts at each task

Models sample their answers, so a task near the edge of a model's ability can pass in one run and
fail in the next. One run of each task is within that noise, so `lcode bench` makes three attempts
at every task, each in a new folder and conversation: every task once, then every task again. The
summary adds:

| | |
|---|---|
| **pass@1** | The share of attempts that passed, averaged over the tasks: the chance that one attempt works |
| **pass^3** | The share of tasks that passed all three attempts: the ones you can count on |
| **Each attempt** | How many tasks passed in each attempt. The difference between the best and the worst is the spread between runs |

They're counted as in the [public benchmarks](public-benchmarks.md). `--attempts 1` runs each task
once, as `lcode bench` did before 0.57, in a third of the time; `--attempts 5` gives pass^5. In the
summary, a task's time is that of one attempt, on average, and *Time* is the total.

### More tasks and features

`--tasks find-concept` adds a task in a larger repository: 40 files of a small backend where the
model has to find a function by what it does. It's there to measure `--repo-map`, which gives the
model the [repository map](search.md), and `--code-search`, which indexes each task's folder for
[semantic search](search.md#semantic-code-search). Both are off in `lcode bench` by default, so
results compare across machines and versions.

### A second try, and a stronger model

`--retry` gives a failed task one more try, as a user would ask: "That didn't fix it: it still
doesn't work right. Look again." `--escalate MODEL` sets a [stronger model](usage.md#a-stronger-model-when-stuck)
that's asked when the model is stuck and, with `--retry`, before the second try, as `/escalate`
would: no questions asked, since the flag is your OK. Comparing the two shows what the stronger
model's advice adds to the same second chance:

```bash
lcode bench --tasks runtime-bug --retry --attempts 3
lcode bench --tasks runtime-bug --retry --attempts 3 --escalate ollama/qwen3.8-27b
```

The JSON report has, for each task, `first_passed` (with `--retry`, whether the first try passed)
and `escalations`.

### Long sessions

Each task normally starts a new conversation. `--session` runs them all in **one** conversation
instead, as a long working session would, so the context fills up and lcode's
[context management](how-it-works.md#context-management) has to work: the summary shows how often
it removed old tool output and how often it summarized the conversation. `--no-prune` turns the
removal off, to compare, and `--compact-at` sets the ceiling (`--compact-at 0`: only at 85%).

```bash
lcode bench --context 32k --session --rounds 3
lcode bench --context 32k --session --rounds 3 --no-prune
```

With `--session`, an attempt is one conversation, and `--attempts` is 1 unless you set it. `--rounds 3`
runs the tasks three times in it, 24 in one conversation, which fills a 32K window several
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

With a large window, `compact_at` decides when lcode makes room. In a 128K window, 24 tasks in one
conversation reach about 40K tokens, so the default ceiling of 64K never comes into play. Making
room earlier than needed doesn't cost tasks:

| 128K window, 24 tasks | No room made (peak ~40K) | `--compact-at 32k` |
|---|---|---|
| Conversation summarized | 0 times | 1 time (old tool output removed once) |
| Tasks solved | 21 of 24 | 21 of 24 |
| Time | 33 min | 27.5 min |
| Average prompt | 22.9K tokens | 16.6K tokens |

## The tasks

Every task runs in a new temporary folder, with every permission granted (`yolo` mode) and web
access off, and ends with an automatic check. The folders are deleted afterwards unless you pass
`--keep`, which also keeps each task's transcript (`lcode-bench.log`).

### Quick tasks

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

### Longer tasks

Good models now get every quick task right: qwen3.6-35b passed all 24 attempts. Four longer tasks,
closer to real work, tell good models from great ones:

| Task | The model has to | Passes when |
|---|---|---|
| `cli-tool` | Write `logsum.py`, which summarizes web server logs, from a spec: options, text and JSON output, standard input, malformed lines, unreadable files and exit codes | 14 runs on logs it never saw give the same output and exit status as a reference implementation |
| `flaky-test` | Find why a test fails in about half the runs, with the same code, and fix it without changing the tests | The original tests pass under 24 hash seeds, and so does a hidden test of the order the code promises |
| `repo-bug` | Fix a bug reported only by its symptom (a second quote gets the first one's shipping cost) in a 47-file repository, keeping its lookups cached | Hidden tests of the report, of the same cause elsewhere and of the cache pass, and so do the repository's own tests |
| `wide-feature` | Add monthly budgets to an expense tracker: a new command, the report in text and JSON, and the data file, with older data files still working | Its tests pass, and a scripted session from an older data file gives the expected output |
| `fast-dedupe` | Make a job that merges duplicate customer records fast, from hours to seconds for 200,000 records, without changing its output | On 60,000 records it never saw, it finishes within a minute and its output is the old job's, byte for byte |
| `runtime-bug` | Fix a camera wall whose window grows taller with every video frame and can't be made smaller again. Reading the code suggests band-aids; running it shows the cause | A hidden test: the window keeps its size, can be made smaller, and the video fits its cell |

They're where qwen3.6-35b misses: see the [results](#results-on-a-12-gb-laptop). In an earlier run
of the six it lost one `fast-dedupe` attempt to lcode itself: after lcode made room in the context,
the model lost track and repeated itself until the loop breaker stopped it.

Each rewards what a careful developer does: reading the whole spec, running a flaky test more than
once, fixing the cause rather than the symptom, trying old data, and comparing the new output with
the old. The longer tasks stop after 10 minutes instead of 5. `--quick` leaves them out and runs the
quick tasks once, as `lcode bench` did before 0.57.

## What's measured

| | |
|---|---|
| **Passed** | Attempts that solved their task within the time limit; with several, also pass@1, pass^3 and each attempt's ([above](#three-attempts-at-each-task)) |
| **Time** | Total time for the tasks, every attempt (loading the model is reported separately) |
| **Generation** | Output tokens per second, across all the model's responses |
| **Prompt reading** | Prompt tokens per second: how fast the model reads files and tool output. Measured per model with fresh 4,000-token prompts (the faster of two readings, since the first one after loading includes warm-up), because during the tasks Ollama serves most of each request from its prompt cache |
| **Tool-call errors** | Tool calls that failed (wrong arguments, editing without reading first, unmatched edits) or that the model wrote as invalid JSON |
| **Memory** | The model's memory and how much of it is on the GPU, counted as in the [line after a load](usage.md#whats-happening-under-the-hood). Ollama leaves out what stays mapped from the model's file in system RAM, so for a model in `lcode models` that's partly off the GPU, it's lcode's estimate of the whole model (`~23 GB`). Otherwise it's what Ollama says |

Results depend on the context window, so `lcode bench` uses **32K** by default on every machine,
which makes results comparable. When comparing models, each one is unloaded before the next starts
so every model gets the whole GPU; lcode warns if other models are already loaded. Use `--context 128k` to measure a larger window, and `--no-think`
to measure a model without reasoning.

## Results on a 12 GB laptop

RTX 4080 Laptop GPU (12 GB) with 31 GB of RAM, Ollama 0.40.13, 32K context, reasoning on, lcode
0.58.0, three attempts at each task:

| | `qwen3.6-35b` |
|---|---|
| fix-bug | ✓✓✓ 25s |
| find-code | ✓✓✓ 14s |
| write-script | ✓✓✓ 15s |
| rename | ✓✓✓ 25s |
| precise-edit | ✓✓✓ 13s |
| recover | ✓✓✓ 23s |
| spec-fix | ✗✓✓ 1m 16s |
| feature | ✓✓✓ 1m 40s |
| cli-tool | ✓✓✗ 1m 23s |
| flaky-test | ✓✓✓ 39s |
| repo-bug | ✓✓✗ 2m 53s |
| wide-feature | ✓✓✓ 4m 17s |
| fast-dedupe | ✓✓✓ 1m 25s |
| runtime-bug | ✗✗✗ 5m 30s |
| **Passed** | **36/42** |
| pass@1 · pass^3 | 86% · 71% |
| Each attempt | 12/14, 13/14, 11/14 |
| Time | 62m 47s |
| Generation | 64.0 tok/s |
| Prompt reading | 851 tok/s |
| Tool-call errors | 4 |
| Memory | ~23 GB, 42% on GPU |

What it missed:
- one `spec-fix` attempt fixed the reported case but not all the docstring's rules;
- one `cli-tool` exited with 1 after skipping malformed lines, where the spec says 0;
- one `repo-bug` attempt went round in circles until lcode stopped it;
- every `runtime-bug` attempt settled for a band-aid: the window still grew, or still couldn't be
  made smaller.

The table shows memory as lcode counts it now. lcode 0.58.0 printed Ollama's count,
`10.5 GB, 98% on GPU`, which leaves out the ~12 GB of experts in system RAM.

Two `wide-feature` attempts were first counted as failed. Their `budget list` printed `No budgets
set.` without budgets, and one listed the budgeted categories first. The task asks for neither
behavior, so the check now accepts both (0.59.0), and the table counts them as passed.

### Earlier: the quick tasks, one attempt each

The quick tasks with three models, Ollama 0.32.15, 32K context, reasoning on, one attempt each (lcode
0.3):

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
fail in the next (`qwen3.6-35b` scored 7/8 in an earlier run), which is why `lcode bench` now makes
three attempts at each task. Compare pass@1 and pass^3 rather than single tasks.

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
  "lcode": "0.57.0",
  "ollama": "0.40.13",
  "server": "Ollama 0.40.13",
  "date": "2026-11-02T09:44:14+01:00",
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
      "load_seconds": 17.8,
      "prompt_tps": 904.6,
      "memory_gb": 10.4,
      "gpu_percent": 98,
      "estimated_memory_gib": 22.7,
      "estimated_gpu_percent": 42,
      "attempts": 3,
      "passed": 24,
      "total": 24,
      "seconds": 756.2,
      "generation_tps": 72.2,
      "tool_errors": 2,
      "summary": {"pass_at_1": 1.0, "pass^3": 1.0, "runs": [1.0, 1.0, 1.0], "spread": 0.0},
      "usage": {"requests": 184, "prompt_tokens": 1058154, "prompt_ns": 199916607993,
                "output_tokens": 38683, "output_ns": 535438114998},
      "tasks": [
        {"id": "fix-bug", "passed": true, "seconds": 21.5, "detail": "the tests pass",
         "steps": 5, "tool_errors": 0, "error": null, "attempt": 1},
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
| `runs[].memory_gb`, `gpu_percent` | The model's memory in GB and the share of it on the GPU, as Ollama's `/api/ps` counts them; `null` if unavailable. Ollama 0.40 leaves out what stays mapped from the model's file in system RAM: `qwen3.6-35b` at 32K on a 12 GB GPU shows as 10.4 GB, 98% of it on the GPU |
| `runs[].estimated_memory_gib`, `estimated_gpu_percent` | When Ollama's count leaves out part of a model in `lcode models`: lcode's estimate of the whole model in GiB, and the share of it on the GPU, as the summary shows them (`~23 GB, 42% on GPU`). `null` when Ollama counts the whole model (all of it on the GPU, or a model outside the catalog) and in older results |
| `runs[].generation_tps` | Output tokens per second across the tasks; `null` if Ollama reported no timings |
| `runs[].prompt_tps` | Prompt tokens per second for a fresh 4,000-token prompt (the faster of two readings); `null` if it couldn't be measured |
| `runs[].usage` | Totals over the tasks as Ollama reports them. `prompt_tokens` includes tokens served from the prompt cache |
| `runs[].attempts` | Attempts at each task (`--attempts`) |
| `runs[].summary` | `pass_at_1`, `pass^3` (with the number of attempts), `runs` (each attempt's pass rate) and `spread` (the best minus the worst), counted as in the [public benchmarks](public-benchmarks.md); `null` where there's nothing to count |
| `runs[].tasks[]` | One entry per attempt at a task; `attempt` says which (1, 2, 3) |
| `runs[].tasks[].steps` | Model responses in the task |
| `runs[].tasks[].error` | Why the task stopped early (`timed out after 300s`, an Ollama error), otherwise `null` |
| `runs[].error` | Why the model didn't run at all (for example, not installed), otherwise `null` |
| `runs[].interrupted` | You pressed ++ctrl+c++; the results cover the tasks that finished |
