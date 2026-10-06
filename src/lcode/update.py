"""`lcode update`: update lcode, and Ollama when it runs in Docker.

lcode updates itself the way it was installed: uv tool, pipx, Homebrew, pip, or `git pull` for a
checkout (an editable install).

For Ollama in Docker, the newest version is looked up on Docker Hub, never taken from a local
`latest` tag (which can be months old), and pulled by its version tag (`ollama/ollama:0.35.1`), so
`docker ps` shows which version runs. Release candidates are skipped, and a ROCm image stays ROCm.
- A container from Docker Compose gets the new tag in its compose file and is recreated with
  `docker compose up`, so Compose keeps managing it.
- Any other container is recreated with the same name, ports, volumes, environment, GPUs, network
  and restart policy. The old one is kept until the new one answers with the new version, and is
  put back if it doesn't.
Ollama installed without Docker gets the command to update it, which lcode doesn't run itself.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

from lcode import __version__

IMAGE = "ollama/ollama"
HUB_TAGS = "https://hub.docker.com/v2/namespaces/ollama/repositories/ollama/tags"
PYPI = "https://pypi.org/pypi/lcode-cli/json"
SEMVER = re.compile(r"\d+\.\d+\.\d+")
START_TIMEOUT = 120  # seconds for a recreated container to answer
NATIVE = {
    "linux": "curl -fsSL https://ollama.com/install.sh | sh",
    "darwin": "brew upgrade ollama   (or let the Ollama app update itself)",
}


class UpdateError(Exception):
    pass


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def run(argv: list[str], cwd: Path | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    """A command whose output lcode reads (and tests replace)."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"{argv[0]} failed: {e}") from e


def stream(argv: list[str], cwd: Path | None = None) -> int:
    """A command the user watches (pulls, installs); returns its exit code."""
    try:
        return subprocess.call(argv, cwd=cwd)
    except OSError as e:
        raise UpdateError(f"{argv[0]} failed: {e}") from e


def get_json(url: str, **params) -> dict:
    try:
        r = requests.get(url, params=params or None, timeout=20)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise UpdateError(f"couldn't read {url.split('?')[0]}: {e}") from e


# ----------------------------------------------------------------------------- the newest versions


def latest_lcode() -> str:
    return str(get_json(PYPI)["info"]["version"])


def latest_ollama(flavor: str = "") -> str:
    """The newest released Ollama image tag on Docker Hub: "0.35.1", or "0.35.1-rocm" for flavor "rocm"."""
    names: list[str] = []
    url, params = HUB_TAGS, {"page_size": 100, "ordering": "last_updated"}
    for _ in range(3):  # the newest tags come first; three pages reach well past them
        page = get_json(url, **params)
        names += [str(t.get("name", "")) for t in page.get("results") or []]
        url, params = page.get("next"), {}
        if not url:
            break
    suffix = f"-{flavor}" if flavor else ""
    versions = [
        n.removesuffix(suffix) for n in names if n.endswith(suffix) and SEMVER.fullmatch(n.removesuffix(suffix))
    ]
    if not versions:
        raise UpdateError(f"Docker Hub lists no released {IMAGE} versions{f' for {flavor}' if flavor else ''}")
    return max(versions, key=version_key) + suffix


def image_flavor(image: str) -> str:
    """The image variant to stay on: "rocm" for ollama/ollama:rocm or :0.35.1-rocm, else ""."""
    tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else "latest"
    return "rocm" if tag == "rocm" or tag.endswith("-rocm") else ""


# ----------------------------------------------------------------------------- lcode itself


@dataclass
class Install:
    kind: str  # checkout, uv, pipx, brew, uvx, pip
    command: list[str] | None = None  # what updates it
    path: Path | None = None  # the checkout, for kind checkout

    def describe(self) -> str:
        return {
            "checkout": f"a git checkout at {self.path}",
            "uv": "uv tool",
            "pipx": "pipx",
            "brew": "Homebrew",
            "uvx": "uvx",
            "pip": "pip",
        }[self.kind]


