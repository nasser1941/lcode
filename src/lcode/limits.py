"""Context sizes learned to be too large on this machine, from out-of-memory errors.

When a model fails to load because its context doesn't fit in GPU memory, lcode retries with half the
context and records the size that was tried, so later sessions start there instead of failing first.
Delete the file (see `path()`) to let lcode try larger contexts again, e.g. after a GPU upgrade.
"""

from __future__ import annotations

import json
from pathlib import Path

from lcode import config


def path() -> Path:
    return config.STATE_DIR / "limits.json"


def _load() -> dict[str, int]:
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}


def get(model: str) -> int | None:
    return _load().get(model)


def record(model: str, context: int) -> None:
    data = _load()
    data[model] = min(context, data.get(model, context))
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cap(model: str, context: int) -> int:
    limit = get(model)
    return min(context, limit) if limit else context
