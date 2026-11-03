"""lcode's command line: the options of lcode and of each subcommand."""

from __future__ import annotations

import argparse

from lcode import __version__, config
from lcode.config import ConfigError


def compact_tokens(value: str) -> int:
    """`--compact-at`: a token count such as 64k, or 0."""
    try:
        return config.parse_compact_at(value)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcode",
        description="A local-first terminal coding agent powered by open-weight models via Ollama.",
        epilog="Subcommands: lcode setup | models | doctor | config | bench | trace | mcp | index | update "
        "(lcode <subcommand> --help). "
        "Docs: https://nasser1941.github.io/lcode/",
    )
    parser.add_argument("-p", "--prompt", help="run one request non-interactively and exit")
    parser.add_argument(
        "--output",
        choices=["text", "json", "stream-json"],
        help="with -p: text (default), json (one result object) or stream-json (an event per line)",
    )
    parser.add_argument("--max-steps", type=int, metavar="N", help="stop a request after N model steps")
    parser.add_argument("--allowed-tools", metavar="TOOLS", help='only these tools, e.g. "read_file,grep,glob,bash"')
    parser.add_argument("-m", "--model", help="catalog key (see `lcode models`) or any installed Ollama model")
    parser.add_argument("--context", "--ctx", dest="context", help="context window, e.g. 65536, 128k or 1m")
    parser.add_argument("-r", "--repo", default=".", help="working directory (default: current directory)")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last session here")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION",
        help="resume a saved session: pick from a list, or give its number, name or id",
    )
    parser.add_argument(
        "-w",
        "--worktree",
        nargs="?",
        const="",
        metavar="NAME",
        help="work in a git worktree of its own, on branch NAME (new, or continue one); removed if nothing changed",
    )
    parser.add_argument("--plan", action="store_true", help="start in plan mode: agree on a plan before any change")
    parser.add_argument("--mode", choices=config.WORK_MODES, help="what lcode makes: code (default), image or video")
    parser.add_argument("--auto-edit", action="store_true", help="apply file edits without asking")
    parser.add_argument("--yolo", action="store_true", help="never ask for permission (edits and commands)")
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="with -p: let lcode ask escalate_model for help when stuck, without asking you (it costs money)",
    )
    parser.add_argument("--no-think", action="store_true", help="disable model reasoning (faster, less accurate)")
    parser.add_argument("--no-web", action="store_true", help="don't let the model search or fetch web pages")
    parser.add_argument("--no-mcp", action="store_true", help="don't start MCP servers in this session")
    parser.add_argument("--no-memory", action="store_true", help="don't load or save memory notes in this session")
    parser.add_argument("--sandbox", action="store_true", help="run shell commands in a container (Docker or Podman)")
    parser.add_argument("--show-thinking", action="store_true", help="print the model's reasoning as it streams")
    parser.add_argument("-V", "--version", action="version", version=f"lcode {__version__}")
    return parser


