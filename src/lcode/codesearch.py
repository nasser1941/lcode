"""Semantic code search with a local embedding model: find code by what it does, not by its words.

grep finds `retry`, not "where do failed uploads wait before trying again?". With an embedding
model installed in Ollama (qwen3-embedding or nomic-embed-text, for example), `lcode index` cuts the
repository's source files into overlapping chunks, embeds them and keeps the vectors in lcode's
state folder; the `search_code` tool embeds the question and returns the closest chunks with
`file:line`. Changed files are re-embedded on the next search.

On a single GPU, the embedding model would push the coding model out of memory, so searches and small
updates run it on the CPU; `lcode index` (outside a session) uses the GPU unless told not to.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lcode import config
from lcode.memory import slugify
from lcode.ollama import Ollama, OllamaError
from lcode.repomap import source_files

MODELS = ("qwen3-embedding:0.6b", "qwen3-embedding", "nomic-embed-text", "embeddinggemma", "mxbai-embed-large",
          "bge-m3", "all-minilm")  # fmt: skip  # tried in this order with embed_model = auto
CHUNK_LINES = 40
CHUNK_STEP = 32  # chunks overlap by 8 lines, so code at a boundary is in one of them whole
MAX_CHUNK_CHARS = 3000
BATCH = 16
EMBED_CONTEXT = 2048  # a chunk is far shorter; a small context keeps the embedding model small
SESSION_UPDATE_LIMIT = 60  # chunks re-embedded on the CPU during a search; more needs `lcode index`
QUERY = "Instruct: Given a question about a codebase, find the code that answers it\nQuery: {query}"
CPU = {"num_gpu": 0, "num_ctx": EMBED_CONTEXT}
GPU = {"num_ctx": EMBED_CONTEXT}


class SearchError(Exception):
    pass


def pick_model(ollama: Ollama, setting: str) -> str | None:
    """The embedding model to use: `setting` if it's a model name, else the first installed one."""
    if setting == "off":
        return None
    try:
        installed = ollama.installed_names()
    except OllamaError:
        return None
    if setting != "auto":
        return setting if setting in installed or f"{setting}:latest" in installed else None
    found = next((m for m in MODELS if m in installed), None)
    if found is None and getattr(ollama, "kind", "ollama") == "openai":  # e.g. LM Studio's text-embedding-…
        from lcode.backends import is_embedding

        found = next((m for m in sorted(installed) if is_embedding(m)), None)
    return found


def index_dir(root: Path) -> Path:
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]
    return config.STATE_DIR / "index" / f"{slugify(root.name, 40) or 'folder'}-{key}"


def chunks(rel: str, text: str) -> list[tuple[int, int, str]]:
    """Overlapping windows of a file: (first line, last line, text with its location on top)."""
    lines = text.splitlines()
    found = []
    for start in range(0, max(1, len(lines)), CHUNK_STEP):
        part = lines[start : start + CHUNK_LINES]
        if not any(line.strip() for line in part):
            continue
        end = start + len(part)
        body = "\n".join(part)[:MAX_CHUNK_CHARS]
        found.append((start + 1, end, f"{rel} (lines {start + 1}-{end})\n{body}"))
        if end >= len(lines):
            break
    return found


def normalize(vector: list[float]) -> array:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return array("f", (v / norm for v in vector))


@dataclass
class Hit:
    score: float
    path: str
    start: int
    end: int