def lcode_install(prefix: Path | None = None, direct_url: str | None = None) -> Install:
    """How this lcode was installed."""
    prefix = (prefix or Path(sys.prefix)).resolve()
    if direct_url is None:
        try:
            direct_url = importlib.metadata.distribution("lcode-cli").read_text("direct_url.json") or ""
        except importlib.metadata.PackageNotFoundError:
            direct_url = ""
    try:
        origin = json.loads(direct_url) if direct_url else {}
    except ValueError:
        origin = {}
    if (origin.get("dir_info") or {}).get("editable") and str(origin.get("url", "")).startswith("file://"):
        return Install("checkout", path=Path(urlsplit(origin["url"]).path))
    text = str(prefix)
    if (prefix / "uv-receipt.toml").exists():
        return Install("uv", ["uv", "tool", "upgrade", "lcode-cli"])
    if "/Cellar/lcode/" in text:
        return Install("brew", ["brew", "upgrade", "nasser1941/tap/lcode"])
    if "pipx" in prefix.parts:
        return Install("pipx", ["pipx", "upgrade", "lcode-cli"])
    if "/uv/" in text and ("archive-v" in text or "/cache/" in text or "/.cache/" in text):
        return Install("uvx")
    return Install("pip", [sys.executable, "-m", "pip", "install", "--upgrade", "lcode-cli"])


@dataclass
class CheckoutState:
    branch: str
    dirty: bool
    behind: int
    upstream: str


def checkout_state(path: Path) -> CheckoutState:
    def git(*args: str) -> str:
        r = run(["git", "-C", str(path), *args], timeout=120)
        if r.returncode != 0:
            raise UpdateError(f"git {args[0]} failed in {path}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout.strip()

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    upstream = run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "@{upstream}"]).stdout.strip()
    behind = 0
    if upstream:
        git("fetch", "-q")
        behind = int(git("rev-list", "--count", "HEAD..@{upstream}") or 0)
    return CheckoutState(branch, dirty, behind, upstream)


def update_checkout(path: Path, install_receipt: bool) -> str:
    """git pull a checkout lcode runs from; reinstall its environment when its dependencies changed."""
    before = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    if stream(["git", "-C", str(path), "pull", "--ff-only"]) != 0:
        raise UpdateError(f"git pull failed in {path}")
    changed = run(["git", "-C", str(path), "diff", "--name-only", before, "HEAD"]).stdout.split()
    reinstall = ["uv", "tool", "install", "--force", "--editable", str(path)]
    if "pyproject.toml" in changed and install_receipt and stream(reinstall) != 0:
        raise UpdateError("reinstalling lcode's environment failed: run  uv tool install --force --editable .")
    count = run(["git", "-C", str(path), "rev-list", "--count", f"{before}..HEAD"]).stdout.strip() or "0"
    return f"{count} new commit(s)"


# ----------------------------------------------------------------------------- Ollama in Docker


@dataclass
class Container:
    id: str
    name: str
    image: str  # as configured, e.g. ollama/ollama:latest
    info: dict = field(repr=False)

    @property
    def compose(self) -> dict | None:
        labels = (self.info.get("Config") or {}).get("Labels") or {}
        project, service = labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")
        if not project or not service:
            return None
        files = [f for f in str(labels.get("com.docker.compose.project.config_files", "")).split(",") if f]
        return {"project": project, "service": service, "files": files,
                "workdir": labels.get("com.docker.compose.project.working_dir", "")}  # fmt: skip


def docker_available() -> bool:
    return shutil.which("docker") is not None and run(["docker", "info", "--format", "{{.ID}}"]).returncode == 0


def find_container(host: str) -> Container | None:
    """The Ollama container that serves `host` (a local address), if Ollama runs in Docker."""
    parts = urlsplit(host)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1", "0.0.0.0", None):
        return None
    port = str(parts.port or 11434)
    ids = run(["docker", "ps", "-q"]).stdout.split()
    if not ids:
        return None
    infos = json.loads(run(["docker", "inspect", *ids]).stdout or "[]")
    found = []
    for info in infos:
        image = (info.get("Config") or {}).get("Image", "")
        if not image.startswith(IMAGE):
            continue
        bindings = ((info.get("HostConfig") or {}).get("PortBindings") or {}).get("11434/tcp") or []
        if any(str(b.get("HostPort")) == port for b in bindings):
            found.append(Container(info["Id"][:12], info["Name"].lstrip("/"), image, info))
    return found[0] if len(found) == 1 else None


