# Editors

lcode also works inside editors that support the
[Agent Client Protocol](https://agentclientprotocol.com) (ACP), such as Zed, JetBrains IDEs,
Neovim and Emacs. The editor starts `lcode acp` and shows lcode's work in its own UI: answers,
tool calls, a diff for each edit, and permission questions.

## Zed

Add lcode to Zed's `settings.json`:

```json
{
  "agent_servers": {
    "lcode": {
      "type": "custom",
      "command": "lcode",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

Then open the Agent Panel and start a new thread with **lcode** from the new-thread menu.

- **Zed can't start lcode:** use the full path in `command`, from `which lcode` (usually
  `~/.local/bin/lcode`). An editor started from the desktop may not see your shell's `PATH`.
- **Something goes wrong:** run **dev: open acp logs** in Zed's command palette to see the
  messages lcode and Zed exchange.

## JetBrains IDEs

Add lcode to `~/.jetbrains/acp.json`, with the full path from `which lcode`:

```json
{
  "agent_servers": {
    "lcode": {
      "command": "/home/you/.local/bin/lcode",
      "args": ["acp"]
    }
  }
}
```

Then pick lcode in the AI Chat tool window.

## Other editors

Any [ACP client](https://agentclientprotocol.com/overview/clients) works the same way: tell it
to run `lcode acp`. That includes Neovim (CodeCompanion, avante.nvim), Emacs (agent-shell),
VS Code extensions and more.

## What the editor shows

| In the editor | From lcode |
|---|---|
| The conversation | lcode's answers and its thinking, as they stream |
| Tool calls | Each tool call, with its kind (read, search, edit, run, fetch) and the files it touches |
| Edits | A diff of the whole file for each edit |
| Open files | Edits go through the editor, so open files update right away. The model reads what's in the editor, unsaved changes included |
| Permission questions | **Allow**, **Always allow** (for this session) or **Reject**, in the editor's own dialog |
| Modes | lcode's [permission modes](usage.md#permissions): Ask, Auto-edit, Plan and Yolo |
| Plans | In Plan mode, the plan is approved in the editor, which then switches to Ask or Auto-edit |
| The plan list | The model's todo list |
| Context | How much of the context window is in use |
| Commands | `/review` and [your own commands](commands.md) |
| Past threads | lcode's saved sessions: an editor that reopens a thread gets its conversation back |

[Checkpoints](usage.md#undo-and-checkpoints), [hooks and permission rules](hooks.md),
[memory](memory.md), [subagents](agents.md) and [MCP servers](mcp.md) work as in the terminal.
MCP servers the editor passes to its agents are added to lcode's own.

## Settings

lcode uses your [configuration](configuration.md): the model, the context window, the
[model server](servers.md) and the default permission mode. To use different settings in an
editor, set them in the agent's `env`, for example `"env": {"LCODE_MODEL": "qwen3.5-9b"}`.

A repository's own commands, skills, agents and hooks are used only after you
[approve them](commands.md#approving-a-repositorys-commands-skills-and-agents). An editor can't
ask, so run `lcode` in the repository once and approve them there.

To see lcode's own output while it runs in an editor, set `LCODE_ACP_LOG` to a file in `env`.
