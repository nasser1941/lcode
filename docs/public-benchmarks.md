# Public benchmarks

`lcode bench` has eight small tasks of its own, and good models solve all of them. To see progress
past that, and to compare with other agents, lcode runs on two public benchmarks that anyone can
rerun:

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
  runs of the same setup differ by several points, so differences smaller than the spread are noise.
- **Time and tokens** per attempt, from lcode's own record of the request (its
  [trace](traces.md)).

Attempts whose container didn't start (a failed image download, say) count neither as passed nor
as failed. The report shows how many there were.

Everything is saved in `~/.local/state/lcode/bench/<slice>-<time>/`:

- `lcode-bench.json`: the results, with the lcode and Harbor versions, the model, the context, the
  machine, the date and the command that reproduces them. `--json FILE` saves a copy.
- Harbor's own results: each attempt's log, the task's test output, and lcode's trace and session.
  `uvx --from harbor harbor view <folder>` browses them.

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
