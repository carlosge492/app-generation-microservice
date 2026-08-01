"""A remote MCP server, so an agent can use this service without integrating it.

Deliberately an HTTP endpoint rather than a published package. A package has to
be found, installed, and kept up to date; a URL is pasted once into Claude or
Cursor and the tools appear. It also means the tool list is generated from the
running service, so it cannot drift from what the API actually does.

The transport is JSON-RPC 2.0 over a single POST, which is the streamable-HTTP
transport's simple case: a request gets one JSON response, a notification gets
none. No SSE, no sessions — nothing here is long-running or stateful, and a
server that claims capabilities it does not implement is worse than a plain one.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# Echoed back to whatever the client asks for when we recognise it. The tool
# half of the protocol has been stable across these, and refusing an unfamiliar
# version is how a working server becomes unusable to a newer client.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Tool:
    """One callable exposed to an agent, with the schema it is described by."""

    def __init__(self, name: str, description: str, schema: dict, run: Callable[..., Any]):
        self.name = name
        self.description = description
        self.schema = schema
        self.run = run

    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict, tools: dict[str, Tool], server_name: str, version: str) -> dict | None:
    """Answer one JSON-RPC message, or None when it is a notification.

    A notification has no `id` and must not be answered — replying to one is a
    protocol violation that some clients treat as a hard failure rather than
    ignoring, which shows up as a server that never finishes initialising.
    """
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if request_id is None:
        return None

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(request_id, {
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
            # Only what is actually implemented. Advertising `resources` or
            # `prompts` here would have clients call methods that do not exist.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": server_name, "version": version},
        })

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": [tool.definition() for tool in tools.values()]})

    if method == "tools/call":
        name = params.get("name")
        tool = tools.get(name)
        if tool is None:
            return _error(request_id, INVALID_PARAMS, f"no such tool: {name}")
        try:
            output = tool.run(**(params.get("arguments") or {}))
        except TypeError as exc:
            return _error(request_id, INVALID_PARAMS, f"bad arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            # Reported as a tool error, not a protocol error: the call was
            # well-formed and the agent should see what went wrong and be able
            # to retry, rather than have the whole session look broken.
            return _result(request_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        return _result(request_id, {
            "content": [{"type": "text", "text": json.dumps(output, indent=2, default=str)}],
            "isError": False,
        })

    return _error(request_id, METHOD_NOT_FOUND, f"unsupported method: {method}")


def handle_payload(payload: Any, tools: dict[str, Tool], server_name: str, version: str):
    """Dispatch one message or a batch, preserving JSON-RPC batch semantics."""
    if isinstance(payload, list):
        answers = [
            answer for answer in
            (handle(message, tools, server_name, version) for message in payload)
            if answer is not None
        ]
        return answers or None
    return handle(payload, tools, server_name, version)
