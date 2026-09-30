"""A small MCP server over stdio for the tests.

    python fake_mcp_server.py [--era legacy|modern] [--instructions TEXT]

Tools: `add` (two numbers), `echo` (text), `fail` (returns an error result) and `die` (exits the
process). A modern server rejects `initialize`; a legacy one answers it and ignores `_meta`.
"""

import argparse
import json
import sys

TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    },
    {
        "name": "echo",
        "description": "Repeat the text.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "die", "description": "Exits the server.", "inputSchema": {"type": "object"}},
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--era", default="legacy")
    parser.add_argument("--instructions", default="Use add for arithmetic.")
    args = parser.parse_args()
    print("fake server starting", file=sys.stderr, flush=True)
    initialized = False

    def send(message: dict) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if request_id is None:
            if method == "notifications/initialized":
                initialized = True
            continue

        def reply(result: dict, request_id=request_id) -> None:
            send({"jsonrpc": "2.0", "id": request_id, "result": result})

        def error(code: int, text: str, data=None, request_id=request_id) -> None:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text, "data": data}})

        meta = params.get("_meta") or {}
        if args.era == "modern":
            if method == "initialize":
                error(-32601, "initialize isn't supported; this server speaks 2026-07-28")
                continue
            if meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28":
                error(-32022, "Unsupported protocol version", {"supported": ["2026-07-28"]})
                continue
        elif method == "initialize":
            reply(
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "fake", "version": "1.0"},
                    "instructions": args.instructions,
                }
            )
            continue
        elif not initialized:
            error(-32600, "not initialized")
            continue
        if method == "server/discover":
            reply(
                {
                    "resultType": "complete",
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "fake-modern", "version": "2.0"}},
                    "instructions": args.instructions,
                }
            )
        elif method == "tools/list":
            if params.get("cursor") == "2":
                reply({"tools": TOOLS[2:]})
            else:
                reply({"tools": TOOLS[:2], "nextCursor": "2"})  # paginated, to test cursors
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if name == "add":
                total = arguments.get("a", 0) + arguments.get("b", 0)
                reply({"content": [{"type": "text", "text": str(total)}], "resultType": "complete"})
            elif name == "echo":
                reply({"content": [{"type": "text", "text": arguments.get("text", "")}]})
            elif name == "fail":
                reply({"content": [{"type": "text", "text": "the database is down"}], "isError": True})
            elif name == "die":
                print("fake server: exiting on request", file=sys.stderr, flush=True)
                sys.exit(3)
            else:
                error(-32602, f"unknown tool {name}")
        elif method == "ping":
            reply({})
        else:
            error(-32601, f"unknown method {method}")


if __name__ == "__main__":
    main()
