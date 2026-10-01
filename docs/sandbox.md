# Sandbox

By default the model's shell commands run directly on your machine, as you. The permission prompts
let you check each command, but they're a guardrail, not isolation, and `yolo` mode removes even
that. The sandbox runs the commands in a container that can only see your project:

```bash
lcode --sandbox                       # this session
lcode config set sandbox docker       # every session (or podman)
```

The session's banner then shows `sandbox  docker · lcode's image · no network`.

## What the container can and can't do

| | |
|---|---|
| **Sees** | Your project: the git repository you started lcode in (or the folder itself, outside a repository), mounted at the same path, read-write |
| **Doesn't see** | Anything else on your machine: your home folder, SSH keys, cloud credentials, other projects, the Docker socket |
| **Network** | None, unless you allow it (`/sandbox network on`, or `sandbox_network = true`) |
| **Runs as** | Your user ID, so files it creates belong to you; no Linux capabilities, no privilege escalation, at most 1,024 processes |
| **Lives** | For the session: started when lcode starts, removed when it exits. Containers left behind by a crash are removed the next time |

lcode's file tools (reading, editing, searching) run on your machine, but with the sandbox on they're
limited to the same project folder, so the model's whole world is the project.

## Approvals

With the sandbox on, `auto-edit` mode runs shell commands without asking too, since they can only
change the project, and [`/undo`](usage.md#undo-and-checkpoints) can take those changes back. `ask`
mode still asks before every command that isn't read-only. `yolo` asks for nothing, with or without
the sandbox.

## The image

lcode builds its default image the first time you use the sandbox (a minute or two): Python 3.12,
Node.js, git, build tools, ripgrep, curl and jq. Use your own image to match your project's toolchain:

```bash
lcode config set sandbox_image node:22
```

Any image with `bash` and `setsid` works. Installing packages (`pip install`, `npm install`) needs
the network: allow it with `/sandbox network on` for the session, or `sandbox_network = true`. The
model is told when a command failed because there's no network.

## Settings

| Setting | Default | |
|---|---|---|
| `sandbox` | `off` | `docker` or `podman` to run commands in a container |
| `sandbox_image` | lcode's image | Any image with bash and setsid |
| `sandbox_network` | `false` | Let commands use the network |

In a session, `/sandbox` shows the sandbox and `/sandbox network on|off` changes network access (the
container restarts; files in the project stay). `lcode doctor` checks that Docker or Podman works.

## Requirements

- **Linux:** [Docker Engine](https://docs.docker.com/engine/install/) (your user in the `docker`
  group) or [Podman](https://podman.io/docs/installation) (rootless; lcode passes `--userns keep-id`.
  Podman is less tested than Docker, so reports are welcome).
- **macOS:** Docker Desktop, [OrbStack](https://orbstack.dev) or [colima](https://github.com/abiosoft/colima).
  Your project must be in a folder they share with containers (your home folder is, by default).

If the sandbox is on but can't start, lcode stops instead of running commands without it, and says
how to fix it.

## What the sandbox doesn't cover

It isolates **shell commands**. Know its limits:

- The project folder is writable: a command can still delete or damage your project's files. Use git,
  and `/undo` for changes since the request started.
- With the network on, commands can reach the internet, including to send out anything in the project.
- [Web search and page fetching](usage.md#web-search) and [MCP tools](mcp.md) run on your machine,
  not in the container. They still need their own approval (MCP) or can be turned off (`--no-web`).
- A container is not a virtual machine: it shares your machine's kernel, so a kernel vulnerability
  could let a process escape. For untrusted code you can't afford to run, use a separate VM.
