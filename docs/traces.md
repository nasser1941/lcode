# Run traces

lcode records each request in code mode as it runs: every model call and every tool call, with
its times and tokens. A trace shows where a slow or failed request spent its time and went wrong,
it can go with a bug report, and it can be **replayed** without a model to check a change to lcode
against real runs.

```bash
lcode trace                 # the newest 20 requests
lcode trace show 1          # the newest one, step by step (or just: lcode trace 1)
lcode trace show 1 -v       # also the model's answers and the start of each result
lcode trace export 1        # a copy for a bug report, with secrets and personal details masked
lcode trace replay 1        # run it again with the recorded answers instead of a model
lcode trace path            # the folder the traces are in
```

## What's recorded

- **The request** as you typed it, and what lcode added to the conversation while it ran: a
  failed check, a note about a malformed tool call, a note you typed while it worked.
- **Each model call:** the answer (text, reasoning and tool calls), the time to the first token,
  the total time, and the tokens in and out. With Ollama, also how long loading the model and
  reading the prompt took.
- **Each tool call:** its arguments, the result the model got, and how long it took. A result
  longer than 200,000 characters keeps its start and end. When lcode cut a long output for the
  model, the result says where the full output was saved.
- **The setup:** the model, its context window and the model's own sampling settings
  (temperature, top_p and so on: lcode doesn't change them), the settings that change how lcode
  works (checks, review, `compact_at`…), the permission mode, the lcode version, the git commit
  the folder was at, and a fingerprint of the system prompt and of the tool definitions.
- **Subagents:** their model and tool calls, each subagent in a lane of its own.

## Where, and for how long

Traces stay on your machine: nothing is sent anywhere. Each request is one file in
`~/.local/state/lcode/traces/` (under `$LCODE_HOME` when that's set), named after its session and
its number in it: `20261031-142233-a1b2c3-2.jsonl` is the second request of that session. A
file holds one JSON object per line, written as the request runs, so a request that crashed
still has everything up to the crash.

- Traces older than **30 days** are deleted.
- When all of them take more than **200 MB**, the oldest go first.
- One trace stops growing at **20 MB**; it still records how the request ended.

A trace holds what the session saw, like a saved session does: the contents of files the model
read, command output, and any secrets in them. Share one with `lcode trace export`, which masks
them.

**Turn traces off** with `lcode config set trace false`. `lcode bench` doesn't record its runs.

Recording takes a few milliseconds per request (2 ms for the 88-second request below): each trace
says how long in its last line (`trace_seconds`), and `lcode trace show` as *recording*.

## Reading a trace

```text
$ lcode trace
 #  When        Request                                      Model                Steps       Time  Status
 1  1 min ago   Use two explore agents at the same time: on… lcode-qwen3.6-35b        2 1 min 13 s  success
 2  5 min ago   The tests fail. Find out why and fix the co… lcode-qwen3.6-35b        6 1 min 28 s  success
2 trace(s), 25 KB, in ~/.local/state/lcode/traces (kept 30 days). lcode trace show 1 · export 1 · replay 1
```

```text
$ lcode trace show 2
Trace 20261031-184203-3e0674-1 · 2026-10-31 18:42 · success in 1 min 28 s
Request: The tests fail. Find out why and fix the code (not the tests).
lcode-qwen3.6-35b · 256K context · thinking on · yolo mode · lcode 0.53.0 · ~/code/textstats at 958102f
Sampling (the model's own): top_k 20, top_p 0.95, min_p 0, temperature 0.6

    1.3 s  model 1  10.2K in · 206 out · first token 1 min 3 s (loading 21 s, reading 41 s) · 1 min 6 s  → read_file, read_file
   67.4 s    read_file src/textstats/stats.py  13 lines  0.0 s
   67.4 s    read_file tests/test_stats.py  13 lines  0.0 s
   67.4 s  model 2  10.8K in · 50 out · first token 1.7 s · 2.4 s  → bash
   69.9 s    bash python3 -m pytest -q 2>&1  36 lines  0.3 s
   70.2 s  model 3  11.2K in · 364 out · first token 1.6 s · 7.6 s  → edit_file
   77.8 s    edit_file src/textstats/stats.py  8 lines  0.8 s
   78.6 s  model 4  11.7K in · 195 out · first token 1.2 s · 4.1 s  → edit_file
   82.8 s    edit_file src/textstats/stats.py  8 lines  0.3 s
   83.1 s  model 5  12.0K in · 51 out · first token 1.4 s · 2.1 s  → bash
   85.2 s    bash python3 -m pytest -q 2>&1  4 lines  0.2 s
   85.4 s  model 6  12.1K in · 144 out · first token 0.8 s · 3.1 s  → answer

success · 6 model call(s) · 68.0K tokens in, 1.0K out · model 1 min 25 s · tools 1.6 s (6 calls) · recording 2 ms
Changed: src/textstats/stats.py
```

Each line shows when its step started. Here the first model call waited a minute for
its first token: 21 seconds loading the model into memory and 41 seconds reading the prompt into
a fresh 256K context. After that, each step answered within two seconds. A subagent's steps
show its lane (`agent 1`, `agent 2`) and the task it was given.

The file itself is plain JSON lines, one per event, for scripts: `start`, `env` (the git commit
and sampling settings), `message`, `model`, `tool`, `check`, `escalate`, `compacted`, and `end`
with the totals.

## Exporting for a bug report

`lcode trace export 1` writes `lcode-trace-<id>.jsonl` in the current folder (`-o FILE` names
another; `-o -` prints it) with these masked:

- **secrets:** keys and tokens lcode's [secret check](usage.md#secrets-in-commits) recognizes,
  passwords in web addresses and in `password = …` style settings, private keys, and the values
  of your environment variables whose names say KEY, TOKEN, SECRET, PASSWORD and the like,
  wherever they turn up;
- **your home folder** (shown as `~`) and other users' home folders;
- **names:** your user name, your full name, the name and email address in your git settings,
  and the computer's name;
- **email addresses and IP addresses** (except 127.0.0.1).

It says how many of each it masked. Masking errs on the side of too much, but it can still miss
something, so read the file before you share it.

## Replaying a trace

`lcode trace replay` runs a recorded request again, with the recorded answers standing in for the
model: no GPU and no model server needed. The tools run again in a temporary copy of the folder,
and lcode compares each result with the recorded one. It answers "does this change to lcode still
behave the same on real runs?" and, when it doesn't, shows exactly where.

```text
$ lcode trace replay 2
Replaying 20261031-184203-3e0674-1 (6 model answer(s)) in ~/code/textstats at the recorded commit 958102f.
Shell commands get their recorded results; --commands runs them again in the copy.
Tool calls run again: 4, with the same result: 4. Given their recorded result: 2. Model calls: 6 recorded, 6 in the replay.
Status: recorded success, now success
No differences.
```

When something differs, the replay shows where, with the recorded and the new result:

```text
Tool calls run again: 4, with the same result: 3. Given their recorded result: 2. Model calls: 6 recorded, 6 in the replay.
Status: recorded success, now success
1 difference(s):
  step 1 · read_file src/textstats/stats.py
    @@ -1,2 +1,2 @@
    -     1     """Small text statistics."""
    +     1     """Small text statistics: word counts and lengths."""
          2
```

- **Where it runs:** never in your folder. In a copy of the git repository at the commit the
  request started from (the copy doesn't have changes that weren't committed then; the replay
  says so), or, outside git, in a copy of the folder as it is now. `--repo DIR` replays in a copy
  of another folder, and `--keep` keeps the copy afterwards. Recorded paths into your folder point
  into the copy, file tools can't reach anything outside it, and the copy has no git remotes.
- **What runs again:** reading, searching, writing and editing files, the todo list, skills, the
  repository map, subagents and plans.
- **What gets its recorded result:** shell commands and the project's checks (`--commands` runs
  them again, in the copy, as they were recorded: use it for traces whose commands you'd run
  yourself), web searches and pages, MCP tools, language servers, code search,
  images and screenshots, memory notes and a stronger model's advice; and whatever you answered,
  such as a refused permission or your decision on a plan.
- **What's compared:** each tool call and its result, lcode's notes to the model, the checks' exit
  codes, the number of model calls, and how the request ended. The folder's path, the names of
  saved outputs, timings, and lines drawn to the terminal's width (pytest's `=====`) don't count.
  Commands run with the terminal size the recorded ones had.

It exits with 0 when nothing differs and 1 when something does, so it fits in a script.

A replay doesn't repeat notes you typed while the request ran. Subagents that ran at the same time
replay one after another. An exported trace has its folder masked: replay it with `--repo`.

### In tests

Any trace replays in a test, without a model server:

```python
from pathlib import Path

from lcode import replay, traces


def test_the_parser_fix_still_replays(tmp_path):
    folder = tmp_path / "parser"
    ...  # the files as they were when the trace was recorded
    report = replay.run(traces.load(Path("tests/traces/parser-fix.jsonl")), folder)
    assert report.ok, report.differences
```

`report.differences` lists each difference with its step, the call, and the recorded and new
result.
