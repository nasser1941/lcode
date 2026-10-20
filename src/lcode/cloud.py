"""Ollama's cloud models: tags like `minimax-m3.1-flash:cloud` run on ollama.com, through the local Ollama.

They look like any installed model to lcode (Ollama lists them, and they chat and call tools), but nothing about
them is local: what lcode sends goes to ollama.com, it's billed per token, and it needs the Ollama signed in to an
ollama.com account (`ollama signin`). So lcode says so where it matters (`lcode doctor`, the start of a session, the
stronger-model panel), skips what only makes sense for a local model (loading it into memory, GPU fit), and turns
Ollama's bare `Unauthorized` into the command that fixes it.
"""

from __future__ import annotations


def is_cloud(model: str | None) -> bool:
    """Whether an Ollama model name is one of Ollama's cloud models: its tag is `cloud` or ends with `-cloud`."""
    name = (model or "").strip().lower()
    if ":" not in name:
        return False
    tag = name.rsplit(":", 1)[1]
    return tag == "cloud" or tag.endswith("-cloud")


def signin_command(host: str) -> str:
    """How to sign the Ollama at `host` in to ollama.com: inside its container when it runs in Docker."""
    try:
        from lcode import update

        container = update.find_container(host) if update.docker_available() else None
    except Exception:  # Docker missing, or not answering: the plain command is still right for most installs
        container = None
    return f"docker exec -it {container.name} ollama signin" if container else "ollama signin"


def unauthorized(model: str, host: str) -> str:
    """The error for a cloud model that Ollama refused: what it means and the command to run."""
    return (
        f"{model} runs in Ollama's cloud (ollama.com), and this Ollama isn't signed in to an ollama.com account. "
        f"Sign it in with `{signin_command(host)}`, then try again."
    )


def notice(model: str) -> str:
    """What to know when a cloud model is lcode's main model."""
    return (
        f"{model} runs in Ollama's cloud: what lcode sends (your requests and the files it reads) goes to "
        "ollama.com, and Ollama bills it per token."
    )
