import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest

from lcode.mcp import config as mcp_config
from lcode.mcp.manager import McpManager
from lcode.mcp.server import Server, ToolFailure
from lcode.servers import encord, valohai

# ----------------------------------------------------------------------------- the server framework


def small_server(allow_writes=False) -> Server:
    server = Server("demo", "1.0", "Demo instructions.", allow_writes)

    @server.tool("echo", "Echo text.", {"text": {"type": "string"}}, ["text"])
    def echo(text: str) -> str:
        return text

    @server.tool("fail", "Always fails.")
    def fail() -> str:
        raise ToolFailure("no luck")

    @server.tool("crash", "Has a bug.")
    def crash() -> str:
        return 1 / 0

    @server.tool("delete", "Deletes things.", writes=True)
    def delete() -> str:
        return "deleted"

    return server


def rpc(server, method, params=None, ident=1):
    return server.handle({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})


def test_the_server_speaks_both_eras_of_mcp():
    server = small_server()
    init = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"] == {"name": "demo", "version": "1.0"}
    assert rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-11-25"
    discover = rpc(server, "server/discover")["result"]
    assert "2026-07-28" in discover["supportedVersions"] and discover["instructions"] == "Demo instructions."
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(server, "nope")["error"]["code"] == -32601


def test_tools_results_errors_and_writes():
    server = small_server()
    names = [t["name"] for t in rpc(server, "tools/list")["result"]["tools"]]
    assert names == ["echo", "fail", "crash"]  # write tools need --allow-writes
    call = lambda name, args=None: rpc(server, "tools/call", {"name": name, "arguments": args or {}})["result"]  # noqa: E731
    assert call("echo", {"text": "hi"}) == {"content": [{"type": "text", "text": "hi"}], "isError": False}
    assert call("echo", {})["content"][0]["text"] == "Bad arguments for echo: missing: text"
    assert "unknown argument(s): x" in call("echo", {"text": "a", "x": 1})["content"][0]["text"]
    assert call("fail") == {"content": [{"type": "text", "text": "no luck"}], "isError": True}
    assert call("crash")["content"][0]["text"] == "ZeroDivisionError: division by zero"
    assert "--allow-writes" in call("delete")["content"][0]["text"]
    assert call("delete")["isError"] is True
    writer = small_server(allow_writes=True)
    assert rpc(writer, "tools/call", {"name": "delete", "arguments": {}})["result"]["content"][0]["text"] == "deleted"


def test_the_server_answers_line_by_line():
    server = small_server()
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
    ]
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
    ]


# ----------------------------------------------------------------------------- Valohai


class FakeValohai(BaseHTTPRequestHandler):
    posted: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != "Token secret":
            return self.reply({"detail": "Invalid token."}, 401)
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        base = f"http://{self.headers['Host']}/api/v0/"
        project = {"id": "p1", "name": "churn", "owner": {"username": "ml"}, "execution_count": 2}
        execution = {
            "id": "e12",
            "counter": 12,
            "status": "error",
            "step": "train",
            "duration": 125,
            "ctime": "2026-10-01T09:30:00Z",
            "error_text": "CUDA out of memory",
            "commit": {"identifier": "abc123"},
            "parameters": {"lr": 0.01, "epochs": 5},
            "cumulative_metadata": {"loss": 0.42, "accuracy": 0.91},
            "outputs": [{"name": "model.pt"}],
            "url": base + "executions/e12/",
            "urls": {
                "display": "https://app.valohai.com/p/ml/churn/execution/e12/",
                "stop": base + "executions/e12/stop/",
            },
        }
        routes = {
            "/api/v0/projects/": {"results": [project], "next": None},
            "/api/v0/executions/": {
                "results": [execution, {**execution, "counter": 11, "status": "completed", "error_text": ""}]
            },
            "/api/v0/executions/p1:12/": execution,
            "/api/v0/executions/p1:11/": {
                **execution,
                "counter": 11,
                "parameters": {"lr": 0.1, "epochs": 5},
                "cumulative_metadata": {"loss": 0.5},
            },
            "/api/v0/executions/e12/events/": {
                "events": [
                    {"stream": "stdout", "time": "2026-10-01T09:31:00.5Z", "message": "epoch 1"},
                    {
                        "stream": "stderr",
                        "time": "2026-10-01T09:32:00Z",
                        "message": "RuntimeError: CUDA out of memory\n",
                    },
                ],
                "truncated": True,
                "total": 900,
            },
            "/api/v0/data/": {"results": [{"id": "d1", "name": "model.pt", "size": 2048}]},
            "/api/v0/pipelines/": {"results": [{"counter": 3, "status": "complete", "title": "nightly"}]},
        }
        if url.path == "/api/v0/projects/" and query.get("offset") != "1":  # two pages
            return self.reply({"results": [{"id": "p0", "name": "older"}], "next": base + "projects/?offset=1"})
        if url.path == "/api/v0/executions/" and query.get("project") != "p1":
            return self.reply({"results": []})
        if url.path in routes:
            return self.reply(routes[url.path])
        self.reply({"detail": "Not found."}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeValohai.posted.append((self.path, body))
        if self.path == "/api/v0/executions/":
            return self.reply(
                {"counter": 13, "status": "created", "urls": {"display": "https://app.valohai.com/x/13"}}, 201
            )
        self.reply({})


@pytest.fixture
def valohai_host():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeValohai)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    FakeValohai.posted = []
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(server, name, **arguments):
    result = server.call(name, arguments)
    return result["content"][0]["text"], result["isError"]