def set_compose_image(text: str, service: str, image: str) -> tuple[str, str]:
    """The compose file with the service's image changed, and the image it had."""
    lines = text.splitlines(keepends=True)
    services = next((i for i, line in enumerate(lines) if re.match(r"services:\s*(#.*)?$", line)), None)
    if services is None:
        raise UpdateError("no services: section")
    start = indent = None
    for i in range(services + 1, len(lines)):
        m = re.match(rf"(\s+)['\"]?{re.escape(service)}['\"]?:\s*(#.*)?$", lines[i])
        if m:
            start, indent = i, len(m.group(1))
            break
        if lines[i].strip() and not lines[i][0].isspace() and not lines[i].lstrip().startswith("#"):
            break  # the next top-level key: services ended
    if start is None or indent is None:
        raise UpdateError(f"no service {service} in it")
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
            break  # the next service
        m = re.match(r"(\s+image:\s*)(['\"]?)([^'\"#\s]+)\2(\s*(#.*)?)$", line.rstrip("\n"))
        if m:
            if "${" in line:
                raise UpdateError(f"the image comes from a variable ({m.group(3)}): set it to {image}")
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"{m.group(1)}{m.group(2)}{image}{m.group(2)}{m.group(4)}{newline}"
            return "".join(lines), m.group(3)
    raise UpdateError(f"service {service} has no image: line (it may be built from a Dockerfile)")


def run_args(info: dict, image_info: dict, image: str) -> list[str]:
    """`docker run` arguments that recreate a container with another image and the same settings."""
    config, host = info.get("Config") or {}, info.get("HostConfig") or {}
    base = image_info.get("Config") or {}
    args = ["docker", "run", "-d", "--name", info["Name"].lstrip("/")]
    restart = host.get("RestartPolicy") or {}
    if restart.get("Name") not in (None, "", "no"):
        retries = restart.get("MaximumRetryCount") or 0
        args += ["--restart", restart["Name"] + (f":{retries}" if restart["Name"] == "on-failure" and retries else "")]
    for port, bindings in (host.get("PortBindings") or {}).items():
        target = port.removesuffix("/tcp")
        for b in bindings or []:
            ip = b.get("HostIp") or ""
            args += ["-p", f"{ip + ':' if ip else ''}{b.get('HostPort', '')}:{target}"]
    for m in info.get("Mounts") or []:
        source = m.get("Name") if m.get("Type") == "volume" else m.get("Source")
        if source and m.get("Type") in ("volume", "bind"):
            args += ["-v", f"{source}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro")]
    default_env = set(base.get("Env") or [])
    for entry in config.get("Env") or []:
        if entry not in default_env:  # only what was set for this container; the new image brings its own PATH
            args += ["-e", entry]
    for request in host.get("DeviceRequests") or []:
        if ["gpu"] in (request.get("Capabilities") or []):
            ids = request.get("DeviceIDs") or []
            count = request.get("Count") or 0
            args += ["--gpus", f"device={','.join(ids)}" if ids else ("all" if count == -1 else str(count))]
    for device in host.get("Devices") or []:
        args += ["--device", f"{device['PathOnHost']}:{device['PathInContainer']}"]
    if host.get("Runtime") not in (None, "", "runc"):
        args += ["--runtime", host["Runtime"]]
    network = host.get("NetworkMode") or "default"
    if network not in ("default", "bridge") and not network.startswith("container:"):
        args += ["--network", network]
        endpoint = ((info.get("NetworkSettings") or {}).get("Networks") or {}).get(network) or {}
        name, short_id = info["Name"].lstrip("/"), info["Id"][:12]
        for alias in endpoint.get("Aliases") or []:
            if alias not in (name, short_id):
                args += ["--network-alias", alias]
    for host_entry in host.get("ExtraHosts") or []:
        args += ["--add-host", host_entry]
    base_labels = base.get("Labels") or {}
    for key, value in (config.get("Labels") or {}).items():
        if base_labels.get(key) != value:
            args += ["--label", f"{key}={value}"]
    entrypoint, cmd = config.get("Entrypoint") or [], config.get("Cmd") or []
    if entrypoint and entrypoint != (base.get("Entrypoint") or []):
        args += ["--entrypoint", entrypoint[0]]
        cmd = [*entrypoint[1:], *cmd]
    elif cmd == (base.get("Cmd") or []):
        cmd = []
    return [*args, image, *cmd]


def wait_for_version(host: str, wanted: str, timeout: float = START_TIMEOUT) -> bool:
    from lcode.ollama import Ollama, OllamaError

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if Ollama(host).version() == wanted.removesuffix("-rocm"):
                return True
        except OllamaError:
            pass
        time.sleep(2)
    return False


