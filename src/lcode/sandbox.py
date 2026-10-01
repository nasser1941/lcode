"""An optional sandbox: the model's shell commands run in a container that only sees the project.

One container per session. The project (the git repository around the working directory, or the
working directory itself) is mounted at the same path, so paths in command output stay valid and
lcode's file tools, which run on the host, see the same files. Commands run as the host user, with
no network unless it's allowed, no Linux capabilities, a process limit, and nothing else from the
host: not the home folder, not the Docker socket.

The default image is built locally on first use from the Dockerfile below. Any image with bash and
setsid (util-linux or busybox) can be used instead (`sandbox_image`).
"""

from __future__ import annotations

import hashlib
import os
import platform
import secrets
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

DOCKERFILE = """\
FROM python:3.12-slim-bookworm
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      git curl ca-certificates build-essential ripgrep jq unzip less procps nodejs npm \\
 && rm -rf /var/lib/apt/lists/*
"""
DEFAULT_IMAGE = f"lcode-sandbox:{hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:12]}"
LABEL = "dev.lcode.sandbox"
HOME = "/tmp/lcode-home"
ENGINES = ("docker", "podman")


class SandboxError(Exception):
    """The sandbox can't be used; the message says how to fix it."""


def engine_problem(engine: str) -> str | None:
    """Why `engine` can't run containers here, or None if it can."""
    if not shutil.which(engine):
        if engine == "podman":
            return "podman isn't installed (https://podman.io/docs/installation)"
        if platform.system() == "Darwin":
            return "docker isn't installed: install Docker Desktop, OrbStack or colima"
        return "docker isn't installed (https://docs.docker.com/engine/install/)"
    try:
        r = subprocess.run(
            [engine, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"{engine} doesn't respond: {e}"
    if r.returncode != 0:
        detail = (r.stderr.strip().splitlines() or [""])[-1]
        if "permission denied" in detail.lower():
            return (
                f"{engine} needs permission: add yourself to the docker group "
                "(sudo usermod -aG docker $USER, then log in again)"
            )
        if platform.system() == "Darwin":
            return f"{engine} isn't running: start Docker Desktop, OrbStack or colima ({detail})"
        return f"{engine} isn't running: start it (sudo systemctl start {engine}) ({detail})"
    return None


def remove_orphans(engine: str) -> None:
    """Remove sandboxes left behind by lcode sessions that ended without cleaning up."""
    try:
        r = subprocess.run(
            [engine, "ps", "-a", "--filter", f"label={LABEL}", "--format", '{{.ID}} {{.Label "dev.lcode.pid"}}'],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return
    stale = []
    for line in r.stdout.split("\n"):
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and not _alive(int(parts[1])):
            stale.append(parts[0])
    if stale:
        subprocess.run([engine, "rm", "-f", *stale], capture_output=True, timeout=60)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Sandbox:
    def __init__(self, engine: str, image: str | None = None, network: bool = False, console: Console | None = None):
        if engine not in ENGINES:
            raise SandboxError(f"unknown sandbox {engine!r}; use docker or podman")
        self.engine = engine
        self.image = image or DEFAULT_IMAGE
        self.network = network
        self.console = console or Console()
        self.container: str | None = None
        self.root: Path | None = None

    # -- lifecycle
    def check(self) -> None:
        problem = engine_problem(self.engine)
        if problem:
            raise SandboxError(problem)

    def contains(self, path: Path) -> bool:
        return self.root is not None and (path == self.root or self.root in path.parents)

    def ensure(self, cwd: Path) -> None:
        """Have a container running whose mounted folder includes `cwd`."""
        if self.container and self.contains(cwd):
            return
        self.stop()
        self.check()
        remove_orphans(self.engine)
        if self.image == DEFAULT_IMAGE:
            self._build_default_image()
        root = project_root(cwd)
        name = f"lcode-sandbox-{os.getpid()}-{secrets.token_hex(3)}"
        command = [
            self.engine, "run", "--detach", "--init", "--name", name,
            "--label", LABEL, "--label", f"dev.lcode.pid={os.getpid()}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "1024",
            "--volume", f"{root}:{root}", "--workdir", str(cwd),
            "--env", f"HOME={HOME}", "--tmpfs", f"{HOME}:exec,mode=1777",
        ]  # fmt: skip
        if not self.network:
            command += ["--network", "none"]
        if self.engine == "podman":
            command += ["--userns", "keep-id"]  # rootless podman: files belong to the host user
        else:
            command += ["--user", f"{os.getuid()}:{os.getgid()}"]
        command += [self.image, "sleep", "infinity"]
        r = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise SandboxError(f"couldn't start the sandbox: {(r.stderr.strip().splitlines() or ['?'])[-1]}")
        self.container, self.root = name, root

    def _build_default_image(self) -> None:
        exists = subprocess.run([self.engine, "image", "inspect", self.image], capture_output=True, timeout=30)
        if exists.returncode == 0:
            return
        with self.console.status("Building the sandbox image (once; a minute or two)…"):
            r = subprocess.run(
                [self.engine, "build", "--tag", self.image, "-"],
                input=DOCKERFILE,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        if r.returncode != 0:
            tail = "\n".join(r.stderr.strip().splitlines()[-5:])
            raise SandboxError(f"couldn't build the sandbox image:\n{tail}")

    def exec_argv(self, cwd: Path, script: str) -> tuple[list[str], str]:
        """The command that runs `script` with bash inside the container, and a token for kill()."""
        assert self.container is not None
        token = secrets.token_hex(6)
        # Its own session, so a timeout or Ctrl+C can stop everything the command started.
        script = f"echo $$ > /tmp/.lcode-{token}.pgid\n{script}"
        argv = [self.engine, "exec", "--interactive", "--workdir", str(cwd), self.container]
        return [*argv, "setsid", "--wait", "bash", "-c", script], token

    def kill(self, token: str) -> None:
        """Stop a command and everything it started (after a timeout or Ctrl+C); the container stays."""
        if self.container:
            pgid = f"$(cat /tmp/.lcode-{token}.pgid 2>/dev/null)"
            subprocess.run(
                # bash, not sh: dash's kill doesn't accept "--" before a negative (process group) id
                [self.engine, "exec", self.container, "bash", "-c", f'[ -n "{pgid}" ] && kill -KILL -- -{pgid}'],
                capture_output=True,
                timeout=30,
            )

    def stop(self) -> None:
        if self.container:
            subprocess.run([self.engine, "rm", "--force", self.container], capture_output=True, timeout=60)
        self.container, self.root = None, None

    def describe(self) -> str:
        image = "lcode's image" if self.image == DEFAULT_IMAGE else self.image
        network = "network on" if self.network else "no network"
        return f"{self.engine} · {image} · {network}"


def project_root(cwd: Path) -> Path:
    """The folder the sandbox mounts: the git repository around `cwd`, or `cwd` itself."""
    from lcode.checkpoints import work_tree_for

    root = work_tree_for(cwd)
    home = Path.home().resolve()
    if root == home or root in home.parents or root == Path("/"):
        raise SandboxError(f"won't mount {root} into the sandbox; start lcode in a project folder")
    return root
