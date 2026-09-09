"""Actual JSON-RPC stdio exchange with no socket connections in either process."""

import json
import select
import socket
import subprocess
import sys

import pytest


def test_mcp_stdio_registration_and_unknown_mutation(tmp_path):
    # asyncio worker completion needs a local self-pipe socket, never a remote connection.
    left, right = socket.socketpair()
    try:
        try:
            left.send(b"probe")
        except PermissionError:
            pytest.skip("Sandbox forbids local event-loop wakeup IPC; stdio process test needs it")
    finally:
        left.close()
        right.close()
    program = f"""
import socket

def denied(*args, **kwargs):
    raise AssertionError("Network is forbidden")
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
from pathlib import Path
from pykbroker.mcp.server import create_server
create_server(limits_path=Path({str(tmp_path / "absent.toml")!r})).run(transport="stdio")
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:

        def exchange(message, expect_response=True):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if expect_response:
                ready, _, _ = select.select([process.stdout], [], [], 10)
                assert ready, "MCP stdio response timed out"
                line = process.stdout.readline()
                assert line, f"MCP exited: {process.poll()}"
                return json.loads(line)
            return None

        initialized = exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "offline-test", "version": "1"},
                },
            }
        )
        assert "result" in initialized
        exchange({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        tools = exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in tools["result"]["tools"]} == {
            "get_quote",
            "get_balance",
            "get_positions",
            "list_orders",
            "get_rules",
        }
        denied = exchange(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "place_order",
                    "arguments": {},
                },
            }
        )
        assert denied.get("error") or denied.get("result", {}).get("isError")
    finally:
        process.terminate()
        process.communicate(timeout=5)
