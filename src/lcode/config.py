"""User configuration (~/.config/lcode/config.toml) and on-disk state locations."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lcode"
CONFIG_PATH = CONFIG_DIR / "config.toml"
if os.environ.get("LCODE_HOME"):  # sessions and prompt history
    STATE_DIR = Path(os.environ["LCODE_HOME"])
else:
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "lcode"

DEFAULT_MODEL = "qwen3.6-35b"
PERMISSION_MODES = ("ask", "plan", "auto-edit", "yolo")  # Shift+Tab cycles in this order
WORK_MODES = ("code", "image", "video")  # what lcode makes (lcode.modes)
WEB_MODES = ("on", "ask", "off")
SEARCH_BACKENDS = ("auto", "ollama", "brave", "tavily", "searxng")
MCP_TOOL_MODES = ("auto", "direct", "search")
SANDBOX_ENGINES = ("off", "docker", "podman")
MEMORY_MODES = ("off", "ask", "auto")
SKILL_SOURCES = ("all", "lcode", "off")

# key -> (default, type, help)
SETTINGS: dict[str, tuple[object, type, str]] = {
    "model": (DEFAULT_MODEL, str, "catalog key (see `lcode models`) or any Ollama model tag"),
    "context": (None, int, "context window in tokens, e.g. 131072 or 128k (default: largest that fits)"),
    "num_batch": (None, int, "prompt batch size; larger reads prompts faster but needs more VRAM"),
    "keep_alive": ("30m", str, "how long Ollama keeps the model loaded after the last request"),
    "ollama_host": ("http://localhost:11434", str, "Ollama server URL"),
    "backend": ("ollama", str, "model server: ollama | lmstudio | llama.cpp | vllm | mlx | openai (any OpenAI API)"),
    "base_url": (None, str, "OpenAI-compatible server address, e.g. http://localhost:1234/v1 (default: the backend's)"),
    "permission_mode": ("ask", str, "ask | plan | auto-edit | yolo"),
    "mode": ("code", str, "what lcode makes when it starts: code | image | video"),
    "comfyui_url": ("http://127.0.0.1:8188", str, "ComfyUI, for image and video mode"),
    "comfyui_path": (None, str, "your ComfyUI folder, if lcode doesn't find it (lcode starts it when needed)"),
    "media_dir": (".lcode/media", str, "where images and videos go: a folder in the project, or an absolute path"),
    "image_model": ("flux2-klein-4b", str, "the model image mode uses: a key from `lcode media`"),
    "video_model": ("wan22-ti2v-5b", str, "the model video mode uses: a key from `lcode media`"),
    "prompt_assistant": ("video", str, "a small model writes fuller prompts: video | all | off | an Ollama model"),
    "check_results": (False, bool, "the prompt assistant checks each image against the request"),
    "preview": ("auto", str, "images in the terminal: auto | kitty | iterm | blocks (colour text) | off"),
    "think": (True, bool, "let the model reason before answering (slower, better)"),
    "web": ("on", str, "web search and page fetching for the model: on | ask | off"),
    "search_backend": ("auto", str, "auto | ollama | brave | tavily | searxng (keys come from environment variables)"),
    "searxng_url": (None, str, "your SearXNG instance, e.g. http://localhost:8888"),
    "checkpoints": (True, bool, "snapshot files before the model changes them, so /undo can restore them"),
    "sandbox": ("off", str, "run the model's shell commands in a container: off | docker | podman"),
    "sandbox_image": (None, str, "container image for the sandbox (default: lcode's, built on first use)"),
    "sandbox_network": (False, bool, "let commands in the sandbox use the network"),
    "vision_model": ("auto", str, "model that looks at images: auto | off | an Ollama model with vision"),
    "mcp_tools": ("auto", str, "how MCP tools reach the model: auto | direct | search (on demand, saves context)"),
    "memory": ("ask", str, "notes that carry over to later sessions: off | ask (confirm each) | auto"),
    "memory_check": (
        False,
        bool,
        "when a conversation ends (quit, /clear, or before it's summarized), ask the model what's worth remembering",
    ),
    "skills": ("all", str, "skills to offer the model: all (also other agents' folders) | lcode | off"),
    "repo_map": (True, bool, "give the model a ranked map of the repository's symbols"),
    "embed_model": ("auto", str, "embedding model for semantic code search (lcode index): auto | off | a model"),
    "lsp": ("auto", str, "language servers for code navigation and errors after edits: auto | off"),
    "prune": (True, bool, "before summarizing a full conversation, first remove old tool output from it"),
    "subagents": (True, bool, "let the model hand tasks to subagents that have their own context"),
    "notify": (True, bool, "desktop notification when a long request is done or waits for you"),
    "notify_after": (30, int, "seconds a request runs before it notifies (with notify on)"),
    "review": ("off", str, "before the answer, check a change against the request and send problems back: auto | off"),
    "checks": ("auto", str, "run the project's checks before calling work done: auto | off | a command"),
    "escalate_model": (
        None,
        str,
        "a stronger model to ask, with your OK, when the model is stuck: anthropic/…, openai/…, xai/… (key from "
        "ANTHROPIC_API_KEY, OPENAI_API_KEY, XAI_API_KEY) or ollama/<model> (local or at escalate_url)",
    ),
    "escalate_url": (None, str, "the stronger model's server, if not its provider's (another Ollama, any OpenAI API)"),
    "escalate_price": (None, str, "its price in dollars per million tokens in/out, e.g. 3/15, for the cost shown"),
    "repeat_limit": (3, int, "identical tool calls with identical results before lcode steps in (0: never)"),
    "secret_check": (True, bool, "before git commit and git push, look for passwords, keys and tokens they'd add"),
    "compact_at": (65536, int, "summarize at this many tokens even when the window is larger (0: only at 85%)"),
    "max_parallel_agents": (1, int, "subagents that may run at the same time (more needs OLLAMA_NUM_PARALLEL)"),
}
ENV_OVERRIDES = {
    "LCODE_MODEL": "model",
    "LCODE_CONTEXT": "context",
    "LCODE_NUM_BATCH": "num_batch",
    "LCODE_KEEP_ALIVE": "keep_alive",
    "OLLAMA_HOST": "ollama_host",
    "LCODE_BACKEND": "backend",
    "LCODE_BASE_URL": "base_url",
    "LCODE_WEB": "web",
    "LCODE_SANDBOX": "sandbox",
    "LCODE_MEMORY": "memory",
}


class ConfigError(ValueError):
    pass


MIN_COMPACT_AT = 16384  # below this, the system prompt and a summary alone come close to the ceiling


def parse_compact_at(value: str | int) -> int:
    """compact_at: a token count like parse_context's, at least 16K, or 0 (off)."""
    if str(value).strip().lower() in ("0", "off", "false", "none"):
        return 0
    tokens = parse_context(value)
    if tokens < MIN_COMPACT_AT:
        raise ConfigError(f"compact_at {tokens} is too small: use at least 16k, or 0 to turn it off")
    return tokens


