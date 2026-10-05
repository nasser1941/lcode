"""An MCP server for Valohai: projects, executions, their logs, metrics and outputs, and pipelines.

Valohai has a REST API but no MCP server, so lcode ships this one. It needs an API token
(VALOHAI_TOKEN, or VH_API_TOKEN as in Valohai's docs; create one under My Profile > Authentication)
and talks to https://app.valohai.com unless VALOHAI_HOST names a self-hosted installation.
It's read-only unless started with --allow-writes, which adds starting and stopping executions.

Run: lcode-mcp-valohai [--allow-writes]   (lcode mcp add valohai sets it up)
"""

from __future__ import annotations

import argparse
import json
import os

import requests

from lcode import __version__
from lcode.mcp.server import Server, ToolFailure

DEFAULT_HOST = "https://app.valohai.com"
TIMEOUT = 60


class Valohai:
    def __init__(self, host: str, token: str):
        host = host.strip().rstrip("/")
        self.base = ("" if "://" in host else "https://") + host + "/api/v0/"
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})

    def request(self, method: str, path: str, **kwargs) -> dict:
        url = path if path.startswith("http") else self.base + path.lstrip("/")
        try:
            r = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise ToolFailure(f"can't reach Valohai: {e}") from e
        if r.status_code in (401, 403):
            raise ToolFailure("Valohai refused the token: check VALOHAI_TOKEN (My Profile > Authentication)")
        if r.status_code == 404:
            raise ToolFailure(f"not found in Valohai: {path}")
        if r.status_code >= 400:
            raise ToolFailure(f"Valohai error {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def all(self, path: str, params: dict, limit: int) -> list[dict]:
        """Up to `limit` results of a paginated listing."""
        found: list[dict] = []
        data = self.request("GET", path, params={**params, "limit": min(limit, 100)})
        while True:
            found += data.get("results") or []
            if len(found) >= limit or not data.get("next"):
                return found[:limit]
            data = self.request("GET", data["next"])

    def project(self, name_or_id: str) -> dict:
        """A project by id, name or owner/name."""
        wanted = name_or_id.strip().lower()
        projects = self.all("projects/", {}, 1000)
        for p in projects:
            owner = (p.get("owner") or {}).get("username", "")
            if wanted in (
                str(p.get("id", "")).lower(),
                str(p.get("name", "")).lower(),
                f"{owner}/{p.get('name')}".lower(),
            ):
                return p
        names = ", ".join(sorted(str(p.get("name")) for p in projects)[:30])
        raise ToolFailure(f"no project {name_or_id!r}; projects: {names or 'none'}")

    def execution(self, project: str, counter: int) -> dict:
        p = self.project(project)
        return self.request("GET", f"executions/{p['id']}:{int(counter)}/", params={"exclude": "events"})


def _when(value: str | None) -> str:
    return (value or "")[:16].replace("T", " ")


def _stream(event: dict) -> str:
    return str(event.get("stream", "")).removeprefix("std")[:3]  # out, err or sta(tus)


def _duration(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def build(api: Valohai, allow_writes: bool = False) -> Server:
    server = Server(
        "valohai",
        __version__,
        "Valohai ML platform: projects, executions (runs of a step from valohai.yaml), their logs, metrics "
        "(metadata) and outputs, and pipelines. Executions are identified by project and counter (#12).",
        allow_writes,
    )
    project_arg = {"type": "string", "description": "Project name, owner/name or id"}
    counter_arg = {"type": "integer", "description": "The execution's counter (the #number in Valohai)"}

    @server.tool(
        "list_projects",
        "List the Valohai projects you can see, with their execution counts.",
        {"name_contains": {"type": "string", "description": "Only projects whose name contains this"}},
    )
    def list_projects(name_contains: str = "") -> str:
        projects = [
            p for p in api.all("projects/", {}, 1000) if name_contains.lower() in str(p.get("name", "")).lower()
        ]
        if not projects:
            return "No projects."
        lines = []
        for p in projects:
            owner = (p.get("owner") or {}).get("username", "")
            name = f"{owner}/{p.get('name')}" if owner else str(p.get("name"))
            lines.append(
                f"{name}  id={p.get('id')}  executions={p.get('execution_count', '?')}"
                f"  last run {_when(p.get('last_execution_ctime')) or 'never'}"
            )
        return "\n".join(lines)

    @server.tool(
        "list_executions",
        "List a project's executions, newest first: counter, status, step, duration, and the error for failed ones.",
        {
            "project": project_arg,
            "status": {"type": "string", "enum": ["queued", "started", "completed", "error", "stopped", "crashed"]},
            "limit": {"type": "integer", "description": "How many (default 20, at most 200)"},
        },
        ["project"],
    )
    def list_executions(project: str, status: str = "", limit: int = 20) -> str:
        p = api.project(project)
        params = {"project": p["id"], "ordering": "-counter"}
        if status:
            params["status"] = status
        executions = api.all("executions/", params, max(1, min(int(limit), 200)))
        if not executions:
            return "No executions."
        lines = []
        for e in executions:
            line = (
                f"#{e.get('counter')} {e.get('status')}  step={e.get('step')}  "
                f"{_duration(e.get('duration'))}  {_when(e.get('ctime'))}"
            )
            if e.get("error_text"):
                line += f"\n    error: {e['error_text'][:300]}"
            lines.append(line)
        return "\n".join(lines)

    @server.tool(
        "get_execution",
        "One execution: status, step, commit, parameters, inputs, latest metrics (metadata), outputs and link.",
        {"project": project_arg, "counter": counter_arg},
        ["project", "counter"],
    )
    def get_execution(project: str, counter: int) -> str:
        e = api.execution(project, counter)
        commit = e.get("commit")
        commit = commit.get("identifier") if isinstance(commit, dict) else commit
        outputs = [o.get("name") for o in e.get("outputs") or [] if isinstance(o, dict)]
        parts = [
            f"#{e.get('counter')} {e.get('status')}  step={e.get('step')}  commit={commit}",
            f"started {_when(e.get('ctime'))}  duration {_duration(e.get('duration'))}",
            f"link: {(e.get('urls') or {}).get('display', '')}",
        ]
        if e.get("error_text"):
            parts.append(f"error: {e['error_text']}")
        for key in ("parameters", "inputs", "cumulative_metadata"):
            if e.get(key):
                label = "metrics (latest values)" if key == "cumulative_metadata" else key
                parts.append(f"{label}: {json.dumps(e[key], indent=1, default=str)}")
        if outputs:
            parts.append(f"outputs ({len(outputs)}): {', '.join(outputs[:50])}")
        return "\n".join(parts)

    @server.tool(
        "get_execution_logs",
        "The last lines of an execution's log (stdout and stderr, with timestamps).",
        {
            "project": project_arg,
            "counter": counter_arg,
            "limit": {"type": "integer", "description": "How many lines (default 200, at most 2000)"},
            "stream": {"type": "string", "enum": ["stdout", "stderr", "status"], "description": "Only this stream"},
        },
        ["project", "counter"],
    )
    def get_execution_logs(project: str, counter: int, limit: int = 200, stream: str = "") -> str:
        e = api.execution(project, counter)
        data = api.request("GET", f"{e['url'].rstrip('/')}/events/", params={"limit": max(1, min(int(limit), 2000))})
        fetched = data.get("events") or []
        events = [ev for ev in fetched if not stream or ev.get("stream") == stream]
        if not events:
            return "No log lines."
        lines = [
            f"{str(ev.get('time', ''))[11:19]} {_stream(ev)} {str(ev.get('message', '')).rstrip()}" for ev in events
        ]
        if data.get("truncated"):
            only = f", {stream} only" if stream else ""
            lines.insert(0, f"(from the last {len(fetched)} of {data.get('total', '?')} lines{only})")
        return "\n".join(lines)

    @server.tool(
        "list_outputs",
        "The files an execution produced (datums), with sizes and their datum:// URIs for use as inputs.",
        {"project": project_arg, "counter": counter_arg},
        ["project", "counter"],
    )
    def list_outputs(project: str, counter: int) -> str:
        e = api.execution(project, counter)
        outputs = api.all("data/", {"output_execution": e["id"]}, 500)
        if not outputs:
            return "No outputs."
        return "\n".join(f"{o.get('name')}  {o.get('size', '?')} bytes  datum://{o.get('id')}" for o in outputs)

    @server.tool(
        "compare_executions",
        "Compare the latest metrics (metadata) and parameters of several executions side by side.",
        {
            "project": project_arg,
            "counters": {"type": "array", "items": {"type": "integer"}, "description": "Execution counters, 2 to 20"},
        },
        ["project", "counters"],
    )
    def compare_executions(project: str, counters: list) -> str:
        if not 1 < len(counters) <= 20:
            raise ToolFailure("give 2 to 20 execution counters")
        rows = []
        for counter in counters:
            e = api.execution(project, int(counter))
            rows.append((counter, e.get("status"), e.get("parameters") or {}, e.get("cumulative_metadata") or {}))
        keys = sorted({k for _, _, _, m in rows for k in m})
        params = sorted({k for _, _, p, _ in rows for k in p if len({json.dumps(r[2].get(k)) for r in rows}) > 1})
        header = ["execution", "status", *params, *keys]
        lines = ["\t".join(header)]
        for counter, status, p, m in rows:
            lines.append(
                "\t".join(
                    [
                        f"#{counter}",
                        str(status),
                        *(str(p.get(k, "")) for k in params),
                        *(str(m.get(k, "")) for k in keys),
                    ]
                )
            )
        return "\n".join(lines) + ("\n(only parameters that differ are shown)" if params else "")

    @server.tool(
        "list_pipelines",
        "List a project's pipelines, newest first, with their status.",
        {"project": project_arg, "limit": {"type": "integer", "description": "How many (default 20)"}},
        ["project"],
    )
    def list_pipelines(project: str, limit: int = 20) -> str:
        p = api.project(project)
        pipelines = api.all("pipelines/", {"project": p["id"], "ordering": "-counter"}, max(1, min(int(limit), 200)))
        if not pipelines:
            return "No pipelines."
        return "\n".join(
            f"={pl.get('counter')} {pl.get('status')}  {pl.get('title') or ''}  {_when(pl.get('ctime'))}"
            for pl in pipelines
        )

    @server.tool(
        "start_execution",
        "Start an execution of a step defined in the project's valohai.yaml.",
        {
            "project": project_arg,
            "step": {"type": "string", "description": "The step's name in valohai.yaml"},
            "commit": {"type": "string", "description": "The commit to run (as Valohai knows it, e.g. a git SHA)"},
            "parameters": {"type": "object", "description": "Parameter values, by name"},
            "inputs": {"type": "object", "description": "Input URLs or datum:// URIs, by input name (lists allowed)"},
            "title": {"type": "string"},
        },
        ["project", "step", "commit"],
        writes=True,
    )
    def start_execution(
        project: str,
        step: str,
        commit: str,
        parameters: dict | None = None,
        inputs: dict | None = None,
        title: str = "",
    ) -> str:
        p = api.project(project)
        payload = {"project": p["id"], "step": step, "commit": commit, "parameters": parameters or {}}
        if inputs:
            payload["inputs"] = {k: v if isinstance(v, list) else [v] for k, v in inputs.items()}
        if title:
            payload["title"] = title
        e = api.request("POST", "executions/", json=payload)
        return f"Started execution #{e.get('counter')} ({e.get('status')}): {(e.get('urls') or {}).get('display', '')}"

    @server.tool(
        "stop_execution",
        "Stop a queued or running execution.",
        {"project": project_arg, "counter": counter_arg},
        ["project", "counter"],
        writes=True,
    )
    def stop_execution(project: str, counter: int) -> str:
        e = api.execution(project, counter)
        stop = (e.get("urls") or {}).get("stop")
        if not stop:
            raise ToolFailure(f"execution #{counter} can't be stopped (status {e.get('status')})")
        api.request("POST", stop)
        return f"Asked Valohai to stop execution #{counter}."

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lcode-mcp-valohai", description="MCP server for Valohai (stdio).")
    parser.add_argument("--allow-writes", action="store_true", help="also offer starting and stopping executions")
    args = parser.parse_args(argv)
    token = os.environ.get("VALOHAI_TOKEN") or os.environ.get("VH_API_TOKEN")
    if not token:
        parser.error("set VALOHAI_TOKEN to a Valohai API token (My Profile > Authentication)")
    api = Valohai(os.environ.get("VALOHAI_HOST") or DEFAULT_HOST, token)
    build(api, args.allow_writes).serve()


if __name__ == "__main__":
    main()
