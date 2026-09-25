from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from synology_nas_mcp.config import Settings
from synology_nas_mcp.server import create_app, create_server


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "note.txt").write_text("NAS sample", encoding="utf-8")
    return Settings(data_root=tmp_path, auth_token="test-token-" + "a" * 32)


async def test_tool_visibility_and_annotations(settings):
    tools = await create_server(settings).list_tools()
    assert {tool.name for tool in tools} == {"list_files", "search_files", "read_file"}
    assert all(tool.annotations.read_only_hint for tool in tools)
    dsm = replace(settings, dsm_url="https://nas.example", dsm_username="u", dsm_password="p")
    tools = await create_server(dsm).list_tools()
    assert "list_containers" in {tool.name for tool in tools}
    assert "control_container" not in {tool.name for tool in tools}
    status = replace(settings, status_snapshot_path="/run/nas-status/status.json")
    tools = {tool.name: tool for tool in await create_server(status).list_tools()}
    assert tools["get_nas_status"].annotations.read_only_hint is True
    enabled = replace(dsm, enable_container_actions=True, allowed_containers=("demo",))
    tools = {tool.name: tool for tool in await create_server(enabled).list_tools()}
    assert tools["control_container"].annotations.read_only_hint is False
    assert tools["control_container"].annotations.destructive_hint is True


def test_http_auth_host_origin_and_real_mcp_roundtrip(settings):
    headers = {
        "Authorization": f"Bearer {settings.auth_token}",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        for method in ("GET", "POST", "DELETE"):
            assert client.request(method, "/mcp").status_code == 401
        bad = {**headers, "Host": "attacker.example"}
        assert client.post("/mcp", headers=bad, json={}).status_code == 421
        bad = {**headers, "Origin": "https://attacker.example"}
        assert client.post("/mcp", headers=bad, json={}).status_code == 403
        init = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert init.status_code == 200, init.text
        negotiated = init.json()["result"]["protocolVersion"]
        headers["MCP-Protocol-Version"] = negotiated
        result = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "note.txt"}},
            },
        )
        assert result.status_code == 200, result.text
        assert not result.json()["result"].get("isError", False)
        assert "NAS sample" in result.text
        rejected = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "../outside.txt"}},
            },
        )
        assert rejected.json()["result"]["isError"] is True
        assert str(settings.data_root) not in rejected.text