def recreate(container: Container, image: str, host: str, say: Callable[[str], None]) -> None:
    """Replace a plain container with one on `image`; put the old one back if that fails."""
    image_info = json.loads(run(["docker", "image", "inspect", container.info["Image"]]).stdout or "[{}]")[0]
    args = run_args(container.info, image_info, image)
    backup = f"{container.name}-before-lcode-update"
    for step in (["docker", "stop", container.name], ["docker", "rename", container.name, backup]):
        r = run(step)
        if r.returncode != 0:
            raise UpdateError(f"{' '.join(step[:2])} failed: {(r.stderr or r.stdout).strip()}")
    say(f"$ {' '.join(args)}")
    started = run(args)
    if started.returncode == 0 and wait_for_version(host, image.rsplit(":", 1)[1]):
        run(["docker", "rm", backup])
        return
    problem = (started.stderr or started.stdout).strip() or "it didn't answer with the new version"
    run(["docker", "rm", "-f", container.name])
    run(["docker", "rename", backup, container.name])
    run(["docker", "start", container.name])
    raise UpdateError(f"the new container didn't start ({problem[:300]}); the old one is running again")


def recreate_with_compose(container: Container, image: str, host: str, say: Callable[[str], None]) -> None:
    command, project_dir = compose_up(container.compose or {})
    say(f"$ {' '.join(command)}")
    if stream(command, cwd=project_dir) != 0:
        raise UpdateError("docker compose up failed")
    if not wait_for_version(host, image.rsplit(":", 1)[1]):
        raise UpdateError(f"Ollama didn't answer with version {image.rsplit(':', 1)[1]} after the restart")


def compose_up(compose: dict) -> tuple[list[str], Path]:
    """`docker compose up` for just the Ollama service of its project, and where to run it."""
    files = [Path(f) for f in compose["files"]]
    project_dir = Path(compose["workdir"]) if compose.get("workdir") else files[0].parent
    command = ["docker", "compose", "-p", compose["project"], "--project-directory", str(project_dir)]
    for f in files:
        command += ["-f", str(f)]
    return [*command, "up", "-d", "--no-deps", compose["service"]], project_dir


def compose_change(container: Container, image: str) -> tuple[Path, str, str]:
    """Which compose file defines the service's image, and its new text."""
    compose = container.compose or {}
    problems = []
    for path in reversed([Path(f) for f in compose.get("files") or []]):  # later files override earlier ones
        try:
            text, old = set_compose_image(path.read_text(), compose["service"], image)
            return path, text, old
        except (OSError, UpdateError) as e:
            problems.append(f"{path}: {e}")
    raise UpdateError("couldn't find the image to change: " + "; ".join(problems))


# ----------------------------------------------------------------------------- lcode update


def command(console, what: str = "", check: bool = False, yes: bool = False) -> int:
    """Update lcode and Ollama (or `what`: "lcode" or "ollama"). Returns the exit code."""
    from rich.prompt import Confirm

    def ask(question: str) -> bool:
        if yes:
            return True
        if not sys.stdin.isatty():
            console.print("[dim]Not asking without a terminal: run lcode update -y to go ahead.[/]")
            return False
        return Confirm.ask(question, default=True, console=console)

    failed = False
    for part, step in (("lcode", update_lcode), ("ollama", update_ollama)):
        if what and what != part:
            continue
        try:
            step(console, check, ask)
        except UpdateError as e:
            console.print(f"[red]✗ {escape(str(e))}[/]")
            failed = True
        console.print()
    return 1 if failed else 0


def escape(text: str) -> str:
    from rich.markup import escape as rich_escape

    return rich_escape(text)


def update_lcode(console, check: bool, ask: Callable[[str], bool]) -> None:
    install = lcode_install()
    if install.kind == "checkout" and install.path is not None:
        state = checkout_state(install.path)
        where = f"lcode {__version__} · {install.describe()}, branch {state.branch}"
        if not state.upstream:
            console.print(f"{where}: no upstream branch to update from.")
        elif not state.behind:
            console.print(f"[green]✓[/] {where}: up to date with {state.upstream}.")
        elif check:
            console.print(f"{where}: {state.behind} new commit(s) on {state.upstream}.")
        elif state.dirty or state.branch not in ("main", "master"):
            why = "has uncommitted changes" if state.dirty else f"is on branch {state.branch}"
            console.print(
                f"{where}: {state.behind} new commit(s) on {state.upstream}, but the checkout {why}. Update it yourself with git pull."
            )
        elif ask(f"Pull {state.behind} new commit(s) into {install.path}?"):
            receipt = (Path(sys.prefix) / "uv-receipt.toml").exists()
            console.print(f"[green]✓[/] lcode updated: {update_checkout(install.path, receipt)}.")
        return
    latest = latest_lcode()
    if version_key(latest) <= version_key(__version__):
        console.print(f"[green]✓[/] lcode {__version__} is the newest version ({install.describe()}).")
        return
    if install.command is None:
        console.print(
            f"lcode {__version__} → {latest} is out. You run lcode with uvx: use  uvx --refresh --from lcode-cli lcode"
        )
        return
    shown = " ".join(install.command)
    if check:
        console.print(f"lcode {__version__} → {latest} is out ({install.describe()}): {shown}")
        return
    if not ask(f"Update lcode {__version__} → {latest} with `{shown}`?"):
        return
    if stream(install.command) != 0:
        raise UpdateError(f"{shown} failed")
    console.print("[green]✓[/] lcode updated; the new version runs from the next `lcode`.")
    if install.kind == "brew":
        console.print(
            "[dim]Homebrew's formula follows a release by up to a day: if it's still the old version, try again later.[/]"
        )