def test_valohai_reads_projects_executions_logs_and_outputs(valohai_host):
    server = valohai.build(valohai.Valohai(f"http://{valohai_host}", "secret"))
    assert [t.name for t in server.listed()] == [
        "list_projects", "list_executions", "get_execution", "get_execution_logs", "list_outputs",
        "compare_executions", "list_pipelines",
    ]  # fmt: skip
    text, _ = call(server, "list_projects")
    assert "older" in text and "ml/churn  id=p1  executions=2" in text  # both pages
    text, _ = call(server, "list_executions", project="ml/churn", status="error")
    assert "#12 error  step=train  2m05s" in text and "error: CUDA out of memory" in text
    text, _ = call(server, "get_execution", project="churn", counter=12)
    assert "commit=abc123" in text and '"accuracy": 0.91' in text and "outputs (1): model.pt" in text
    text, _ = call(server, "get_execution_logs", project="churn", counter=12, stream="stderr")
    assert text == "(from the last 2 of 900 lines, stderr only)\n09:32:00 err RuntimeError: CUDA out of memory"
    text, _ = call(server, "get_execution_logs", project="churn", counter=12)
    assert text.startswith("(from the last 2 of 900 lines)\n09:31:00 out epoch 1")
    assert call(server, "list_outputs", project="churn", counter=12)[0] == "model.pt  2048 bytes  datum://d1"
    text, _ = call(server, "compare_executions", project="churn", counters=[12, 11])
    assert text.splitlines()[0] == "execution\tstatus\tlr\taccuracy\tloss" and "(only parameters that differ" in text
    assert "=3 complete  nightly" in call(server, "list_pipelines", project="churn")[0]
    text, error = call(server, "list_executions", project="nope")
    assert error and text.startswith("no project 'nope'; projects:")


def test_valohai_writes_need_allow_writes(valohai_host):
    server = valohai.build(valohai.Valohai(valohai_host, "secret"), allow_writes=True)  # no scheme: https added…
    assert server.tools["start_execution"] in server.listed()
    server = valohai.build(valohai.Valohai(f"http://{valohai_host}", "secret"), allow_writes=True)
    text, _ = call(server, "start_execution", project="churn", step="train", commit="abc123", parameters={"lr": 0.1},
                   inputs={"data": "datum://d1"})  # fmt: skip
    assert text == "Started execution #13 (created): https://app.valohai.com/x/13"
    assert FakeValohai.posted[0] == (
        "/api/v0/executions/",
        {
            "project": "p1",
            "step": "train",
            "commit": "abc123",
            "parameters": {"lr": 0.1},
            "inputs": {"data": ["datum://d1"]},
        },
    )
    assert call(server, "stop_execution", project="churn", counter=12)[0] == "Asked Valohai to stop execution #12."
    assert FakeValohai.posted[1][0] == "/api/v0/executions/e12/stop/"


def test_valohai_explains_a_bad_token(valohai_host):
    server = valohai.build(valohai.Valohai(f"http://{valohai_host}", "wrong"))
    text, error = call(server, "list_projects")
    assert error and "check VALOHAI_TOKEN" in text


def test_lcode_talks_to_its_own_valohai_server(valohai_host, tmp_path):
    cfg = mcp_config.parse(
        "valohai",
        {
            "command": sys.executable,
            "args": ["-m", "lcode.servers.valohai"],
            "env": {"VALOHAI_TOKEN": "secret", "VALOHAI_HOST": f"http://{valohai_host}"},
        },
    )
    manager = McpManager(tmp_path, [cfg], "auto")
    manager.start()
    manager.wait(30)
    try:
        state = manager.servers["valohai"]
        assert state.status == "ready", state.error
        assert "start_execution" not in [t["name"] for t in state.tools]
        tool = next(t for t in state.tools if t["name"] == "list_executions")
        assert "#12 error" in manager.call(state, tool, {"project": "churn"})
    finally:
        manager.close()


# ----------------------------------------------------------------------------- Encord


