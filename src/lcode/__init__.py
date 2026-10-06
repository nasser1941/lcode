"""lcode — a local-first terminal coding agent powered by open-weight models via Ollama.

From a program: `from lcode import Session` (see lcode.api).
"""

__version__ = "0.22.0"

API = ("Session", "Result", "Options", "SetupError")


def __getattr__(name: str):
    if name in API:  # loaded on first use, so `import lcode` stays fast
        from lcode import api

        return getattr(api, name)
    raise AttributeError(f"module 'lcode' has no attribute {name!r}")