def parse_context(value: str | int) -> int:
    """Parse '131072', '128k', '128K' or '1m' into a token count (k = 1024)."""
    if isinstance(value, int):
        n = value
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", str(value))
        if not m:
            raise ConfigError(f"invalid context size {value!r}; use e.g. 131072, 128k or 1m")
        n = int(float(m.group(1)) * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()])
    if n < 2048:
        raise ConfigError(f"context size {n} is too small (minimum 2048)")
    return n


def format_tokens(n: int) -> str:
    if n >= 1024 * 1024 and n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)}M"
    if n >= 1024 and n % 1024 == 0:
        return f"{n // 1024}K"
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def normalize_host(host: str) -> str:
    host = host.strip()
    if "://" not in host:
        host = "http://" + host
    return host.replace("0.0.0.0", "localhost").rstrip("/")


def coerce(key: str, value: object) -> object:
    if key not in SETTINGS:
        raise ConfigError(f"unknown setting {key!r}; valid: {', '.join(SETTINGS)}")
    if value is None:
        return None
    _, typ, _ = SETTINGS[key]
    if key == "context":
        return parse_context(value)  # type: ignore[arg-type]
    if key == "compact_at":
        return parse_compact_at(value)  # type: ignore[arg-type]
    if key in ("ollama_host", "searxng_url"):
        return normalize_host(str(value))
    if key == "base_url":
        url = normalize_host(str(value))
        return url if urlsplit(url).path.strip("/") else url + "/v1"  # the OpenAI API lives under /v1
    if key == "backend":
        from lcode.backends import BACKENDS

        value = {"lm-studio": "lmstudio", "llamacpp": "llama.cpp", "llama-server": "llama.cpp"}.get(
            str(value).lower(), str(value).lower()
        )
        if value not in BACKENDS:
            raise ConfigError(f"backend must be one of {', '.join(BACKENDS)}")
        return value
    if key == "web":
        value = {"true": "on", "false": "off", "yes": "on", "no": "off"}.get(str(value).lower(), str(value).lower())
        if value not in WEB_MODES:
            raise ConfigError(f"web must be one of {', '.join(WEB_MODES)}")
        return value
    if key == "sandbox":
        value = {"none": "off", "false": "off", "no": "off"}.get(str(value).lower(), str(value).lower())
        if value not in SANDBOX_ENGINES:
            raise ConfigError(f"sandbox must be one of {', '.join(SANDBOX_ENGINES)}")
        return value
    if key == "memory":
        value = {"on": "ask", "true": "ask", "false": "off", "no": "off"}.get(str(value).lower(), str(value).lower())
        if value not in MEMORY_MODES:
            raise ConfigError(f"memory must be one of {', '.join(MEMORY_MODES)}")
        return value
    if key in ("escalate_model", "escalate_url", "escalate_price"):
        from lcode import escalate

        text = str(value).strip()
        if key == "escalate_url":
            return normalize_host(text)
        if key == "escalate_model":
            provider = text.partition("/")[0].lower()
            if "/" in text and provider not in escalate.PROVIDERS:
                raise ConfigError(f"escalate_model's provider must be one of {', '.join(escalate.PROVIDERS)}")
            return text
        try:
            escalate.parse_price(text)
        except escalate.EscalateError as e:
            raise ConfigError(str(e)) from e
        return text
    if key == "lsp" and str(value).lower() not in ("auto", "off"):
        raise ConfigError("lsp must be auto or off")
    if key == "lsp":
        return str(value).lower()
    if key == "skills" and str(value).lower() not in SKILL_SOURCES:
        raise ConfigError(f"skills must be one of {', '.join(SKILL_SOURCES)}")
    if key == "skills":
        return str(value).lower()
    if key == "mcp_tools" and value not in MCP_TOOL_MODES:
        raise ConfigError(f"mcp_tools must be one of {', '.join(MCP_TOOL_MODES)}")
    if key == "search_backend" and value not in SEARCH_BACKENDS:
        raise ConfigError(f"search_backend must be one of {', '.join(SEARCH_BACKENDS)}")
    if key == "permission_mode" and value not in PERMISSION_MODES:
        raise ConfigError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    if key == "mode" and value not in WORK_MODES:
        raise ConfigError(f"mode must be one of {', '.join(WORK_MODES)}")
    if key == "preview":
        from lcode.preview import PROTOCOLS

        if value not in PROTOCOLS:
            raise ConfigError(f"preview must be one of {', '.join(PROTOCOLS)}")
    if key in ("image_model", "video_model"):
        from lcode import media_catalog

        keys = [m.key for m in media_catalog.load().for_mode(key.removesuffix("_model"))]
        if value not in keys:
            raise ConfigError(f"{key} must be one of {', '.join(keys)} (see lcode media)")
    if typ is bool and isinstance(value, str):
        if value.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raise ConfigError(f"{key} must be true or false")
        return value.lower() in ("true", "1", "yes", "on")
    if typ is int:
        try:
            number = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{key} must be an integer") from e
        if key == "max_parallel_agents" and not 1 <= number <= 8:
            raise ConfigError("max_parallel_agents must be between 1 and 8")
        return number
    return typ(value)


def read_file(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    return {k: coerce(k, v) for k, v in data.items() if k in SETTINGS}


def load(path: Path | None = None) -> dict:
    """Defaults < config file < environment variables."""
    cfg = {k: default for k, (default, _, _) in SETTINGS.items()}
    cfg.update(read_file(path))
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[key] = coerce(key, os.environ[env])
    return cfg


def save(updates: dict, path: Path | None = None, remove: tuple[str, ...] = ()) -> None:
    path = path or CONFIG_PATH
    data = read_file(path)
    data.update({k: coerce(k, v) for k, v in updates.items()})
    for k in remove:
        data.pop(k, None)
    lines = ["# lcode configuration — see `lcode config` or https://nasser1941.github.io/lcode/configuration/"]
    for k in SETTINGS:
        if data.get(k) is not None:
            v = data[k]
            lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else json.dumps(v)}")
    tables = ""  # [[hooks]], [permissions] and anything else after the settings stay as they were
    if path.is_file():
        text = path.read_text()
        m = re.search(r"^\s*\[", text, re.M)
        tables = text[m.start() :] if m else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" + ("\n" + tables.rstrip() + "\n" if tables.strip() else ""))
