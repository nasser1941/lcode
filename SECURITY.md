# Security policy

## Supported versions

lcode is young; security fixes go into the latest release on `main`.

| Version | Supported |
|---|---|
| 0.2.x (latest) | ✅ |
| older | ❌ |

## Reporting a vulnerability

**Please don't open a public issue for security problems.** Report them privately through
[GitHub's private vulnerability reporting](https://github.com/nasser1941/lcode/security/advisories/new).
Include what you found, how to reproduce it, and the impact you expect.

You can expect an acknowledgement within 3 days and a status update within 10 days. Once a fix is
released you'll be credited in the advisory unless you prefer otherwise.

## Security model

lcode runs a language model on your machine and lets it read files and run shell commands **as your
user, in your working directory**. It is not a sandbox. Keep in mind:

- In the default `ask` mode, every file edit and every shell command that isn't on the read-only
  allowlist (`ls`, `cat`, `grep`, `git status`, …) is shown to you and needs your approval.
  `auto-edit` skips approval for edits; `yolo` skips all approval — use it only with the sandbox or
  in disposable environments (a VM, a throwaway clone).
- The optional sandbox (`sandbox = "docker"` or `"podman"`, or `lcode --sandbox`) runs shell commands
  in a container that sees only the project folder, as your user, without network access unless
  allowed. The file tools are then limited to the project too. It doesn't cover web or MCP tools,
  the project folder itself stays writable, and a container shares the host kernel; see
  https://nasser1941.github.io/lcode/sandbox/ for its limits.
- Content the model reads (files, command output, search results and web pages) can contain prompt
  injections that try to make it run harmful commands. lcode marks web content as untrusted, but
  review commands before approving them.
- The model runs on the Ollama server you configure (default `http://localhost:11434`); if you point
  `ollama_host` at a remote server, your prompts and code go to that server. lcode sends no telemetry.
- With web access on (the default), search queries go to the configured search provider and fetched
  pages are downloaded from their websites. The model writes the queries, so they can contain names or
  snippets from your code. Use `web = ask` to approve each search, or `web = off` / `--no-web` to
  keep everything local.
- MCP servers you add run as your user (local servers) or act on your accounts (remote servers).
  Every MCP tool call needs your approval unless you're in `yolo` mode or listed the tool under
  `allow`. A project's `.mcp.json` is only used after you approve it, and again after it changes.
  MCP tool results can contain prompt injections, like web pages. Tokens you enter are stored in
  `~/.config/lcode/mcp.json` and sign-in tokens in `~/.local/state/lcode/mcp-auth/`, both readable
  only by you.
- Sessions (including file contents the model read) are stored in `~/.local/state/lcode/sessions`.
  Checkpoints for `/undo` keep copies of your project's files (except ignored ones) in
  `~/.local/state/lcode/checkpoints` for 14 days; turn them off with `checkpoints = false`.

Reports about bypassing the permission prompts, the read-only allowlist, or data leaving the
machine unexpectedly are especially welcome.