def fake_encord():
    assigned, priorities = [], []

    def row(title, stage, status):
        labels = SimpleNamespace(
            get_object_instances=lambda: [SimpleNamespace(ontology_item=SimpleNamespace(name="car"))] * 2,
            get_classification_instances=lambda: [
                SimpleNamespace(ontology_item=SimpleNamespace(attributes=[SimpleNamespace(name="weather")]))
            ],
        )
        return SimpleNamespace(
            data_title=title,
            data_hash=f"hash-{title}",
            workflow_graph_node=SimpleNamespace(title=stage),
            annotation_task_status=SimpleNamespace(value=status),
            last_edited_at="2026-10-01 10:00:00",
            initialise_labels=lambda: None,
            to_encord_dict=lambda: {"data_title": title, "objects": ["car", "car"]},
            set_priority=lambda p, title=title: priorities.append((title, p)),
            **vars(labels),
        )

    rows = [
        row("a.jpg", "Annotate", "QUEUED"),
        row("b.jpg", "Annotate", "IN_PROGRESS"),
        row("c.jpg", "Review", "QUEUED"),
    ]

    def list_label_rows_v2(include_workflow_graph_node=True, **filters):
        found = rows
        if "workflow_graph_node_title_eq" in filters:
            found = [r for r in found if r.workflow_graph_node.title == filters["workflow_graph_node_title_eq"]]
        if "data_title_eq" in filters:
            found = [r for r in found if r.data_title == filters["data_title_eq"]]
        if "data_title_like" in filters:
            found = [r for r in found if filters["data_title_like"].strip("%") in r.data_title]
        return found

    stage = SimpleNamespace(
        title="Annotate",
        get_tasks=lambda data_title=None: [SimpleNamespace(assign=lambda email: assigned.append((data_title, email)))],
    )
    project = SimpleNamespace(
        title="Street scenes",
        project_hash="11111111-2222-3333-4444-555555555555",
        description="Cars and weather",
        created_at="2026-09-01",
        list_datasets=lambda: [SimpleNamespace(title="Dashcam", dataset_hash="d-1")],
        ontology_structure=SimpleNamespace(
            objects=[SimpleNamespace(name="car", shape=SimpleNamespace(value="bounding_box"))],
            classifications=[SimpleNamespace(attributes=[SimpleNamespace(name="weather")])],
        ),
        workflow=SimpleNamespace(stages=[stage], get_stage=lambda name: stage if name == "Annotate" else 1 / 0),
        list_label_rows_v2=list_label_rows_v2,
    )

    def list_projects(title_eq=None, title_like=None):
        if title_eq:
            return [project] if title_eq == project.title else []
        return [project] if not title_like or title_like.strip("%").lower() in project.title.lower() else []

    client = SimpleNamespace(
        list_projects=list_projects,
        get_project=lambda h: project,
        get_datasets=lambda title_like=None: [
            {"dataset": SimpleNamespace(title="Dashcam", dataset_hash="d-1", last_edited_at="2026-09-30")}
        ],
    )
    return client, assigned, priorities


def test_encord_reads_projects_progress_tasks_and_labels():
    client, _, _ = fake_encord()
    server = encord.build(client)
    assert "assign_task" not in [t.name for t in server.listed()]
    assert "Street scenes  hash=11111111" in call(server, "list_projects", title_contains="street")[0]
    assert call(server, "list_datasets")[0] == "Dashcam  hash=d-1  edited 2026-09-30"
    text, _ = call(server, "get_project", project="Street scenes")
    assert "datasets: Dashcam (d-1)" in text and "car (bounding_box)" in text and "workflow stages: Annotate" in text
    text, _ = call(server, "workflow_progress", project="11111111-2222-3333-4444-555555555555")
    assert text.splitlines()[:2] == ["3 data units", "Annotate: 2  (QUEUED 1, IN_PROGRESS 1)"]
    text, _ = call(server, "list_tasks", project="Street", stage="Review")
    assert text.startswith("c.jpg  stage=Review  status=QUEUED")
    text, _ = call(server, "get_labels", project="Street scenes", data_title="a.jpg")
    assert "objects: car ×2" in text and "classifications: weather ×1" in text and '"objects": ["car", "car"]' in text
    assert call(server, "get_labels", project="Street scenes", data_title="zzz.jpg")[1] is True
    assert call(server, "list_tasks", project="Nope")[0].startswith("no project 'Nope'")


def test_encord_writes_need_allow_writes():
    client, assigned, priorities = fake_encord()
    server = encord.build(client, allow_writes=True)
    text, _ = call(
        server, "assign_task", project="Street scenes", stage="Annotate", data_title="a.jpg", assignee="x@example.com"
    )
    assert text.startswith("Assigned 1 task(s)") and assigned == [("a.jpg", "x@example.com")]
    assert (
        call(server, "assign_task", project="Street scenes", stage="Nope", data_title="a.jpg", assignee="x")[1] is True
    )
    assert call(server, "set_priority", project="Street scenes", data_title="b.jpg", priority=0.9)[0].endswith(
        "to 0.9."
    )
    assert priorities == [("b.jpg", 0.9)]
    assert call(server, "set_priority", project="Street scenes", data_title="b.jpg", priority=3)[1] is True


def test_the_servers_need_their_credentials(monkeypatch, capsys):
    monkeypatch.delenv("VALOHAI_TOKEN", raising=False)
    monkeypatch.delenv("VH_API_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        valohai.main([])
    assert "VALOHAI_TOKEN" in capsys.readouterr().err
