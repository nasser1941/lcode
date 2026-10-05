"""A tiny language server for tests: Content-Length framed JSON-RPC over stdin/stdout.

It knows `def NAME` lines as definitions, every whole-word occurrence as a reference, and reports an
error for each line containing BROKEN. Like real servers, it asks the client for its configuration.
"""

import json
import re
import sys

documents: dict[str, str] = {}
versions: dict[str, int] = {}


def send(message: dict) -> None:
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def read() -> dict | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))


def word_at(uri: str, position: dict) -> str:
    line = documents[uri].splitlines()[position["line"]]
    for m in re.finditer(r"\w+", line):
        if m.start() <= position["character"] <= m.end():
            return m.group()
    return ""


def locations(pattern: str) -> list[dict]:
    found = []
    for uri, text in documents.items():
        for number, line in enumerate(text.splitlines()):
            for m in re.finditer(pattern, line):
                found.append(
                    {
                        "uri": uri,
                        "range": {
                            "start": {"line": number, "character": m.start()},
                            "end": {"line": number, "character": m.end()},
                        },
                    }
                )
    return found


def publish(uri: str) -> None:
    diagnostics = [
        {
            "range": {"start": {"line": n, "character": 0}, "end": {"line": n, "character": 1}},
            "severity": 1,
            "message": f"broken thing: {line.strip()}",
            "source": "fake",
        }
        for n, line in enumerate(documents[uri].splitlines())
        if "BROKEN" in line
    ]
    send(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": versions[uri], "diagnostics": diagnostics},
        }
    )


while (message := read()) is not None:
    method, params, ident = message.get("method"), message.get("params") or {}, message.get("id")
    if method is None:
        continue  # the client's answer to our configuration request
    result = None
    if method == "initialize":
        result = {"capabilities": {"definitionProvider": True, "referencesProvider": True, "hoverProvider": True}}
    elif method == "initialized":
        send(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "workspace/configuration",
                "params": {"items": [{"section": "fake"}]},
            }
        )
    elif method == "textDocument/didOpen":
        document = params["textDocument"]
        documents[document["uri"]], versions[document["uri"]] = document["text"], document["version"]
        publish(document["uri"])
    elif method == "textDocument/didChange":
        uri = params["textDocument"]["uri"]
        documents[uri], versions[uri] = params["contentChanges"][-1]["text"], params["textDocument"]["version"]
        publish(uri)
    elif method == "textDocument/definition":
        name = word_at(params["textDocument"]["uri"], params["position"])
        found = locations(rf"(?<=def ){name}\b")
        result = found[0] if found else None
    elif method == "textDocument/references":
        result = locations(rf"\b{word_at(params['textDocument']['uri'], params['position'])}\b")
    elif method == "textDocument/hover":
        result = {
            "contents": {
                "kind": "markdown",
                "value": f"```python\n{word_at(params['textDocument']['uri'], params['position'])}: int\n```",
            }
        }
    elif method == "textDocument/documentSymbol":
        text = documents[params["textDocument"]["uri"]]
        result = []
        for m in re.finditer(r"def (\w+)", text):
            line = text[: m.start()].count("\n")
            span = {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 0}}
            local = {"name": "x", "kind": 13, "range": span}
            result.append({"name": m.group(1), "kind": 12, "range": span, "selectionRange": span, "children": [local]})
    elif method == "workspace/symbol":
        result = [
            {"name": loc_name, "kind": 12, "location": loc}
            for loc in locations(rf"(?<=def ){params['query']}\w*")
            for loc_name in [params["query"]]
        ]
    elif method == "shutdown":
        result = None
    elif method == "exit":
        break
    if ident is not None:
        send({"jsonrpc": "2.0", "id": ident, "result": result})
