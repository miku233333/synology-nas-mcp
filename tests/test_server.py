import json
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from starlette.testclient import TestClient

from synology_nas_mcp import server as server_module
from synology_nas_mcp.config import Settings
from synology_nas_mcp.files import FileStore
from synology_nas_mcp.index import SearchIndex
from synology_nas_mcp.server import create_app, create_server


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "note.txt").write_text("NAS sample", encoding="utf-8")
    return Settings(data_root=tmp_path, auth_token="test-token-" + "a" * 32)


async def test_tool_visibility_and_annotations(settings):
    tools = await create_server(settings).list_tools()
    assert {tool.name for tool in tools} == {
        "list_files",
        "search_files",
        "search_content",
        "read_file",
    }
    assert all(tool.annotations.read_only_hint for tool in tools)
    dsm = replace(settings, dsm_url="https://nas.example", dsm_username="u", dsm_password="p")
    tools = await create_server(dsm).list_tools()
    assert "list_containers" in {tool.name for tool in tools}
    assert "control_container" not in {tool.name for tool in tools}
    status = replace(settings, status_snapshot_path="/run/nas-status/status.json")
    tools = {tool.name: tool for tool in await create_server(status).list_tools()}
    assert tools["get_nas_status"].annotations.read_only_hint is True
    for name in (
        "get_power_status",
        "get_ups_status",
        "get_network_status",
        "get_service_status",
    ):
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.destructive_hint is False
    enabled = replace(dsm, enable_container_actions=True, allowed_containers=("demo",))
    tools = {tool.name: tool for tool in await create_server(enabled).list_tools()}
    assert tools["control_container"].annotations.read_only_hint is False
    assert tools["control_container"].annotations.destructive_hint is True


@pytest.fixture
def source_settings(settings):
    return replace(
        settings,
        auth_mode="cloudflare-access",
        cf_access_issuer_url="https://team.cloudflareaccess.com",
        cf_access_audience="a" * 64,
        cf_access_allowed_emails=("owner@example.com",),
        source_origin="https://mcp.example.test",
        allowed_hosts=(*settings.allowed_hosts, "mcp.example.test:*"),
    )


async def test_content_search_and_fetch_return_clickable_sources(source_settings):
    (source_settings.data_root / "report.txt").write_text("Alpha NAS finding", encoding="utf-8")
    (source_settings.data_root / "NAS finding-plan.txt").write_text("Other words", encoding="utf-8")
    server = create_server(source_settings)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["search"].annotations.read_only_hint is True
    assert tools["fetch"].annotations.read_only_hint is True
    assert set(tools["search"].output_schema["properties"]) == {"results"}
    assert tools["search"].output_schema["required"] == ["results"]
    assert set(tools["fetch"].output_schema["properties"]) == {
        "id",
        "title",
        "text",
        "url",
        "metadata",
    }

    result = await server.call_tool("search", {"query": "NAS finding"})
    assert result.is_error is False
    search = json.loads(result.content[0].text)
    assert result.structured_content == search
    assert set(search) == {"results"}
    assert len(search["results"]) == 2
    match = search["results"][0]
    assert match["title"] == "report.txt"
    assert match["url"].startswith("https://mcp.example.test/source/")
    assert {item["title"] for item in search["results"]} == {
        "report.txt",
        "NAS finding-plan.txt",
    }

    scoped = await server.call_tool("search_content", {"query": "NAS finding", "limit": 1})
    assert scoped.is_error is False
    assert json.loads(scoped.content[0].text)["scanned_files"] >= 1

    fetched = await server.call_tool("fetch", {"id": match["id"]})
    assert fetched.is_error is False
    document = json.loads(fetched.content[0].text)
    assert fetched.structured_content == document
    assert document == {
        "id": match["id"],
        "title": "report.txt",
        "text": "Alpha NAS finding",
        "url": match["url"],
        "metadata": {
            "media_type": "text/plain; charset=utf-8",
            "size_bytes": len("Alpha NAS finding"),
            "truncated": False,
        },
    }
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("fetch", {"id": server_module._source_id("../outside.txt")})


async def test_standard_search_skips_unreadable_filename_matches(source_settings, monkeypatch):
    paths = [f"needle-{index:02}.bin" for index in range(25)] + ["needle-readable.txt"]
    for path in paths[:-1]:
        (source_settings.data_root / path).write_bytes(b"\xff")
    (source_settings.data_root / paths[-1]).write_text("Other words", encoding="utf-8")

    def ordered_filename_matches(self, query, path=".", limit=100):
        return {"results": [{"path": item} for item in paths[:limit]]}

    monkeypatch.setattr(server_module.FileStore, "search_files", ordered_filename_matches)
    result = await create_server(source_settings).call_tool("search", {"query": "needle"})
    assert result.structured_content["results"] == [
        {
            "id": server_module._source_id(paths[-1]),
            "title": paths[-1],
            "url": f"{source_settings.source_origin}/source/{server_module._source_id(paths[-1])}",
        }
    ]


