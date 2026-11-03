# Public benchmarks

`lcode bench` has [tasks of its own](bench.md) that say which model works best on your machine. To
measure lcode against published results and other agents, it also runs on two public benchmarks
that anyone can rerun:

- [Terminal-Bench 2.0](https://www.tbench.ai/): tasks in a terminal, from fixing a git repository
  to building software, recovering data or setting up servers.
- [SWE-bench Verified](https://www.swebench.com/): real bug fixes in Python projects such as Django,
  SymPy and matplotlib, checked by the project's own tests.

Both run on [Harbor](https://github.com/harbor-framework/harbor). Harbor starts each task in a
fresh Docker container. lcode is installed in that container and works on the task as one request
(`lcode -p "<the task>" --yolo`), with the model on your machine. Afterwards the task's tests decide
whether it passed.

## What you need

- **Docker**, running.
- **uv**: lcode runs Harbor with `uvx`, so there's nothing else to install.
- **Disk space** for the tasks' images: about 20 GB for the dev slice. Each SWE-bench task has an
  image of its own, 1–3 GB.
- **A model server the containers can reach.** lcode in a container finds your machine by itself:
  as `host.docker.internal` with Docker Desktop (macOS, Windows), or as the container's gateway on
  Linux. Ollama has to listen on more than `127.0.0.1` for that:
    - Ollama in Docker with its port published (`-p 11434:11434`) already does.
    - Ollama installed on Linux listens only on `127.0.0.1`. Run `sudo systemctl edit ollama`, add
      `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`, and restart it. That also opens it to
      your network, so keep port 11434 closed in your firewall.

## Running it

```bash
lcode bench --harbor dev                       # the dev slice with your model, 3 attempts per task
lcode bench --harbor dev qwen3.6-35b --context 64k
lcode bench --harbor dev --tasks cobol-modernization,django__django-11555 --attempts 1
lcode bench --harbor dev --list                # the slice's tasks
```

Tasks run one at a time, since they share your GPU. The context is your configured one unless you
give `--context`. A container can't see your GPU, so lcode there doesn't choose a context itself.

lcode in the container is the same version as yours: from PyPI, or a wheel built from your checkout
when you run lcode from one, so a change can be measured before it's released.

## The slices

| Slice | Terminal-Bench 2.0 | SWE-bench Verified | For |
|---|---|---|---|
| `dev` | 15 | 10 | everyday checks of a change |
| `held-out` | 14 | 11 | confirming a change helps beyond the tasks it was tuned on |

Both were drawn once, at random and by difficulty, from the tasks a local model can try in a night:

- Terminal-Bench without its model-training tasks, and with at most 30 minutes per task: 2 easy,
  10 medium and 3 hard tasks in the dev slice.
- SWE-bench Verified's two common difficulties: five fixes estimated under 15 minutes and five of
  15 minutes to an hour.

Don't tune lcode on the held-out set: once a change is chosen because it does well there, the set
no longer tells you whether changes generalize.

## What's reported

Two tasks, one attempt each, with qwen3.6-35b at 32K on a 12 GB laptop GPU:

```text
$ lcode bench --harbor dev --tasks prove-plus-comm,django__django-11555 --attempts 1
…
Task                  Benchmark          Passed      Time  Tokens in  Tokens out
prove-plus-comm       terminal-bench        1/1   0.6 min     27,827       1,494
django__django-11555  swebench-verified     0/1  19.9 min  2,370,217      38,508
pass@1 50% · pass^1 50%
```

With three attempts, the last line also gives each run's pass rate and the spread between them.

- **pass@1**: the share of attempts that passed, averaged over the tasks. It's the number to compare
  with leaderboards.
- **pass^3**: the share of tasks whose three attempts all passed. That shows how dependable lcode is,
  not just how often it gets lucky.
- **runs and spread**: the pass rate of each task's first attempt, its second and its third. Single
  runs of the same setup differ by several points; `--compare` ([below](#comparing-two-runs)) says
  whether a change between two results is more than that.
- **Time and tokens** per attempt, from lcode's own record of the request (its
  [trace](traces.md)). An attempt stopped at the task's time limit counts the tokens recorded
  until then, and a reply lcode stopped counts with an estimate. `–` means no attempt of the task
  has a record.
- **The model's speed**: how many tokens per second it generated, in the median attempt, from the
  same traces. A slower machine (short of memory, or busy with other programs) runs into the
  tasks' time limits more often.

Attempts whose container didn't start (a failed image download, say) count neither as passed nor
as failed. Attempts whose tests ran but didn't finish (a time limit, say) or gave no result count
as failed, as in Harbor's own mean, so a change that makes the tests hang can't hide a drop. The
report shows how many of each there were.

Everything is saved in `~/.local/state/lcode/bench/<slice>-<time>/`:

- `lcode-bench.json`: the results, with the lcode and Harbor versions, the model, the context, the
  machine, when the run started and how long it took, and the command that reproduces them.
  `--json FILE` saves a copy.
- Harbor's own results: each attempt's log, the task's test output, and lcode's trace and session.
  `uvx --from harbor harbor view <folder>` browses them.

## Comparing two runs

`lcode bench --compare` compares two saved results, for example the dev slice before and after a
change, given as their folders or their `lcode-bench.json`:

```bash
lcode bench --compare ~/.local/state/lcode/bench/dev-20261101-161623 ~/.local/state/lcode/bench/dev-20261105-201500
```

It says which lcode, model and context each one ran, warns when they don't match, lists the tasks
whose results changed (`3/3 → 1/3`), and compares pass@1 over the tasks both ran: within the noise,
better, or a drop beyond the noise.

While Harbor's results are still on the machine, `--compare` reads both runs from them again, so a
run saved by an older lcode counts its attempts the same way as a new one. It also gives each run's
model speed, and warns when one ran more than 10% slower: more of its attempts run into their time
limits, so part of a difference can come from the machine rather than from lcode.

The noise is how far pass@1 moves between runs of the same setup, estimated from how much each
task's attempts disagree: a task that always passes or always fails adds nothing, one that passes
half the time the most. A change counts when it's more than twice the standard error of the
difference. On the dev slice, with three attempts per task, that's about 9 points. `--compare` exits
with 1 when the second result dropped beyond the noise, so a script can stop on it.

**The release rule:** a release that changes lcode's prompts, tools or context handling ships only
if the dev slice doesn't drop beyond the noise against the last baseline, and the comparison goes on
its pull request. See the [release steps](contributing.md#releases).

## Other agents on the same model

`--agent` runs Claude Code, OpenCode or Codex on the same tasks, with the same model, context
window and time limits, so you can compare them with lcode:

```bash
lcode bench --harbor dev qwen3.6-35b --context 64k                      # lcode
lcode bench --harbor dev qwen3.6-35b --context 64k --agent claude-code
lcode bench --harbor dev qwen3.6-35b --context 64k --agent opencode
lcode bench --harbor dev qwen3.6-35b --context 64k --agent codex
```

Harbor installs the agent in each task's container, with approvals off. lcode sets the agent up the
way `ollama launch` does, with the Ollama on your machine:

- **Claude Code** uses Ollama's Anthropic-compatible API, with the model for every tier (subagents
  too) and `ollama launch claude`'s settings: no telemetry, error reports or feedback prompts. Like
  `ollama launch claude`, it doesn't tell Claude Code the context window.
- **OpenCode** uses an Ollama provider on Ollama's OpenAI-compatible API. The model is marked as a
  reasoning model, with the limits of its context window.
- **Codex** uses Ollama's Responses API and `ollama launch codex`'s model catalog. That catalog gives
  local models no base instructions, so Codex runs without its own system prompt, as it does with
  `ollama launch codex`.

These agents' APIs can't set the context window per request, and Ollama's default is small (4K on
GPUs under 24 GB), so lcode makes a copy of the model with the context set:
`lcode-qwen3.6-35b-64k` for 64K. It uses the model's files, so nothing is downloaded, and
`ollama rm lcode-qwen3.6-35b-64k` removes it. Ollama recommends at least 64K for these agents.

The time per task leaves out installing the agent, which Harbor does in every task's container: up to
three minutes for Claude Code, so its runs take longer overall. The results go to
`~/.local/state/lcode/bench/<slice>-<agent>-<time>/`, and `lcode-bench.json` adds the agent and its
version.

## With Harbor directly

`lcode.harbor:Lcode` is an ordinary Harbor agent, so every Harbor dataset and option works:

```bash
uvx --python 3.13 --from harbor==0.25.0 --with lcode-cli harbor run \
  -d terminal-bench@2.0 -a lcode.harbor:Lcode -m qwen3.6-35b --ak context=32768
```

Its options, each given with `--ak name=value`:

| Option | What it does |
|---|---|
| `context` | the context window, e.g. `32768` |
| `host` | Ollama's address as the container sees it, e.g. `http://192.168.1.20:11434` |
| `version` | the lcode version to install (default: the one Harbor runs with) |
| `wheel` | an lcode wheel on your machine to install instead |

Other model servers work as in lcode itself: `--ae LCODE_BACKEND=openai --ae LCODE_BASE_URL=…`.

## When Docker can't make networks

Harbor gives each task a network of its own. When Docker says *all predefined address pools have
been fully subnetted*, every address range it could use overlaps a route on your machine. A VPN that
sends all traffic through itself does that. Add `--docker-bridge`, and the tasks run on Docker's
default network instead. With Harbor directly, pass `--extra-docker-compose` with a file holding:

```yaml
services:
  main:
    network_mode: bridge
```
