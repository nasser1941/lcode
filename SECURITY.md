# Security policy

## Supported versions

lcode is young; security fixes go into the latest release on `main`.

| Version | Supported |
|---|---|
| 0.1.x (latest) | ✅ |
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
  `auto-edit` skips approval for edits; `yolo` skips all approval — use it only in disposable
  environments (a container, a VM, a throwaway clone).
- Content the model reads (files, command output, web pages fetched by commands) can contain prompt
  injections that try to make it run harmful commands. Review commands before approving them.
- lcode talks only to the Ollama server you configure (default `http://localhost:11434`). It sends no
  telemetry. If you point `ollama_host` at a remote server, your prompts and code go to that server.
- Sessions (including file contents the model read) are stored in `~/.local/state/lcode/sessions`.

Reports about bypassing the permission prompts, the read-only allowlist, or data leaving the
machine unexpectedly are especially welcome.
