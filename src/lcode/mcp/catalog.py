"""Ready-made MCP servers (catalog.toml) and turning one into an mcp.json entry."""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CATALOG_PATH = Path(__file__).with_name("catalog.toml")


@dataclass
class Input:
    var: str
    prompt: str
    secret: bool = False
    default: str | None = None
    optional: bool = False
    from_command: list[str] = field(default_factory=list)  # a command whose output can be used

    def suggestion(self) -> str | None:
        """A value to offer: from the environment's tools (such as `gh auth token`), if any."""
        if not self.from_command or not shutil.which(self.from_command[0]):
            return None
        try:
            out = subprocess.run(self.from_command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        value = out.stdout.strip()
        return value if out.returncode == 0 and value and "\n" not in value else None


@dataclass
class Preset:
    key: str
    name: str
    description: str
    server: dict
    homepage: str = ""
    setup: str = ""
    requires: list[str] = field(default_factory=list)
    inputs: list[Input] = field(default_factory=list)
    login: bool = False

    @property
    def kind(self) -> str:
        return "remote" if self.server.get("url") else "local"

    def missing(self) -> list[str]:
        """Required commands that aren't installed."""
        return [c for c in self.requires if not shutil.which(c)]


def load() -> dict[str, Preset]:
    data = tomllib.loads(CATALOG_PATH.read_text())
    presets = {}
    for key, entry in data.items():
        inputs = [Input(**i) for i in entry.get("inputs", [])]
        fields = {k: v for k, v in entry.items() if k != "inputs"}
        presets[key] = Preset(key=key, inputs=inputs, **fields)
    return presets


def _replace(value, var: str, new: str):
    if isinstance(value, str):
        return value.replace("${" + var + "}", new)
    if isinstance(value, list):
        return [_replace(v, var, new) for v in value]
    if isinstance(value, dict):
        return {k: _replace(v, var, new) for k, v in value.items()}
    return value


def _drop(value, var: str):
    """Remove settings that use an optional input the user skipped."""
    marker = "${" + var + "}"
    if isinstance(value, dict):
        return {k: _drop(v, var) for k, v in value.items() if not (isinstance(v, str) and marker in v)}
    if isinstance(value, list):
        return [_drop(v, var) for v in value]
    return value


def build(preset: Preset, answers: dict[str, str | None]) -> dict:
    """The mcp.json entry for a preset.

    `answers` maps each input's variable to the value typed by the user, or None to keep reading it
    from the environment variable (as ${VAR}). Skipped optional inputs remove the settings using them.
    """
    server = copy.deepcopy(preset.server)
    for spec in preset.inputs:
        value = answers.get(spec.var)
        if value:
            server = _replace(server, spec.var, value)
        elif spec.optional and not os.environ.get(spec.var):
            server = _drop(server, spec.var)
    for key in ("headers", "env"):
        if key in server and not server[key]:
            del server[key]
    return server
