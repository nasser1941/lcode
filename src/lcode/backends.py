"""Model servers other than Ollama: anything with an OpenAI-compatible API.

LM Studio, llama.cpp's llama-server, vLLM and MLX servers (mlx_lm.server, LM Studio's MLX engine)
all speak the OpenAI chat completions API, with streaming and tool calls. `OpenAICompatible` gives
lcode the same interface as its Ollama client, translating on the way: tool calls get ids and
JSON-string arguments, streamed tool calls arrive in pieces, reasoning text comes as
`reasoning_content`, and Ollama's `format` becomes `response_format`.

What only Ollama can do degrades cleanly: lcode can't unload the model to free the GPU, can't ask
for a context size (the server has its own; lcode reads it where the server says), and doesn't know
whether a model can see images unless you set `vision_model`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from urllib.parse import quote

import requests

from lcode.ollama import OllamaError, redact

PRESETS = {  # backend setting -> (the server's name, its default address)
    "lmstudio": ("LM Studio", "http://localhost:1234/v1"),
    "llama.cpp": ("llama-server", "http://localhost:8080/v1"),
    "vllm": ("vLLM", "http://localhost:8000/v1"),
    "mlx": ("MLX server", "http://localhost:8080/v1"),
    "openai": ("OpenAI-compatible server", ""),
}
BACKENDS = ("ollama", *PRESETS)
DEFAULT_CONTEXT = 32768  # when the server doesn't say its context size
EMBEDDING_HINTS = ("embed", "bge-", "minilm", "e5-")  # model ids that are embedding models, not chat models
GRAMMAR_ERRORS = ("grammar", "sampler", "response_format", "json_schema")  # a server that can't use a schema
CONTEXT_ERRORS = ("context size", "context length", "context window", "maximum context", "exceeds the")


class BackendError(OllamaError):
    """The model server failed; the agent handles it like an Ollama error."""


IMAGE_TYPES = {"iVBOR": "image/png", "/9j/": "image/jpeg", "R0lGOD": "image/gif", "UklGR": "image/webp"}


def with_images(text: str, images: list[str]) -> list[dict]:
    """A message's text and base64 images (Ollama's `images`) as OpenAI content parts."""
    parts: list[dict] = [{"type": "text", "text": text}]
    for data in images:
        mime = next((t for prefix, t in IMAGE_TYPES.items() if data.startswith(prefix)), "image/png")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts


def to_openai(messages: list[dict]) -> list[dict]:
    """lcode's messages (Ollama's format) as OpenAI chat messages."""
    out: list[dict] = []
    pending: list[str] = []  # ids of the latest tool calls, for the tool results that follow
    for n, m in enumerate(messages):
        role = m.get("role")
        if role in ("system", "user"):
            text = m.get("content") or ""
            out.append({"role": role, "content": with_images(text, m["images"]) if m.get("images") else text})
        elif role == "assistant":
            message: dict = {"role": "assistant", "content": m.get("content") or ""}
            calls = m.get("tool_calls") or []
            if calls:
                ids = [c.get("id") or f"call_{n}_{i}" for i, c in enumerate(calls)]
                message["tool_calls"] = []
                for call_id, call in zip(ids, calls, strict=False):
                    fn = call.get("function") or {}
                    args = fn.get("arguments") or {}
                    message["tool_calls"].append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": args if isinstance(args, str) else json.dumps(args),
                            },
                        }
                    )
                pending = list(ids)
            out.append(message)
        elif role == "tool":
            call_id = m.get("tool_call_id") or (pending.pop(0) if pending else f"call_{n}")
            if m.get("tool_call_id") and call_id in pending:
                pending.remove(call_id)
            out.append({"role": "tool", "tool_call_id": call_id, "content": m.get("content") or ""})
    return out


class ThinkSplitter:
    """Moves a leading <think>…</think> block out of the answer, for servers that don't separate reasoning.

    vLLM without a reasoning parser, MLX's server and some LM Studio setups stream a reasoning model's
    thinking as ordinary text. The tags can arrive split across chunks.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.state = "start"  # start -> thinking -> answer, or start -> answer
        self.trim = False  # drop the blank lines between the thinking and the answer

    def feed(self, text: str) -> list[tuple[str, str]]:
        """("thinking" or "content", text) pieces that are ready to show."""
        self.buffer += text
        out: list[tuple[str, str]] = []
        if self.state == "start":
            head = self.buffer.lstrip()
            if head.startswith(self.OPEN):
                self.state, self.buffer = "thinking", head[len(self.OPEN) :]
            elif not head or self.OPEN.startswith(head):
                return out  # maybe the start of the tag: wait for more
            else:
                self.state = "answer"
        if self.state == "thinking":
            end = self.buffer.find(self.CLOSE)
            if end >= 0:
                out.append(("thinking", self.buffer[:end]))
                self.state, self.trim, self.buffer = "answer", True, self.buffer[end + len(self.CLOSE) :]
            else:
                keep = next((n for n in range(len(self.CLOSE) - 1, 0, -1) if self.buffer.endswith(self.CLOSE[:n])), 0)
                ready, self.buffer = self.buffer[: len(self.buffer) - keep], self.buffer[len(self.buffer) - keep :]
                out.append(("thinking", ready))
        if self.state == "answer":
            if self.trim:
                self.buffer = self.buffer.lstrip("\n")
                self.trim = not self.buffer
            out.append(("content", self.buffer))
            self.buffer = ""
        return [(kind, piece) for kind, piece in out if piece]

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buffer = self.buffer, ""
        return [("thinking" if self.state == "thinking" else "content", rest)] if rest else []


