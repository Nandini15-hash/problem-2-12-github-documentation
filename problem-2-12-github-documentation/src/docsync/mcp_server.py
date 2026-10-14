"""Minimal MCP server (JSON-RPC 2.0 over stdio, no SDK dependency).

Register with Claude Code:
    claude mcp add docsync -- python -m src.docsync.cli mcp --head-dir examples/httpx/0.28.1 --package httpx
Tools: search_api, get_symbol, check_snippet. Read-only by construction.
"""
from __future__ import annotations

import json
import sys
from typing import Dict

from .concierge import Concierge

TOOLS = [
    {"name": "search_api", "description": "Search the library's current API (generated from source, never stale).",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "get_symbol", "description": "Signature, docstring, deprecations and verified examples for one symbol.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "check_snippet", "description": "Check Python code against the current API: unknown/deprecated "
                                             "parameters, removed symbols, with migration hints.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
]


def handle(c: Concierge, req: Dict) -> Dict:
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "docsync", "version": "0.1.0"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        name, args = req["params"]["name"], req["params"].get("arguments", {})
        if name == "search_api":
            text = "\n\n".join(card.render() for card in c.search(args["query"], int(args.get("k", 5)))) or "no match"
        elif name == "get_symbol":
            card = c.get(args["symbol"])
            text = card.render() if card else f"unknown symbol {args['symbol']}"
        elif name == "check_snippet":
            issues = c.check_snippet(args["code"])
            text = json.dumps(issues, indent=1) if issues else "OK: no issues against the current API"
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown tool {name}"}}
        res = {"content": [{"type": "text", "text": text}]}
    elif m and m.startswith("notifications/"):
        return {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {m}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": res}


def serve_stdio(c: Concierge) -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            resp = handle(c, json.loads(line))
        except Exception as e:  # never crash the host IDE
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if resp:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
