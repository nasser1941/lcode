# Code intelligence

The model navigates code with `grep` by default. In a large codebase that's imprecise (the same
names appear everywhere), every extra file it reads costs context, and type errors only show up
when the build runs. With a **language server** installed, lcode does better:

- **An `lsp` tool.** The model asks where a symbol is defined, where it's used, what its type or
  signature is, or what a file contains, and gets exact `path:line` answers.
- **Errors after edits.** After the model changes a file, the language server checks it, and any
  errors the change introduced are added to the result of the edit, so the model fixes them
  right away:

```text
● edit_file(src/main.ts)
  ✓ Edited src/main.ts
  Edited src/main.ts (1 replacement(s)). Result: …
  [The language server found 1 new error after this change:
    main.ts:3: Argument of type 'string' is not assignable to parameter of type 'number'. (typescript)]
```

Only errors the edit introduced are listed, not ones that were there before, and warnings are left
out. Only the edited file is checked: errors a change causes elsewhere (in the callers of a
function whose signature changed, say) show up when the model runs the build or the tests.

## Setting it up

Install a language server for your language; lcode finds it on the `PATH`, or in the project's
`node_modules/.bin`:

| Language | Server | Install |
|---|---|---|
| Python | basedpyright, pyright or pylsp | `uv tool install basedpyright` (or `npm install -g pyright`) |
| TypeScript, JavaScript | typescript-language-server (TypeScript 5), or `tsc --lsp` (TypeScript 7) | `npm install -g typescript-language-server typescript` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| C, C++ | clangd | `apt install clangd`, or `brew install llvm` |

`lcode doctor` shows what it found. Servers start the first time they're needed in a session, one per
language, and stop when you quit. Without any server installed, nothing changes: there's no `lsp`
tool and nothing about it in the model's instructions.

TypeScript 7 no longer includes the `tsserver.js` that typescript-language-server needs, but its
own `tsc --lsp` is a language server; lcode tries one after the other.

## The tool

| Action | What the model gets |
|---|---|
| `definition` | Where the symbol is defined: `src/calc.py:12: def add(a, b):` |
| `references` | Every place it's used, one line each |
| `hover` | Its type or signature, and documentation |
| `symbols` with a path | The file's outline: classes, functions, methods, with line numbers |
| `symbols` with a query | Symbols with that name anywhere in the workspace |

The model gives a line number and the symbol's name on that line (models rarely know column
numbers). The tool is available to [subagents](agents.md) and in [plan mode](usage.md#plan-mode)
too.

## Settings

| Setting | |
|---|---|
| `lsp` | `auto` (default): use the language servers that are installed; `off`: never |

`~/.config/lcode/lsp.json` picks another command for a language, or turns one off:

```json
{
  "python": {"command": ["pylsp"]},
  "rust": {"disabled": true}
}
```

A language server's own output goes to `~/.local/state/lcode/lsp-logs/<language>.log`. Language
servers run on your machine even when the [sandbox](sandbox.md) is on: they only read the project.
