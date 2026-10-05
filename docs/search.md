# Repository map and code search

On an unfamiliar codebase the model spends many steps finding its way around, and `grep` only
finds exact words: not "where do failed uploads wait before trying again?". Two things help.

## The repository map

A compact outline of the codebase: for each important file, its classes and functions with their
signatures and line numbers, ranked by how much of the rest of the code uses them, and cut to a
token budget:

```text
storage/sender.py:
  12: class TransferFailed(Exception)
  16: def send_file(bucket: str, path: str) -> int
  24: def send_part(bucket: str, path: str, number: int, part: bytes) -> dict
transport/http.py:
  10: def put(url: str, body: bytes, checksum: str='') -> dict
  21: def retry_delay_header(headers: dict) -> float | None
…
```

- **Small repositories** (up to 300 source files, with a map of about 1,500 tokens or less) get the
  whole map in the model's instructions.
- **Larger ones** get a `repo_map` tool instead, which the model calls when it needs the overview,
  or for one folder at a time.

Python is read with Python's own parser; TypeScript, JavaScript, Go, Rust, Java and Kotlin by their
declarations. Files git ignores, `node_modules`, build output and virtual environments are left
out, and tests rank lower. The map needs nothing installed. Turn it off with
`lcode config set repo_map false`.

## Semantic code search

With an embedding model in Ollama, the model can search the code by meaning with the `search_code`
tool: `search_code("where are failed uploads retried")` returns the closest pieces of code with
`file:line` and the first lines of each.

```bash
ollama pull qwen3-embedding:0.6b      # or nomic-embed-text (smaller)
cd ~/code/my-project
lcode index                           # build the index (once; it updates itself after that)
```

`lcode index` cuts the source files into overlapping 40-line pieces, embeds them and keeps the
vectors in `~/.local/state/lcode/index/`. During a session, files that changed are embedded again
before each search; after big changes (many files), `search_code` says the index is out of date
and `lcode index` brings it up to date. `lcode index --status` shows the state.

!!! note "Sharing one GPU"

    An embedding model is small, but on a 12 GB GPU it still pushes a large coding model out of
    memory. So during a session lcode runs the embedding model **on the CPU** (searches take a
    fraction of a second; re-embedding a changed file a second or two). `lcode index` uses the GPU
    by default, since you run it outside a session: it indexes about 25 pieces a second there,
    against 1–2 on the CPU (`lcode index --cpu` keeps the GPU free). The coding model is reloaded at
    the next request.

| Setting | |
|---|---|
| `embed_model` | `auto` (default: the first installed of qwen3-embedding, nomic-embed-text, embeddinggemma, mxbai-embed-large, bge-m3, all-minilm), a model name, or `off` |
| `repo_map` | `true` (default) or `false` |

`search_code` is only offered once the repository has been indexed, and `lcode doctor` shows
whether it is.

## Measured

`lcode bench qwen3.6-35b --tasks find-code,find-concept --rounds 3` on a 12 GB laptop. `find-concept`
asks which function computes the pause before a failed upload is sent again, in a 40-file backend
full of look-alikes (retry policies, job requeue delays, log backoff); the right function's name and
docstring use none of the question's words.

| | find-code | find-concept | Prompt tokens, all 6 runs |
|---|---|---|---|
| Neither | 3.3 steps, 10 s | 4.0 steps, 13 s | 56K |
| Repository map (`--repo-map`) | **2.0 steps, 7 s** | **2.0 steps**, 13 s | **34K** |
| Code search (`--code-search`) | 3.7 steps, 11 s | 4.3 steps, 14 s | 63K |
| Both | 2.0 steps, 7 s | 2.3 steps, 13 s | 40K |

Every run found the right answer. The map halved the steps on these tasks and cut the tokens the
model read by 39%. Code search made no difference here: the model preferred `grep` even when
`search_code` was offered, and on these small repositories grep gets there in a few steps. Where
code search should pay off is in large repositories, where grep returns too much; the index is
there for that, and costs nothing until you run `lcode index`.