def update_ollama(console, check: bool, ask: Callable[[str], bool]) -> None:
    from lcode import config
    from lcode.hardware import detect
    from lcode.ollama import Ollama, OllamaError

    cfg = config.load()
    if cfg["backend"] != "ollama":
        console.print(f"lcode uses another model server (backend = {cfg['backend']}): update it there.")
        return
    host = cfg["ollama_host"]
    try:
        current = Ollama(host).version()
    except OllamaError:
        current = ""
    container = find_container(host) if docker_available() else None
    if container is None:
        if not current:
            raise UpdateError(f"Ollama doesn't answer at {host}: start it, then run lcode update again")
        latest = latest_ollama()
        local = urlsplit(host).hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", None)
        if version_key(latest) <= version_key(current):
            console.print(f"[green]✓[/] Ollama {current} is the newest version.")
        elif not local:
            console.print(f"Ollama {current} → {latest} is out. It runs on {host}: update it on that machine.")
        else:
            hint = NATIVE.get(detect().os, NATIVE["linux"])
            console.print(
                f"Ollama {current} → {latest} is out. Ollama isn't in a Docker container here, so update it with:\n  {hint}"
            )
        return
    latest = latest_ollama(image_flavor(container.image))
    image = f"{IMAGE}:{latest}"
    compose = container.compose
    where = f"container {container.name}, image {container.image}" + (
        f", Docker Compose project {compose['project']}" if compose else ""
    )
    console.print(f"Ollama {current or '?'} · {where}. Newest on Docker Hub: {latest}.")
    if container.image == image and current == latest.removesuffix("-rocm"):
        console.print(f"[green]✓[/] Ollama is up to date, on {image}.")
        return
    if current == latest.removesuffix("-rocm"):
        console.print(
            f"[dim]It already runs {current}, but as {container.image}: pinning it to {image} shows the version in docker ps.[/]"
        )
    change = compose_change(container, image) if compose else None
    plan = [f"docker pull {image}"]
    if change:
        path, _, old = change
        plan.append(f"in {path}, service {compose['service']}: image {old} → {image}")  # type: ignore[index]
        plan.append(f"docker compose up -d --no-deps {compose['service']}")  # type: ignore[index]
    else:
        plan.append(
            f"recreate container {container.name} on {image}, with the same name, ports, volumes, environment, GPUs, network and restart policy (the old one is kept until the new one answers)"
        )
    console.print("\n".join(f"  {i}. {escape(step)}" for i, step in enumerate(plan, 1)))
    if check:
        return
    console.print("[dim]Ollama restarts: requests running now stop, and models load again on their next request.[/]")
    if not ask("Update Ollama?"):
        return
    if stream(["docker", "pull", image]) != 0:
        raise UpdateError(f"docker pull {image} failed")

    def say(text: str) -> None:
        console.print(f"[dim]{escape(text)}[/]")

    if change:
        path, text, _ = change
        original = path.read_text()
        path.write_text(text)
        try:
            recreate_with_compose(container, image, host, say)
        except UpdateError:
            path.write_text(original)
            say(f"put {path} back the way it was")
            command, workdir = compose_up(compose)  # type: ignore[arg-type]
            stream(command, cwd=workdir)
            raise
    else:
        recreate(container, image, host, say)
    console.print(f"[green]✓[/] Ollama {current or '?'} → {latest.removesuffix('-rocm')}, running as {image}.")
    old_image = container.info.get("Image", "")
    if old_image and not run(["docker", "ps", "-aq", "--filter", f"ancestor={old_image}"]).stdout.strip():
        size = run(["docker", "image", "inspect", old_image, "--format", "{{.Size}}"]).stdout.strip()
        gb = f" ({int(size) / 1e9:.1f} GB)" if size.isdigit() else ""
        console.print(
            f"[dim]The previous image is still on disk{gb}. To remove it: docker image rm {old_image[7:19]}[/]"
        )
