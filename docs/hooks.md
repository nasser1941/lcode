# Hooks and permission rules

Some things should happen whatever the model does: format every edited file, never force-push,
run the tests when a request is done. Hooks and permission rules do that without relying on the
model to follow instructions.

They go in `~/.config/lcode/config.toml` (yours, for every repository) or in a repository's
`.lcode/settings.toml` (shared with everyone who works on it). A repository's settings are used only
after you [approve them](commands.md#approving-a-repositorys-commands-skills-and-agents), because
hooks run commands; lcode asks again whenever the file changes.

## Permission rules

Rules are checked before lcode asks for permission. An **allow** rule lets an action through
without asking; a **deny** rule blocks it, even in `yolo` mode and even for commands that are
otherwise read-only. Deny always wins.

```toml
[permissions]
allow = ["bash:pytest *", "bash:npm test", "bash:ruff *", "edit:src/**", "web:docs.python.org"]
deny = ["bash:git push --force*", "bash:git push -f*", "bash:rm -rf /*", "edit:.env", "edit:**/*.pem"]
```

| Rule | Matches |
|---|---|
| `bash:<pattern>` | Shell commands, with `*` and `?` wildcards. A leading `cd <folder> &&` is ignored. A deny rule matches any part of a chained command (`git add . && git push --force`); an allow rule needs the whole command, or every part of it, to be allowed |
| `edit:<pattern>` | Files the model writes or edits, relative to the repository's root. A pattern without a `/` matches the file name anywhere (`.env`, `*.pem`); `src/**` matches everything under `src` |
| `web:<domain>` | Pages fetched from a domain; `web:search` for searches (only asked about with `web = "ask"`) |
| `mcp:<server>:<tool>` | An [MCP](mcp.md) server's tools, e.g. `mcp:github:list_*` |

When a rule blocks something, the model is told which rule, and not to work around it.

## Hooks

```toml
[[hooks]]
event = "after_tool"
tools = ["edit_file", "write_file"]
paths = ["*.py"]
command = "ruff format {path} && ruff check --fix {path}"
```

| Event | Runs | What it can do |
|---|---|---|
| `before_tool` | Before a tool call | Exit with code **2** to block the call; what the hook printed tells the model why |
| `after_tool` | After a tool call | Its output goes to the model when it exits with an error, or always with `feedback = true` |
| `after_request` | When a request is done | Its output is shown to you |
| `session_start` | When a session starts | Its output is shown, and with `feedback = true` given to the model |
| `notification` | When lcode waits for your answer, and when a request that took over 30 seconds is done | Show a desktop notification, ring a bell |

| Key | |
|---|---|
| `event` | One of the events above (required) |
| `command` | Run with `bash -c` in the working directory (required). `{path}` and `{tool}` are replaced, quoted |
| `tools` | Only for these tools (wildcards allowed), e.g. `["edit_file", "write_file"]` or `["mcp__*"]` |
| `paths` | Only for these files, like the `edit:` rules: `["*.py"]`, `["src/**"]` |
| `timeout` | Seconds before the hook is stopped (default 60) |
| `feedback` | `true` to give the hook's output to the model even when it succeeds |

Each hook gets the event as JSON on its standard input (the tool, its arguments, the file's full
path, the result for `after_tool`, the working directory), and in the environment variables
`LCODE_EVENT`, `LCODE_TOOL` and `LCODE_PATH`. Hooks run on your machine, also with the
[sandbox](sandbox.md) on.

## Recipes

**Format and lint Python files after every edit** (lint problems go back to the model):

```toml
[[hooks]]
event = "after_tool"
tools = ["edit_file", "write_file"]
paths = ["*.py"]
command = "ruff format --quiet {path} && ruff check --fix --quiet {path}"
```

**Prettier for JavaScript and TypeScript:**

```toml
[[hooks]]
event = "after_tool"
tools = ["edit_file", "write_file"]
paths = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.css", "*.json"]
command = "npx --no-install prettier --write {path} > /dev/null"
```

**Never force-push.** A rule is enough:

```toml
[permissions]
deny = ["bash:git push --force*", "bash:git push -f*", "bash:git push * --force*"]
```

or, with a hook that explains the house rule to the model:

```toml
[[hooks]]
event = "before_tool"
tools = ["bash"]
command = '''
if jq -r .arguments.command | grep -Eq 'git push.*(--force|-f)'; then
  echo "Force-pushing isn't allowed here: push to a new branch and open a pull request instead."
  exit 2
fi
'''
```

**Run the tests when a request is done:**

```toml
[[hooks]]
event = "after_request"
command = "pytest -q -x 2>&1 | tail -3"
timeout = 300
```

**A desktop notification** when lcode needs you or finishes a long request:

```toml
[[hooks]]
event = "notification"
command = "notify-send lcode \"$(jq -r .message)\""   # macOS: osascript -e "display notification \"…\""
```

**Tell the model what branch it's on** at the start of every session:

```toml
[[hooks]]
event = "session_start"
command = "git status --short --branch | head -20"
feedback = true
```

`lcode doctor` lists the hooks and rules it found, and any it couldn't read.