def strip_thinking(text: str) -> str:
    """An answer without its leading <think>…</think> block."""
    splitter = ThinkSplitter()
    pieces = splitter.feed(text) + splitter.flush()
    return "".join(piece for kind, piece in pieces if kind == "content")


class OpenAICompatible:
    """A model server with an OpenAI-compatible API, behind the same interface as lcode's Ollama client."""

    kind = "openai"

    def __init__(self, base_url: str, api_key: str | None = None, name: str = "OpenAI-compatible server"):
        self.name = name
        self.host = base_url.rstrip("/")
        self.root = self.host.removesuffix("/v1")
        key = api_key if api_key is not None else os.environ.get("LCODE_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}

    # -- plumbing
    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def describe(self) -> str:
        return f"{self.name} at {redact(self.host)}"

    def _get(self, url: str, timeout: float = 10) -> dict:
        try:
            r = requests.get(url, headers=self.headers, timeout=timeout)
        except requests.RequestException as e:
            raise BackendError(f"cannot reach the model server at {redact(self.host)}: {e}") from e
        if r.status_code != 200:
            raise BackendError(f"{url.removeprefix(self.root)} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def _payload(self, payload: dict, stream: bool) -> dict:
        body: dict = {"model": payload["model"], "messages": to_openai(payload.get("messages") or []), "stream": stream}
        if payload.get("tools") and not payload.get("format"):
            # An answer in a given format has no tool calls, and llama-server can't combine the two grammars.
            body["tools"] = payload["tools"]
        if stream:
            body["stream_options"] = {"include_usage": True}
        if payload.get("think") is False:
            body["chat_template_kwargs"] = {"enable_thinking": False}  # Qwen-style models on vLLM and llama.cpp
        schema = payload.get("format")
        if isinstance(schema, dict):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        elif schema == "json":
            body["response_format"] = {"type": "json_object"}
        if (payload.get("options") or {}).get("num_predict"):
            body["max_tokens"] = payload["options"]["num_predict"]
        return body

    def _failed(self, status: int, text: str) -> BackendError:
        message = f"{self.name} error {status}: {text[:500]}"
        if any(marker in text.lower() for marker in CONTEXT_ERRORS):
            message += (
                "\nThe conversation no longer fits the server's context window. /compact summarizes it; lcode's "
                "context setting (/ctx) should be no larger than the server's."
            )
        return BackendError(message)

    # -- what the agent uses
    def version(self) -> str:
        self._get(self._url("/models"))
        return self.name

    def list_models(self) -> list[dict]:
        return [{"name": m.get("id", ""), **m} for m in self._get(self._url("/models")).get("data") or []]

    def installed_names(self) -> set[str]:
        return {m["name"] for m in self.list_models()}

    def chat_models(self) -> list[str]:
        """The models served for chat (servers also list their embedding models)."""
        return sorted(n for n in self.installed_names() if not is_embedding(n))

    def show(self, model: str) -> dict:
        return {"capabilities": ["completion", "tools"]}  # the API doesn't say; vision_model can name a model

    def max_context(self, model: str) -> int | None:
        """The server's context window, where it tells: vLLM, LM Studio or llama-server."""
        try:
            for entry in self.list_models():
                if entry.get("name") == model and entry.get("max_model_len"):  # vLLM
                    return int(entry["max_model_len"])
        except BackendError:
            return None
        for url, read in (
            # LM Studio: the size the model was loaded with (it loads models with its own default otherwise)
            (f"{self.root}/api/v0/models/{quote(model, safe='')}", lambda d: d.get("loaded_context_length")),
            (f"{self.root}/props", lambda d: (d.get("default_generation_settings") or {}).get("n_ctx")),  # llama-server
        ):  # fmt: skip
            try:
                value = read(self._get(url, timeout=5))
            except (BackendError, ValueError, AttributeError):
                continue
            if value:
                return int(value)
        return None

    def running(self) -> list[dict]:
        return []  # the server decides what it keeps loaded

    def load(self, model: str, options: dict, keep_alive: str) -> None:
        pass

    def unload(self, model: str) -> None:
        pass  # not possible through this API: free_gpu tools share the GPU with the model

    def chat(self, payload: dict, timeout: float = 900) -> dict:
        try:
            r = requests.post(
                self._url("/chat/completions"),
                json=self._payload(payload, False),
                headers=self.headers,
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise BackendError(f"cannot reach the model server at {redact(self.host)}: {e}") from e
        if r.status_code == 400 and payload.get("format") and any(w in r.text.lower() for w in GRAMMAR_ERRORS):
            # The server can't hold the answer to a schema (llama-server can't with some settings):
            # ask without one. The prompt asks for the same JSON, and lcode reads it out of the text.
            return self.chat({k: v for k, v in payload.items() if k not in ("format", "tools")}, timeout)
        if r.status_code != 200:
            raise self._failed(r.status_code, r.text)
        choice = (r.json().get("choices") or [{}])[0].get("message") or {}
        message: dict = {"role": "assistant", "content": strip_thinking(choice.get("content") or "")}
        if choice.get("tool_calls"):
            message["tool_calls"] = [self._ollama_call(c) for c in choice["tool_calls"]]
        return {"message": message}

    @staticmethod
    def _ollama_call(call: dict) -> dict:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            # The same wording as Ollama's, so the agent asks the model to try again.
            raise BackendError(f"error parsing tool call: raw='{raw[:200]}', err={e}") from e
        return {"id": call.get("id", ""), "function": {"name": fn.get("name", ""), "arguments": args}}

    def chat_stream(self, payload: dict, on_open: Callable | None = None) -> Iterator[dict]:
        """Ollama-style chunks from an OpenAI-style stream: content, thinking, then tool calls and counts."""
        started = time.monotonic_ns()
        try:
            r = requests.post(
                self._url("/chat/completions"), json=self._payload(payload, True), headers=self.headers,
                stream=True, timeout=(10, None),
            )  # fmt: skip
        except requests.RequestException as e:
            raise BackendError(f"cannot reach the model server at {redact(self.host)}: {e}") from e
        if on_open:
            on_open(r)
        calls: dict[int, dict] = {}
        usage: dict = {}
        timings: dict = {}  # llama-server measures prompt and generation time itself
        splitter = ThinkSplitter()
        first_token = None
        with r:
            if r.status_code != 200:
                raise self._failed(r.status_code, r.text)
            for line in r.iter_lines():
                if not line or not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    error = chunk["error"]
                    raise self._failed(0, error.get("message", str(error)) if isinstance(error, dict) else str(error))
                usage = chunk.get("usage") or usage
                timings = chunk.get("timings") or timings
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    thinking = delta.get("reasoning_content") or delta.get("reasoning")
                    if thinking:
                        first_token = first_token or time.monotonic_ns()
                        yield {"message": {"thinking": thinking}, "done": False}
                    if delta.get("content"):
                        first_token = first_token or time.monotonic_ns()
                        for kind, piece in splitter.feed(delta["content"]):
                            yield {"message": {kind: piece}, "done": False}
                    for piece in delta.get("tool_calls") or []:
                        first_token = first_token or time.monotonic_ns()
                        slot = calls.setdefault(piece.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        fn = piece.get("function") or {}
                        slot["id"] = piece.get("id") or slot["id"]
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
        for kind, piece in splitter.flush():
            yield {"message": {kind: piece}, "done": False}
        if calls:
            tool_calls = [
                self._ollama_call({"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}})
                for _, c in sorted(calls.items())
            ]
            yield {"message": {"tool_calls": tool_calls}, "done": False}
        ended = time.monotonic_ns()
        first_token = first_token or ended
        final = {
            "message": {},
            "done": True,
            "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
            "prompt_eval_duration": first_token - started,
            "eval_count": int(usage.get("completion_tokens") or 0),
            "eval_duration": max(1, ended - first_token),
        }
        if timings.get("prompt_ms") is not None and timings.get("predicted_ms"):
            # The whole prompt counts (it's what fills the context), cached tokens included.
            prompt = final["prompt_eval_count"] or int(timings.get("prompt_n", 0)) + int(timings.get("cache_n", 0))
            final.update(
                prompt_eval_count=prompt,
                prompt_eval_duration=int(timings["prompt_ms"] * 1e6),
                eval_count=final["eval_count"] or int(timings.get("predicted_n", 0)),
                eval_duration=max(1, int(timings["predicted_ms"] * 1e6)),
            )
        yield final

    def embed(self, model: str, inputs: list[str], options: dict | None = None, keep_alive: str = "5m") -> list:
        try:
            r = requests.post(
                self._url("/embeddings"), json={"model": model, "input": inputs}, headers=self.headers, timeout=600
            )
        except requests.RequestException as e:
            raise BackendError(f"cannot reach the model server at {redact(self.host)}: {e}") from e
        if r.status_code != 200:
            raise BackendError(f"{self.name} embeddings failed ({r.status_code}): {r.text[:300]}")
        return [item["embedding"] for item in sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))]


def is_openai(client) -> bool:
    return getattr(client, "kind", "ollama") == "openai"


def is_embedding(model: str) -> bool:
    return any(hint in model.lower() for hint in EMBEDDING_HINTS)


def pick_model(client: OpenAICompatible, name: str) -> str:
    """The served model `name` stands for: the same id, else the one id that contains it, else the only one."""
    served = client.chat_models()
    if name in served or not served:
        return name  # nothing listed: some servers load models on request
    matches = [m for m in served if name.lower() in m.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(served) == 1:
        return served[0]  # llama-server serves one model, whatever it's called
    listed = ", ".join(served) if served else "none"
    raise BackendError(
        f"{client.describe()} doesn't serve '{name}'. Its models: {listed}. "
        "Choose one with --model or `lcode config set model <id>`."
    )


def context_for(client: OpenAICompatible, model: str, requested: int | None) -> tuple[int, str]:
    """lcode's context budget: the server's window (lcode can't change it), or less when asked."""
    try:
        limit = client.max_context(model)
    except BackendError:
        limit = None
    if limit and requested and requested > limit:
        return limit, f"capped at the server's context window of {limit:,} tokens"
    if requested or limit:
        return requested or limit, ""  # type: ignore[return-value]
    return DEFAULT_CONTEXT, (
        f"{client.name} doesn't say how large its context window is; lcode assumes {DEFAULT_CONTEXT:,} tokens. "
        "Set the real size with --context or `lcode config set context`"
    )


def connect(cfg: dict):
    """The model server the settings point at: Ollama, or a server with an OpenAI-compatible API."""
    from lcode.ollama import Ollama

    backend = cfg.get("backend") or "ollama"
    if backend == "ollama":
        return Ollama(cfg["ollama_host"])
    name, default_url = PRESETS[backend]
    url = cfg.get("base_url") or default_url
    if not url:
        raise BackendError("backend = openai needs the server's address: lcode config set base_url http://host:port/v1")
    return OpenAICompatible(url, name=name)
