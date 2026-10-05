# Subagents

A single conversation used to do everything. Exploring a large codebase, reading logs or comparing
approaches fills the context window with material that's only needed for a moment, and that hurts
most at 32K to 128K.

Now the model can hand a task to a **subagent**: a fresh agent with its own empty context that
works on the task alone and returns only its report. The main conversation keeps the answer, not
the 40 files the subagent read to find it.

```text
❯ how do the payment webhooks get verified, and where would I add retries?
  ✓ ↳ explore: find webhook verification · 9 tools · 41s
  ✓ ↳ plan: retries for webhook handlers · 6 tools · 38s
Webhooks are verified in `payments/webhooks.py:88` …
```

## Agent types

| Type | Does | Tools |
|---|---|---|
| `explore` | Finds code and answers questions about the codebase | Read-only: reading and searching files, read-only shell commands, the web |
| `plan` | Works out a step-by-step plan for a change, without making it | Read-only, like explore |
| `worker` | Makes a self-contained change and verifies it | All of lcode's tools |

The model picks the type and writes the task. A subagent can't see the conversation, so the model
is told to describe the task completely. Subagents can't start subagents of their own, and they
don't save [memory](memory.md) notes; they do see the notes and `AGENTS.md`.

To ask for a specific agent yourself, mention it: `@plan add a dark mode toggle` (or
`@agent-plan …`).

## What you see

Each subagent gets one progress line with its current action, its tool count and its time. When it
finishes, the line stays as a summary. `/agents` lists the agent types and the subagents of this
session; `/agents 2` shows what the second one did, call by call.

Ctrl+C stops the subagents and the request.

## Safety

Subagents work under the session's rules:

- **Permissions.** They use the session's permission mode. Every prompt names the agent
  (`worker agent › Run command`) and is asked in the main terminal, one at a time, even when
  several agents run at once.
- **Read-only agents** (`explore`, `plan`) can't edit files, and lcode refuses any shell command that
  isn't on the read-only list.
- **Undo.** A worker's changes are part of your request's checkpoint, so `/undo` takes them back.
- **Sandbox.** With the [sandbox](sandbox.md) on, their commands run in it too.

## Several at once

When the model calls several agents in one response, lcode can run them at the same time. Whether
that helps depends on Ollama:

- By default **Ollama runs one request at a time**. Two agents would take turns, and each turn would
  push the other's conversation out of Ollama's prompt cache, which makes both slower. So lcode
  runs subagents one after another by default (`max_parallel_agents = 1`).
- Ollama runs several requests at once when its server is started with `OLLAMA_NUM_PARALLEL`. Each
  parallel request gets **its own context cache**, so memory use grows with every slot.

To run two at once:

```bash
OLLAMA_NUM_PARALLEL=2 ollama serve
lcode config set max_parallel_agents 2
```

`lcode doctor` estimates how many fit with your model and context window:

```text
✓ Agents     subagents one at a time
!            2 could run at once here (2 × 256K context fits): set OLLAMA_NUM_PARALLEL=2 on the
             Ollama server and max_parallel_agents 2
```

### On a 12 GB GPU

Subagents use **the session's model and context window**. A different window, or a different
model, would make Ollama reload the model, and on one GPU that costs more than the subagent saves.
Some guidance:

- With one slot (the default), a subagent's requests push the main conversation out of Ollama's
  cache, so the main model reads its whole context again afterwards. qwen3.6-35b reads about 850
  tokens a second on a 12 GB laptop GPU: ~35 s for a 30K-token conversation. Delegating pays off when the subagent
  would otherwise add much more than that to the main context.
- Two slots keep the main conversation cached while a subagent works. qwen3.6-35b's context cache
  is small, so two slots of 128K fit in 12 GB of VRAM plus system RAM, but the extra cache sits in
  system RAM and slows the model down.
- Smaller context windows leave room for more slots: `/context 64k` with
  `OLLAMA_NUM_PARALLEL=3`.

## Workers that run together

A worker edits your working tree directly. When **two or more workers** (or other agents that can
change files) run at the same time, each gets its own **git worktree** instead, created from your
working tree as it is now, uncommitted and untracked files included. When a worker finishes,
lcode shows its changes as a diff and asks before applying them to your working tree. The diff is
applied automatically in `auto-edit` and `yolo` mode. If the diff doesn't apply cleanly, lcode
saves it as a patch file and tells the model where.

Worktrees live in `.git/lcode-worktrees/` and are removed when the worker is done. Outside a git
repository, or before the first commit, workers run one after another in the working tree.

## Custom agents

Define your own agent types as markdown files: the frontmatter describes the agent, and the body is
its instructions.

```markdown
---
description: reviews a change for bugs and missing tests (read-only)
tools: read-only
max_steps: 30
---
Review the change described in the task. Read the changed code and its callers.
Report bugs first, with `path:line`, then missing tests, then style. Don't fix anything.
```

| Key | Meaning |
|---|---|
| `description` | What it does; the main model reads this to decide when to use it (required) |
| `tools` | `all` (default), `read-only`, or a list such as `read_file, grep, bash` |
| `read_only` | `true` to allow only read-only shell commands (set by `tools: read-only`) |
| `max_steps` | Tool-calling steps before it must report (default 60, at most 200) |
| `model` | Another Ollama model for this agent. Only worth it if both fit in memory at once |
| `isolation` | `auto` (default: a worktree only when running with other editing agents), `worktree` (always) or `none` |

| Folder | Agents |
|---|---|
| `.lcode/agents/<name>.md` in the repository | For this repository; commit them to share them |
| `~/.config/lcode/agents/<name>.md` | For every repository |

The file name is the agent's name. A repository's agent overrides one of yours with the same name,
and both override the built-in types. `/agents` and `lcode doctor` point out files that couldn't be
read.

## Settings

| Setting | Default | |
|---|---|---|
| `subagents` | `true` | Let the model use the `agent` tool |
| `max_parallel_agents` | `1` | How many subagents run at the same time (see [Several at once](#several-at-once)) |
