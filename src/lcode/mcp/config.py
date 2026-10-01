"""MCP server settings: ~/.config/lcode/mcp.json and a project's .mcp.json.

Both use the `mcpServers` format that most MCP servers document, so a snippet from a server's README
can be pasted in as is. lcode adds a few optional keys per server: `disabled`, `tools` (only offer
these tools to the model), `allow` (tools that don't need approval), `timeout`, `oauth` and
`free_gpu` (tools that need the GPU to themselves, so lcode unloads its model first).
Values can refer to environment variables as ${NAME} or ${NAME:-default}.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from lcode import config

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
DEFAULT_TIMEOUT = 300  # seconds for one tool call


def user_path() -> Path:
    return config.CONFIG_DIR / "mcp.json"


def approvals_path() -> Path:
    return config.STATE_DIR / "mcp-approved.json"


class MissingVariable(ValueError):
    pass


def expand(value, env: dict[str, str] | None = None):
    """Replace ${NAME} and ${NAME:-default} in strings, lists and dicts."""
    env = os.environ if env is None else env
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if env.get(name):
                return env[name]
            if default is not None:
                return default
            raise MissingVariable(name)

        return VARIABLE.sub(sub, value)
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


@dataclass
class ServerConfig:
    name: str
    raw: dict
    source: str = "user"  # "user" or the project file's path
    transport: str = "stdio"
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    tools: list[str] | None = None
    allow: list[str] = field(default_factory=list)
    timeout: float = DEFAULT_TIMEOUT
    oauth: dict = field(default_factory=dict)
    disabled: bool = False
    free_gpu: list[str] | bool = False  # tools (or all, if True) that need lcode's model off the GPU
    error: str = ""  # a problem with the settings; the server can't start

    @property
    def target(self) -> str:
        """What the server is, for display (unexpanded, so tokens in variables stay hidden)."""
        if self.transport == "http":
            return str(self.raw.get("url", ""))
        words = [str(self.raw.get("command", "")), *[str(a) for a in self.raw.get("args") or []]]
        text = " ".join(words)
        return text if len(text) <= 80 else text[:79] + "…"

    @property
    def key(self) -> str:
        """Identifies the server's launch settings (for remembering its protocol era)."""
        return hashlib.sha256(json.dumps([self.command, self.url], sort_keys=True).encode()).hexdigest()[:16]


def parse(name: str, raw: dict, source: str = "user") -> ServerConfig:
    cfg = ServerConfig(name=name, raw=raw, source=source)
    if not isinstance(raw, dict):
        cfg.error = "the settings must be a JSON object"
        return cfg
    cfg.disabled = bool(raw.get("disabled", False))
    kind = str(raw.get("type") or ("http" if raw.get("url") else "stdio")).lower()
    try:
        if kind in ("http", "streamable-http", "streamablehttp"):
            cfg.transport = "http"
            cfg.url = expand(str(raw.get("url", "")))
            cfg.headers = {str(k): str(v) for k, v in expand(raw.get("headers") or {}).items()}
            if not cfg.url.startswith(("http://", "https://")):
                cfg.error = "`url` must start with http:// or https://"
        elif kind == "stdio":
            command = raw.get("command")
            if not command or not isinstance(command, str):
                cfg.error = "`command` is missing"
            else:
                args = raw.get("args") or []
                cfg.command = [expand(command), *[str(a) for a in expand(list(args))]]
                cfg.env = {str(k): str(v) for k, v in expand(raw.get("env") or {}).items()}
        elif kind == "sse":
            cfg.transport = "http"
            cfg.error = (
                "the old HTTP+SSE transport isn't supported. Most servers also have a Streamable HTTP endpoint "
                "(often ending in /mcp instead of /sse); otherwise bridge it with: npx -y mcp-remote <url>"
            )
        else:
            cfg.error = f"unknown type {kind!r} (use stdio or http)"
        cfg.oauth = expand(raw.get("oauth") or {})
    except MissingVariable as e:
        cfg.error = f"the environment variable {e} isn't set"
    tools = raw.get("tools")
    cfg.tools = [str(t) for t in tools] if isinstance(tools, list) else None
    free_gpu = raw.get("free_gpu", False)
    cfg.free_gpu = [str(t) for t in free_gpu] if isinstance(free_gpu, list) else bool(free_gpu)
    cfg.allow = [str(t) for t in raw.get("allow") or []] if isinstance(raw.get("allow"), list) else []
    try:
        cfg.timeout = float(raw.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        cfg.error = cfg.error or "`timeout` must be a number of seconds"
    return cfg


# ----------------------------------------------------------------------------- files


def read(path: Path) -> dict[str, dict]:
    """The `mcpServers` of a file; raises ValueError if it isn't valid."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} isn't valid JSON: {e}") from e
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError(f'{path} should contain an object with an "mcpServers" object')
    return servers


def load_user() -> dict[str, dict]:
    return read(user_path())


def save_user(servers: dict[str, dict]) -> Path:
    """Write the user's servers. The file can hold tokens, so only the user can read it."""
    path = user_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["mcpServers"] = servers
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)
    return path


def project_file(cwd: Path) -> Path | None:
    """The project's .mcp.json: in the working directory or a folder above it within the same repository."""
    root = next((f for f in (cwd, *cwd.parents) if (f / ".git").exists()), None)
    folders = [cwd] if root is None else [cwd, *[p for p in cwd.parents if p == root or root in p.parents]]
    return next((f / ".mcp.json" for f in folders if (f / ".mcp.json").is_file()), None)


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_approved(path: Path) -> bool:
    try:
        approved = json.loads(approvals_path().read_text())
    except (OSError, ValueError):
        return False
    return isinstance(approved, dict) and approved.get(str(path.resolve())) == _fingerprint(path)


def approve(path: Path) -> None:
    """Remember that the user trusts this exact version of a project's .mcp.json."""
    try:
        approved = json.loads(approvals_path().read_text())
        if not isinstance(approved, dict):
            approved = {}
    except (OSError, ValueError):
        approved = {}
    approved[str(path.resolve())] = _fingerprint(path)
    approvals_path().parent.mkdir(parents=True, exist_ok=True)
    approvals_path().write_text(json.dumps(approved, indent=2))


def load_all(cwd: Path, include_project: bool = True) -> tuple[list[ServerConfig], list[str]]:
    """Every configured server (project servers override user servers of the same name), and problems."""
    problems: list[str] = []
    servers: dict[str, ServerConfig] = {}
    try:
        for name, raw in load_user().items():
            servers[name] = parse(name, raw)
    except ValueError as e:
        problems.append(str(e))
    project = project_file(cwd) if include_project else None
    if project:
        try:
            for name, raw in read(project).items():
                servers[name] = parse(name, raw, source=str(project))
        except ValueError as e:
            problems.append(str(e))
    return list(servers.values()), problems