def build_subparsers() -> dict[str, argparse.ArgumentParser]:
    subs = {}
    p = argparse.ArgumentParser(prog="lcode setup", description="Download a model and make it lcode's default.")
    p.add_argument("model", nargs="?", help="catalog key or Ollama tag (default: the best fit for this machine)")
    p.add_argument("--context", "--ctx", dest="context", help="context window, e.g. 128k (default: largest that fits)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    subs["setup"] = p
    subs["models"] = argparse.ArgumentParser(prog="lcode models", description="List models and how they fit here.")
    subs["doctor"] = argparse.ArgumentParser(prog="lcode doctor", description="Check the installation.")
    p = argparse.ArgumentParser(
        prog="lcode media", description="Image and video models: list them, with how they fit here, or install one."
    )
    p.add_argument("action", nargs="?", choices=["list", "install"], default="list")
    p.add_argument("model", nargs="?", help="with install: a model key from the list, e.g. z-image-turbo")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before downloading")
    subs["media"] = p
    p = argparse.ArgumentParser(prog="lcode config", description="Show or change settings.")
    p.add_argument("action", nargs="?", choices=["show", "set", "unset", "path"], default="show")
    p.add_argument("key", nargs="?", choices=list(config.SETTINGS))
    p.add_argument("value", nargs="?")
    subs["config"] = p
    p = argparse.ArgumentParser(
        prog="lcode bench",
        description="Score models on small coding tasks on this machine: pass rate, speed and memory.",
    )
    p.add_argument("models", nargs="*", help="catalog keys or Ollama tags (default: the configured model)")
    p.add_argument("--context", "--ctx", dest="context", help="context window (default: 32k, the same everywhere)")
    p.add_argument("--tasks", help="comma-separated task ids to run (default: all; see --list)")
    p.add_argument(
        "--timeout", type=float, help="seconds per task (default: 300 for the quick tasks, 600 for the longer ones)"
    )
    p.add_argument("--json", metavar="FILE", help="also save the results as JSON")
    p.add_argument("--markdown", action="store_true", help="also print a Markdown table for a test report")
    p.add_argument("--no-think", action="store_true", help="run without model reasoning")
    p.add_argument("--keep", action="store_true", help="keep each task's folder and transcript for inspection")
    p.add_argument("-v", "--verbose", action="store_true", help="show the model working, like a normal session")
    p.add_argument("--list", action="store_true", help="list the tasks and exit")
    p.add_argument("--quick", action="store_true", help="only the eight quick tasks, one attempt each (as before 0.57)")
    p.add_argument(
        "--compare",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="compare two --harbor results (lcode-bench.json or its folder): fails if AFTER dropped beyond the noise",
    )
    p.add_argument(
        "--harbor",
        choices=["dev", "held-out"],
        help="public benchmarks instead: a slice of Terminal-Bench 2.0 and SWE-bench Verified, run with Harbor",
    )
    p.add_argument(
        "--attempts",
        type=int,
        help="attempts per task, each in a new folder and conversation (default: 3, for pass@1 and pass^3; "
        "with --session: 1)",
    )
    p.add_argument(
        "--agent",
        choices=["lcode", "claude-code", "opencode", "codex"],
        default="lcode",
        help="with --harbor: the agent to run, for a comparison on the same model (default: lcode)",
    )
    p.add_argument(
        "--docker-bridge",
        action="store_true",
        help="with --harbor: run the tasks on Docker's default network (when Docker can't make networks)",
    )
    p.add_argument(
        "--session",
        action="store_true",
        help="run all tasks in one conversation, to test a long session and its context management",
    )
    p.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="with --session: the tasks this many times in the conversation, for a longer session "
        "(without --session: the same as --attempts)",
    )
    p.add_argument("--repo-map", action="store_true", help="give the model the repository map (off by default)")
    p.add_argument("--code-search", action="store_true", help="index each task's folder and offer semantic code search")
    p.add_argument("--no-prune", action="store_true", help="don't remove old tool output (to compare, with --session)")
    p.add_argument(
        "--review", choices=["auto", "off"], help="review each change before it counts (default: the setting)"
    )
    p.add_argument(
        "--retry",
        action="store_true",
        help='give a failed task one more try, as a user would: "That didn\'t fix it. Look again."',
    )
    p.add_argument(
        "--escalate",
        dest="escalate_model",
        metavar="MODEL",
        help="a stronger model to ask when stuck and on --retry, e.g. ollama/qwen3.8-27b (no questions asked)",
    )
    p.add_argument(
        "--checks",
        metavar="auto|off|COMMAND",
        help="run each task's checks before it counts as done (default: the checks setting)",
    )
    p.add_argument(
        "--compact-at",
        type=compact_tokens,
        metavar="TOKENS",
        help="summarize at this many tokens, e.g. 64k (default: the compact_at setting; 0: only at 85%%)",
    )
    subs["bench"] = p
    p = argparse.ArgumentParser(
        prog="lcode trace",
        description="Requests recorded on this machine (the trace setting): list them, show one step by step, "
        "export one for a bug report with secrets and personal details masked, or replay one with the recorded "
        "answers instead of a model and see where the tool results now differ.",
    )
    p.add_argument("action", nargs="?", default="list", help="list (default), show, export, replay or path")
    p.add_argument("trace", nargs="?", help="a number from the list (1: the newest), a trace id or a trace file")
    p.add_argument("-n", "--limit", type=int, default=20, help="list: how many (default: 20)")
    p.add_argument("-v", "--verbose", action="store_true", help="show: also the model's answers and results")
    p.add_argument(
        "-o", "--output", help="export: the file to write (default: lcode-trace-<id>.jsonl here; - prints it)"
    )
    p.add_argument("--repo", help="replay: in a copy of this folder (default: the recorded folder, at its commit)")
    p.add_argument(
        "--commands", action="store_true", help="replay: run shell commands and the checks again too (in the copy)"
    )
    p.add_argument("--keep", action="store_true", help="replay: keep the copy it ran in")
    subs["trace"] = p
    p = argparse.ArgumentParser(
        prog="lcode index",
        description="Build or refresh the semantic code search index of the repository (an Ollama embedding model).",
    )
    p.add_argument("-r", "--repo", default=".", help="a folder in the repository (default: the current folder)")
    p.add_argument("--cpu", action="store_true", help="embed on the CPU (slower, but keeps the GPU for lcode's model)")
    p.add_argument("--model", help="embedding model to use (default: the embed_model setting)")
    p.add_argument("--status", action="store_true", help="only show the state of the index")
    subs["index"] = p
    from lcode.mcp.commands import build_parser as mcp_parser

    subs["mcp"] = mcp_parser()
    p = argparse.ArgumentParser(
        prog="lcode update",
        description="Update lcode, and Ollama when it runs in Docker (to the newest version on Docker Hub, pinned "
        "to its version tag).",
    )
    p.add_argument("what", nargs="?", choices=["lcode", "ollama"], help="only this one (default: both)")
    p.add_argument("--check", action="store_true", help="only show which versions are out")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before updating")
    subs["update"] = p
    subs["acp"] = argparse.ArgumentParser(
        prog="lcode acp",
        description="Run as the agent inside an editor, over the Agent Client Protocol on stdin and stdout "
        "(Zed and other ACP editors start it for you; see the docs).",
    )
    subs["action"] = argparse.ArgumentParser(
        prog="lcode action",
        description="Run as a GitHub Action on a self-hosted runner: answer @lcode in issues and pull requests, "
        "and review pull requests. Configured through the action's inputs (see the docs).",
    )
    return subs