async def test_standard_search_skips_unfetchable_long_source_ids(source_settings, monkeypatch):
    long_path = "a" * 6145
    assert len(server_module._source_id("a" * 6144)) == 8192
    with pytest.raises(ValueError, match="maximum citation URL length"):
        server_module._source_id(long_path)
    with pytest.raises(ValueError, match="Invalid source ID"):
        server_module._source_path("a" * 8193)

    def long_content_result(self, query, path=".", limit=20):
        return {
            "results": [
                {"path": long_path, "snippet": "needle"},
                {"path": "note.txt", "snippet": "needle"},
            ]
        }

    monkeypatch.setattr(server_module.FileStore, "search_content", long_content_result)
    result = await create_server(source_settings).call_tool("search", {"query": "needle"})
    assert [item["title"] for item in result.structured_content["results"]] == ["note.txt"]


async def test_standard_search_uses_nas_index_beyond_bounded_scan(source_settings):
    for number in range(70):
        (source_settings.data_root / f"ordinary-{number:03}.txt").write_text(
            "ordinary text", encoding="utf-8"
        )
    (source_settings.data_root / "late.txt").write_text(
        "The indexed deep finding", encoding="utf-8"
    )
    index_path = source_settings.data_root.parent / "private-index" / "index.sqlite3"
    SearchIndex(index_path, FileStore(source_settings.data_root)).rebuild_or_refresh()
    server = create_server(replace(source_settings, index_path=str(index_path)))

    tools = {tool.name for tool in await server.list_tools()}
    assert "get_search_status" in tools
    status = await server.call_tool("get_search_status", {})
    status_data = json.loads(status.content[0].text)
    assert status_data["fresh"] is True
    assert status_data["indexed_files"] == 72

    result = await server.call_tool("search", {"query": "deep finding"})
    assert result.is_error is False
    assert [item["title"] for item in result.structured_content["results"]] == ["late.txt"]


async def test_indexed_search_fails_when_index_is_unavailable(source_settings):
    index_path = source_settings.data_root.parent / "missing-index" / "index.sqlite3"
    server = create_server(replace(source_settings, index_path=str(index_path)))
    with pytest.raises(ToolError, match="Search index unavailable or incomplete"):
        await server.call_tool("search", {"query": "NAS"})


def test_source_route_requires_access_and_escapes_content(source_settings, monkeypatch):
    (source_settings.data_root / "report.txt").write_text(
        "<script>alert('x')</script>", encoding="utf-8"
    )

    async def verify_owner(self, assertion):
        return assertion == "owner"

    monkeypatch.setattr(server_module.CloudflareAccessVerifier, "verify", verify_owner)
    source_path = f"/source/{server_module._source_id('report.txt')}"
    with TestClient(create_app(source_settings)) as client:
        assert client.get(source_path).status_code == 401
        assert (
            client.get(source_path, headers={"cf-access-jwt-assertion": "other"}).status_code == 401
        )
        response = client.get(source_path, headers={"cf-access-jwt-assertion": "owner"})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert "&lt;script&gt;" in response.text
        assert "<script>" not in response.text
        invalid = client.get("/source/invalid!", headers={"cf-access-jwt-assertion": "owner"})
        assert invalid.status_code == 404
        traversal = client.get(
            f"/source/{server_module._source_id('../outside.txt')}",
            headers={"cf-access-jwt-assertion": "owner"},
        )
        assert traversal.status_code == 404


async def test_health_snapshot_tools_return_only_requested_section(settings, monkeypatch):
    captured_at = "2026-10-08T02:00:00+00:00"
    snapshot = {
        "captured_at": captured_at,
        "power": {"data_status": "reported", "running": True, "psu_health": "unknown"},
        "ups": {"data_status": "unavailable", "status": "unknown"},
        "network": {"data_status": "reported", "interfaces": []},
        "services": {"data_status": "reported", "units": []},
    }
    calls = []

    def fake_read(path, max_age_seconds):
        calls.append((path, max_age_seconds))
        return snapshot

    monkeypatch.setattr(server_module, "read_health_snapshot", fake_read)
    status = replace(settings, status_snapshot_path="/run/nas-status/status.json")
    server = create_server(status)
    for name, section in (
        ("get_power_status", "power"),
        ("get_ups_status", "ups"),
        ("get_network_status", "network"),
        ("get_service_status", "services"),
    ):
        result = await server.call_tool(name, {})
        assert result.is_error is False
        assert len(result.content) == 1
        assert json.loads(result.content[0].text) == {
            "captured_at": captured_at,
            section: snapshot[section],
        }
    assert calls == [(Path("/run/nas-status/health.json"), 600)] * 4


def test_http_auth_host_origin_and_real_mcp_roundtrip(settings):
    headers = {
        "Authorization": f"Bearer {settings.auth_token}",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/source/anything", headers=headers).status_code == 404
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
