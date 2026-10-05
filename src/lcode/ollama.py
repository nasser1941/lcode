"""A small client for the Ollama HTTP API (works with native, Docker and remote servers)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

import requests

MIN_VERSION = (0, 30, 0)  # first release that runs the Qwen3.5/3.6 hybrid-attention architecture


class OllamaError(RuntimeError):
    pass


def redact(url: str) -> str:
    """Hide credentials embedded in a URL (http://user:pass@host) before printing it."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
    return url


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version)[:3])


class Ollama:
    kind = "ollama"
    name = "Ollama"

    def __init__(self, host: str):
        self.host = host.rstrip("/")

    def describe(self) -> str:
        return f"Ollama at {redact(self.host)}"

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _post(self, path: str, payload: dict, timeout: float | None = 30) -> dict:
        try:
            r = requests.post(self._url(path), json=payload, timeout=timeout)
        except requests.RequestException as e:
            raise OllamaError(f"cannot reach Ollama at {redact(self.host)}: {e}") from e
        if r.status_code != 200:
            raise OllamaError(f"Ollama {path} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    # -- info
    def version(self) -> str:
        try:
            return requests.get(self._url("/api/version"), timeout=5).json()["version"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise OllamaError(f"cannot reach Ollama at {redact(self.host)}: {e}") from e

    def list_models(self) -> list[dict]:
        try:
            return requests.get(self._url("/api/tags"), timeout=10).json().get("models", [])
        except (requests.RequestException, ValueError) as e:
            raise OllamaError(f"cannot reach Ollama at {redact(self.host)}: {e}") from e

    def installed_names(self) -> set[str]:
        names = set()
        for m in self.list_models():
            names.add(m["name"])
            if m["name"].endswith(":latest"):
                names.add(m["name"][: -len(":latest")])
        return names

    def chat(self, payload: dict, timeout: float = 900) -> dict:
        """One chat request without streaming."""
        return self._post("/api/chat", {**payload, "stream": False}, timeout=timeout)

    def embed(
        self, model: str, inputs: list[str], options: dict | None = None, keep_alive: str = "5m"
    ) -> list[list[float]]:
        """Embedding vectors for the inputs (cut to the model's context if too long)."""
        payload = {
            "model": model,
            "input": inputs,
            "truncate": True,
            "keep_alive": keep_alive,
            "options": options or {},
        }
        return self._post("/api/embed", payload, timeout=600).get("embeddings") or []

    def show(self, model: str) -> dict:
        return self._post("/api/show", {"model": model})

    def max_context(self, model: str) -> int | None:
        info = self.show(model).get("model_info", {})
        return next((int(v) for k, v in info.items() if k.endswith(".context_length")), None)

    def running(self) -> list[dict]:
        try:
            return requests.get(self._url("/api/ps"), timeout=5).json().get("models", [])
        except (requests.RequestException, ValueError):
            return []

    # -- management
    def pull(self, model: str, on_progress: Callable[[dict], None]) -> None:
        try:
            with requests.post(
                self._url("/api/pull"), json={"model": model, "stream": True}, stream=True, timeout=(10, None)
            ) as r:
                if r.status_code != 200:
                    raise OllamaError(f"pull failed ({r.status_code}): {r.text[:300]}")
                for line in r.iter_lines():
                    if line:
                        event = json.loads(line)
                        if "error" in event:
                            raise OllamaError(f"pull failed: {event['error']}")
                        on_progress(event)
        except requests.RequestException as e:
            raise OllamaError(f"pull failed: {e}") from e

    def create_text_only(self, name: str, base: str) -> bool:
        """Create `name` from `base` without its vision projector. Returns False if `base` has none.

        Only the language-model weights are referenced (no copy, no extra disk). Dropping the
        projector saves ~1 GB of VRAM, which Ollama under-counts on small GPUs.
        """
        show = self.show(base)
        modelfile = show.get("modelfile", "")
        blobs = re.findall(r"^FROM .*?sha256[-:]([0-9a-f]{64})", modelfile, re.M)
        if len(blobs) < 2 and "projector_info" not in show:
            return False
        if not blobs:
            raise OllamaError(f"could not find the weights of {base}")
        params: dict[str, list[str]] = {}
        for line in show.get("parameters", "").splitlines():
            key, _, value = line.strip().partition(" ")
            if key:
                params.setdefault(key, []).append(value.strip().strip('"'))
        payload: dict = {
            "model": name,
            "files": {"model.gguf": f"sha256:{blobs[0]}"},
            "template": show.get("template", ""),
            "parameters": {k: (v if k == "stop" else _number(v[-1])) for k, v in params.items()},
            "stream": False,
        }
        for key in ("renderer", "parser"):
            m = re.search(rf"^{key.upper()} (\S+)", modelfile, re.M)
            if m:
                payload[key] = m.group(1)
        if self._post("/api/create", payload, timeout=600).get("status") != "success":
            raise OllamaError(f"creating {name} failed")
        return True

    # -- inference
    def chat_stream(self, payload: dict, on_open: Callable[[requests.Response], None] | None = None) -> Iterator[dict]:
        """Stream a chat. `on_open` gets the response, so another thread can abort it."""
        try:
            r = requests.post(self._url("/api/chat"), json={**payload, "stream": True}, stream=True, timeout=(10, None))
        except requests.RequestException as e:
            raise OllamaError(f"cannot reach Ollama at {redact(self.host)}: {e}") from e
        if on_open:
            on_open(r)
        with r:
            if r.status_code != 200:
                raise OllamaError(f"Ollama error {r.status_code}: {r.text[:500]}")
            for line in r.iter_lines():
                if line:
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise OllamaError(f"Ollama error: {chunk['error']}")
                    yield chunk

    def unload(self, model: str) -> None:
        """Free the memory a model is using."""
        self._post("/api/generate", {"model": model, "keep_alive": 0}, timeout=60)

    def load(self, model: str, options: dict, keep_alive: str) -> None:
        """Load a model into memory without generating anything."""
        self._post(
            "/api/chat", {"model": model, "messages": [], "keep_alive": keep_alive, "options": options}, timeout=900
        )


def _number(value: str) -> int | float | str:
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value
