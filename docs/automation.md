# Automation

lcode can also run without a person at the keyboard:
- **Scripts and CI** get structured results from `lcode -p`.
- **Programs** use it from Python.
- **A GitHub Action** answers `@lcode` in issues and pull requests on your own GPU machine.

In all three, nothing waits for an answer: anything that would need your permission is refused,
unless the permission mode, a [permission rule](hooks.md#permission-rules) or your program allows it.

## Structured output

```bash
lcode -p "Fix the failing test in tests/test_parser.py" --auto-edit --output json
lcode -p "Explain the retry logic" --output stream-json --max-steps 20
```

| Option | |
|---|---|
| `--output json` | One JSON object when the request is done |
| `--output stream-json` | One JSON event per line as the request runs, ending with the same result object |
| `--max-steps N` | Stop after N model steps (status `max_steps`) |
| `--allowed-tools LIST` | Only these tools, e.g. `"read_file,grep,glob,bash"` |

lcode's usual output goes to stderr, so stdout holds only the JSON. The exit code says how it
went: `0` success, `1` an error, `3` stopped at `--max-steps`, `4` stopped because the model
[kept repeating itself](how-it-works.md#when-the-model-repeats-itself), `130` interrupted.

### The result

```json
{
  "type": "result",
  "schema": 1,
  "lcode": "0.18.0",
  "status": "success",
  "text": "Two bugs fixed in `calc.py`: …",
  "session_id": "20261006-101503-7f3a",
  "model": "lcode-qwen3.6-35b",
  "tool_calls": [
    {"id": "call_ab9fmzq1", "name": "read_file", "arguments": {"path": "calc.py"}, "error": false, "output": "…"},
    {"id": "call_lhhyt2jn", "name": "bash", "arguments": {"command": "pytest -q"}, "error": true, "output": "This needs the user's permission, …"}
  ],
  "files_changed": [{"status": "M", "path": "calc.py"}],
  "usage": {"requests": 6, "prompt_tokens": 35092, "output_tokens": 942},
  "seconds": 30.67,
  "error": ""
}
```

| Field | |
|---|---|
| `status` | `success`, `error`, `max_steps`, `loop` (the model kept repeating the same call) or `interrupted` |
| `text` | The model's final answer |
| `tool_calls` | Every tool call, with its arguments and output (the first 4,000 characters). `error` is true when the call failed or wasn't allowed |
| `files_changed` | Files the request added (`A`), modified (`M`) or deleted (`D`), shell commands included |
| `usage` | Model requests and tokens; `prompt_tokens` includes tokens served from the prompt cache |
| `error` | What went wrong, when `status` is `error` |
| `session_id` | Continue the conversation later with `lcode --resume <id>` |

A failure before the request starts (no model server, an unknown model) prints
`{"type": "result", "status": "error", "error": "…"}` and exits with `1`.

### The events (stream-json)

```json
{"type": "start", "schema": 1, "lcode": "0.18.0", "session_id": "…", "model": "lcode-qwen3.6-35b", "cwd": "/src/app"}
{"type": "assistant", "text": "", "tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "calc.py"}}]}
{"type": "tool_result", "id": "call_1", "name": "read_file", "error": false, "output": "…"}
{"type": "assistant", "text": "The bug is in clamp(): …", "tool_calls": []}
{"type": "result", "status": "success", …}
```

An `assistant` event comes after each model step; a `tool_result` follows each of its calls, with
the same `id`.

## Python

```python
from lcode import Session

with Session("path/to/repo", permission_mode="auto-edit") as session:
    result = session.run("Fix the failing test in tests/test_parser.py")
    print(result.status, result.text)
    for change in result.files_changed:
        print(change["status"], change["path"])
```

- **A session is one conversation:** each `run` continues it.
- **Events while it runs:** `session.stream(prompt)` yields the events above, ending with the result.
- **Permissions:** pass `approve` to decide permission requests. It gets
  `{"kind": "bash", "title": "…", "target": "pytest -q"}`; without it, every request is refused.
- **Options:** `model`, `context`, `permission_mode`, `allowed_tools`, `max_steps`, `think`,
  `web`, `memory`, `mcp` and `sandbox`. Leave one out to use your
  [configuration](configuration.md).
- **lcode's own output:** `verbose=True` shows it on stderr.
- **When lcode can't start** (no model server, a missing model): `lcode.SetupError`.

[`examples/api/fix_tests.py`](https://github.com/nasser1941/lcode/blob/main/examples/api/fix_tests.py)
runs a repository's tests, lets the model fix what fails and approves only the test command:

```text
$ python examples/api/fix_tests.py ~/src/calc
✓ read_file:      1	def average(values):
✓ bash: ============================= test session starts ==============================
✓ edit_file: Edited calc.py (1 replacement(s)). Result:
✓ edit_file: Edited calc.py (1 replacement(s)). Result:
✓ bash: ============================= test session starts ==============================

success in 22s, 5 model requests
  M calc.py
```

## GitHub Action

The action runs lcode on a **self-hosted runner**, a machine of yours with a GPU and Ollama (or
[another model server](servers.md)). The code and the model stay on that machine; only the
replies and the commits go to GitHub.

- **Someone comments `@lcode …`** on an issue or a pull request. lcode reacts with 👀, does what
  they ask and replies.
  - When it changed files on an **issue**, it commits them to a new branch and opens a pull
    request that closes the issue.
  - On a **pull request**, it pushes the commit to that pull request's branch.
- **With `review: true`**, every new or updated pull request gets a review as a comment.

```yaml
# .github/workflows/lcode.yml
name: lcode
on:
  issue_comment:
    types: [created]
  pull_request:
    types: [opened, synchronize]

permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  lcode:
    if: github.event_name == 'pull_request' || contains(github.event.comment.body, '@lcode')
    runs-on: [self-hosted, gpu]
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: nasser1941/lcode@v0.18.0
        with:
          review: true
          allow-commands: "pytest*, npm test"
```

| Input | Default | |
|---|---|---|
| `model` | the runner's config | The model to use |
| `trigger` | `@lcode` | The word that calls lcode in a comment |
| `review` | `false` | Review pull requests when they're opened or updated |
| `review-forks` | `false` | Also review pull requests from forks |
| `author-associations` | `OWNER,MEMBER,COLLABORATOR` | Who can call lcode |
| `permission-mode` | `auto-edit` | `auto-edit`: file changes, and only the shell commands in `allow-commands`. `yolo`: anything |
| `allow-commands` | | Shell commands the model may run, with `*` wildcards |
| `max-steps` | `60` | Model steps per request |
| `lcode-version` | the action's | The lcode version to run |

### Setting up the runner

1. [Add a self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners)
   to the repository, and give it a label such as `gpu`.
2. On that machine, as the runner's user, install lcode and a model:
   `curl -fsSL https://nasser1941.github.io/lcode/install.sh | bash`. The action runs lcode with
   [uv](https://docs.astral.sh/uv/) when it's there, otherwise with the `lcode` it finds, otherwise
   it installs lcode with pip.
3. Settings and [permission rules](hooks.md#permission-rules) for the action go in that user's
   `~/.config/lcode/config.toml`. A repository's own `.lcode/settings.toml` isn't used, because
   nobody approved it on the runner.

### Safety

- **Who can trigger it.** Only people with an owner, member or collaborator association, and
  never bots.
- **Other people's text.** The issue's description and the code may have been written by anyone.
  lcode tells the model to treat instructions in them as information, but a model can still be
  talked into things. Keep `permission-mode: auto-edit` with a short `allow-commands` list,
  unless the runner is isolated and disposable.
- **Forks.** Pull requests from forks aren't reviewed unless you set `review-forks`: their code
  would run through lcode's tools on your machine.
- **Git.** lcode does the git steps itself: the model can't commit, push or switch branches, and
  commits are made as `lcode`.
- **Public repositories.** GitHub
  [advises against](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#hardening-for-self-hosted-runners)
  self-hosted runners for public repositories, since anyone can open a pull request. Use the
  action on private repositories, or on a runner you can afford to lose.