class Index:
    """The vectors of one repository's code, on disk in lcode's state folder."""

    def __init__(self, root: Path, model: str):
        self.root = root.resolve()
        self.model = model
        self.folder = index_dir(self.root)
        self.files: dict[str, dict] = {}  # path -> {"mtime", "size", "chunks": [[start, end], ...]}
        self.vectors = array("f")
        self.dim = 0
        self.load()

    # -- storage
    def exists(self) -> bool:
        return bool(self.files)

    def load(self) -> None:
        try:
            meta = json.loads((self.folder / "meta.json").read_text())
            vectors = array("f")
            vectors.frombytes((self.folder / "vectors.f32").read_bytes())
        except (OSError, ValueError):
            return
        if meta.get("model") != self.model:
            return  # another model's vectors can't be compared with this one's
        self.files, self.dim, self.vectors = meta.get("files") or {}, int(meta.get("dim") or 0), vectors

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / "vectors.f32.tmp"
        tmp.write_bytes(self.vectors.tobytes())
        os.replace(tmp, self.folder / "vectors.f32")
        meta = {"model": self.model, "dim": self.dim, "root": str(self.root), "files": self.files}
        (self.folder / "meta.json").write_text(json.dumps(meta))

    def rows(self) -> list[tuple[str, int, int]]:
        return [(path, c[0], c[1]) for path, info in self.files.items() for c in info["chunks"]]

    # -- keeping it current
    def stale(self) -> tuple[list[Path], list[str]]:
        """Source files that are new or changed since they were indexed, and indexed files that are gone."""
        current = {p.relative_to(self.root).as_posix(): p for p in source_files(self.root)}
        changed = []
        for rel, path in current.items():
            try:
                stat = path.stat()
            except OSError:
                continue
            info = self.files.get(rel)
            if info is None or info["mtime"] != stat.st_mtime or info["size"] != stat.st_size:
                changed.append(path)
        return changed, [rel for rel in self.files if rel not in current]

    def update(self, ollama: Ollama, on_gpu: bool = False, limit: int | None = None,
               progress: Callable[[int, int], None] | None = None) -> tuple[int, int]:  # fmt: skip
        """Re-embed new and changed files, drop deleted ones. Returns (files, chunks) embedded.

        With `limit`, nothing is done when more than that many chunks would need embedding."""
        changed, removed = self.stale()
        todo: list[tuple[str, Path, list[tuple[int, int, str]]]] = []
        for path in changed:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            todo.append((rel, path, chunks(rel, text)))
        total = sum(len(c) for _, _, c in todo)
        if limit is not None and total > limit:
            raise SearchError(f"{len(todo)} files changed since the index was built ({total} chunks)")
        new_vectors: dict[str, list[array]] = {}
        done = 0
        texts = [(rel, c[2]) for rel, _, cs in todo for c in cs]
        for i in range(0, len(texts), BATCH):
            batch = texts[i : i + BATCH]
            try:
                vectors = ollama.embed(self.model, [t for _, t in batch], GPU if on_gpu else CPU)
            except OllamaError as e:
                raise SearchError(f"the embedding model {self.model} failed: {e}") from e
            for (rel, _), vector in zip(batch, vectors, strict=False):
                new_vectors.setdefault(rel, []).append(normalize(vector))
                self.dim = len(vector)
            done += len(batch)
            if progress:
                progress(done, total)
        # Rebuild the table: kept files keep their vectors, changed ones get the new ones.
        old_rows = self.rows()
        old_vectors = (
            {i: self.vectors[i * self.dim : (i + 1) * self.dim] for i in range(len(old_rows))} if self.dim else {}
        )
        replaced = {rel for rel, _, _ in todo} | set(removed)
        files: dict[str, dict] = {}
        vectors = array("f")
        row = 0
        for rel, info in self.files.items():
            n = len(info["chunks"])
            if rel not in replaced:
                files[rel] = info
                for k in range(n):
                    vectors.extend(old_vectors.get(row + k, array("f")))
            row += n
        for rel, path, cs in todo:
            stat = path.stat()
            files[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "chunks": [[c[0], c[1]] for c in cs]}
            for vector in new_vectors.get(rel, []):
                vectors.extend(vector)
        self.files, self.vectors = files, vectors
        self.save()
        return len(todo), total

    # -- searching
    def search(self, ollama: Ollama, query: str, k: int = 8, folder: str = "") -> list[Hit]:
        if not self.exists() or not self.dim:
            raise SearchError("there's no index for this repository yet: run `lcode index`")
        try:
            [vector] = ollama.embed(self.model, [QUERY.format(query=query) if "qwen3" in self.model else query], CPU)
        except (OllamaError, ValueError) as e:
            raise SearchError(f"the embedding model {self.model} failed: {e}") from e
        q = normalize(vector)
        dim, data = self.dim, self.vectors
        prefix = folder.strip().strip("/").removeprefix("./")
        hits = []
        for i, (path, start, end) in enumerate(self.rows()):
            if prefix and not path.startswith(prefix + "/") and path != prefix:
                continue
            row = data[i * dim : (i + 1) * dim]
            hits.append(Hit(sum(a * b for a, b in zip(q, row, strict=False)), path, start, end))
        hits.sort(key=lambda h: -h.score)
        best: list[Hit] = []
        for hit in hits:  # overlapping windows of the same code count once
            if any(h.path == hit.path and h.start <= hit.end and hit.start <= h.end for h in best):
                continue
            best.append(hit)
            if len(best) == k:
                break
        return best


def format_hits(root: Path, hits: list[Hit], lines_each: int = 12) -> str:
    if not hits:
        return "Nothing found."
    out = []
    for hit in hits:
        try:
            text = (root / hit.path).read_text(errors="replace").splitlines()[hit.start - 1 : hit.end]
        except OSError:
            text = []
        # Show the part of the window that has code, from its first non-blank line.
        first = next((i for i, line in enumerate(text) if line.strip()), 0)
        shown = text[first : first + lines_each]
        body = "\n".join(f"{hit.start + first + i:6}\t{line}" for i, line in enumerate(shown))
        out.append(f"{hit.path}:{hit.start + first}-{hit.end} (match {hit.score:.2f})\n{body}")
    return "\n\n".join(out)
