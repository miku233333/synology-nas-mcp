from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qs

import httpx
import pytest

from synology_nas_mcp.dsm import DSMClient, DSMError, NASManager

API_INFO = {
    "SYNO.API.Auth": {"path": "auth.cgi", "maxVersion": 7},
    "SYNO.Core.System": {"path": "entry.cgi", "maxVersion": 3},
    "SYNO.Docker.Container": {
        "path": "entry.cgi",
        "maxVersion": 1,
    },
    "SYNO.DownloadStation.Task": {
        "path": "DownloadStation/task.cgi",
        "maxVersion": 3,
    },
}


def form(request: httpx.Request) -> dict[str, str]:
    return {key: values[-1] for key, values in parse_qs(request.content.decode()).items()}


@pytest.mark.anyio
async def test_discovery_and_login_keep_secrets_out_of_url() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            if data["method"] == "logout":
                assert data["_sid"] == "secret-sid"
                assert data["SynoToken"] == "secret-token"
                return httpx.Response(200, json={"success": True})
            assert data["account"] == "alice"
            assert data["passwd"] == "very-secret"
            assert data["enable_syno_token"] == "yes"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"sid": "secret-sid", "synotoken": "secret-token"},
                },
            )
        assert data["version"] == "3"
        assert data["_sid"] == "secret-sid"
        assert data["SynoToken"] == "secret-token"
        return httpx.Response(200, json={"success": True, "data": {"model": "DS"}})

    client = DSMClient(
        "https://nas.example:5001",
        "alice",
        "very-secret",
        transport=httpx.MockTransport(handler),
    )
    assert await client.call("SYNO.Core.System", "info") == {"model": "DS"}
    await client.aclose()

    assert all("alice" not in str(request.url) for request in requests)
    assert all("very-secret" not in str(request.url) for request in requests)
    assert all("secret-sid" not in str(request.url) for request in requests)
    assert all("secret-token" not in str(request.url) for request in requests)
    assert requests[0].url.path == "/webapi/query.cgi"
    assert any(form(request).get("method") == "logout" for request in requests)


@pytest.mark.anyio
async def test_expired_session_reauthenticates_and_replays_read_once() -> None:
    counts: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        data = form(request)
        counts[data["api"]] += 1
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            return httpx.Response(
                200,
                json={"success": True, "data": {"sid": f"sid-{counts[data['api']]}"}},
            )
        if counts[data["api"]] == 1:
            return httpx.Response(200, json={"success": False, "error": {"code": 106}})
        return httpx.Response(200, json={"success": True, "data": {"model": "DS923+"}})

    client = DSMClient("https://nas.example", "u", "p", transport=httpx.MockTransport(handler))
    assert await client.call("SYNO.Core.System", "info") == {"model": "DS923+"}
    assert counts["SYNO.API.Auth"] == 2
    assert counts["SYNO.Core.System"] == 2
    await client.aclose()


@pytest.mark.anyio
async def test_direct_integer_session_error_reauthenticates_read() -> None:
    login_count = 0
    read_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, read_count
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            login_count += 1
            return httpx.Response(
                200,
                json={"success": True, "data": {"sid": f"sid-{login_count}"}},
            )
        read_count += 1
        if read_count == 1:
            return httpx.Response(200, json={"success": False, "error": 106})
        return httpx.Response(200, json={"success": True, "data": {"model": "DS923+"}})

    client = DSMClient("https://nas.example", "u", "p", transport=httpx.MockTransport(handler))
    assert await client.call("SYNO.Core.System", "info") == {"model": "DS923+"}
    assert login_count == 2
    assert read_count == 2
    await client.aclose()


@pytest.mark.anyio
async def test_json_request_format_and_synotoken_refresh_and_logout() -> None:
    api_info = {
        "SYNO.API.Auth": {
            "path": "entry.cgi",
            "maxVersion": 7,
            "requestFormat": "JSON",
        },
        "SYNO.Core.System": {
            "path": "entry.cgi",
            "maxVersion": 3,
            "requestFormat": "JSON",
        },
    }
    login_count = 0
    system_count = 0
    logout_seen = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count, logout_seen, system_count
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: api_info[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth" and data["method"] == "login":
            login_count += 1
            assert data["api"] == "SYNO.API.Auth"
            assert data["version"] == "7"
            assert data["method"] == "login"
            assert data["account"] == '"alice"'
            assert data["passwd"] == '"password"'
            assert data["enable_syno_token"] == '"yes"'
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "sid": f"sid-{login_count}",
                        "synotoken": f"token-{login_count}",
                    },
                },
            )
        if data["api"] == "SYNO.API.Auth":
            logout_seen = True
            assert data["method"] == "logout"
            assert data["_sid"] == "sid-2"
            assert data["SynoToken"] == "token-2"
            return httpx.Response(200, json={"success": True})

        system_count += 1
        assert data["api"] == "SYNO.Core.System"
        assert data["version"] == "3"
        assert data["method"] == "info"
        assert data["type"] == '""'
        assert data["_sid"] == f"sid-{system_count}"
        assert data["SynoToken"] == f"token-{system_count}"
        if system_count == 1:
            return httpx.Response(200, json={"success": False, "error": {"code": 106}})
        return httpx.Response(200, json={"success": True, "data": {"model": "DS923+"}})

    client = DSMClient(
        "https://nas.example",
        "alice",
        "password",
        transport=httpx.MockTransport(handler),
    )
    manager = NASManager(client)
    assert await manager.system_info() == {"model": "DS923+"}
    assert login_count == 2
    await client.aclose()
    assert logout_seen


@pytest.mark.anyio
async def test_expired_session_does_not_replay_mutation() -> None:
    counts: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        data = form(request)
        counts[data["api"]] += 1
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            return httpx.Response(200, json={"success": True, "data": {"sid": "sid"}})
        return httpx.Response(200, json={"success": False, "error": {"code": 106}})

    client = DSMClient("https://nas.example", "u", "p", transport=httpx.MockTransport(handler))
    manager = NASManager(client, allowed_containers=("web",), enable_container_actions=True)
    with pytest.raises(DSMError, match="retry explicitly"):
        await manager.control_container("web", "restart")
    assert counts["SYNO.Docker.Container"] == 1
    assert counts["SYNO.API.Auth"] == 1
    await client.aclose()


@pytest.mark.anyio
async def test_client_preserves_per_item_mutation_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            return httpx.Response(200, json={"success": True, "data": {"sid": "sid"}})
        return httpx.Response(
            200,
            json={"success": True, "data": [{"id": "task-1", "error": 404}]},
        )

    client = DSMClient("https://nas.example", "u", "p", transport=httpx.MockTransport(handler))
    result = await client.call("SYNO.DownloadStation.Task", "pause", {"id": "task-1"})
    assert result == {"items": [{"id": "task-1", "error": 404}]}
    await client.aclose()


@pytest.mark.anyio
async def test_logout_failure_is_best_effort() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            if data["method"] == "logout":
                raise httpx.ReadTimeout("logout timeout", request=request)
            return httpx.Response(200, json={"success": True, "data": {"sid": "sid"}})
        return httpx.Response(200, json={"success": True, "data": {"model": "DS"}})

    client = DSMClient("https://nas.example", "u", "p", transport=httpx.MockTransport(handler))
    assert await client.call("SYNO.Core.System", "info") == {"model": "DS"}
    await client.aclose()


@pytest.mark.anyio
async def test_container_mutation_requires_switch_and_exact_allowlist() -> None:
    class NoCallClient:
        async def call(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("DSM must not be called")

    disabled = NASManager(NoCallClient(), allowed_containers=("web",))  # type: ignore[arg-type]
    with pytest.raises(DSMError, match="not permitted"):
        await disabled.control_container("web", "stop")

    enabled = NASManager(NoCallClient(), allowed_containers=("web",), enable_container_actions=True)  # type: ignore[arg-type]
    with pytest.raises(DSMError, match="not permitted"):
        await enabled.control_container("web-prod", "stop")


@pytest.mark.anyio
async def test_download_rules_and_destination_scope() -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeClient:
        async def call(
            self, api: str, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            calls.append((api, method, params))
            if method == "getinfo":
                return {
                    "tasks": [
                        {
                            "id": "task-1",
                            "additional": {"detail": {"destination": "Downloads/safe"}},
                        }
                    ]
                }
            if method in {"pause", "resume"}:
                return {"items": [{"id": "task-1", "error": 0}]}
            return {}

    manager = NASManager(
        FakeClient(),  # type: ignore[arg-type]
        enable_download_actions=True,
        download_destination="Downloads",
    )
    with pytest.raises(DSMError, match="DNS rebinding"):
        await manager.create_download_task("https://example.com/file?token=secret")
    with pytest.raises(DSMError, match="credentials"):
        await manager.create_download_task("https://user:pass@example.com/file")

    valid_magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=file.iso"
    result = await manager.create_download_task(valid_magnet)
    assert result == {"accepted": True, "destination": "Downloads"}
    assert await manager.control_download_task("task-1", "pause") == {
        "task_id": "task-1",
        "action": "pause",
        "accepted": True,
    }
    assert calls[-1][1] == "pause"

    for unsafe_magnet in (
        "magnet:?xt=urn:btih:abc",
        "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=http://127.0.0.1/announce",
        "magnet:?xt=urn:btih:" + "a" * 40 + "&ws=http://169.254.169.254/",
        "magnet:?xt=urn:btih:" + "a" * 40 + "&as=https://example.com/file",
        "magnet:?xt=urn:btih:" + "a" * 40 + "&xs=https://example.com/file",
    ):
        with pytest.raises(DSMError):
            await manager.create_download_task(unsafe_magnet)

    class OutsideClient(FakeClient):
        async def call(
            self, api: str, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            if method == "getinfo":
                return {
                    "tasks": [
                        {
                            "id": "task-2",
                            "additional": {"detail": {"destination": "Other"}},
                        }
                    ]
                }
            raise AssertionError("out-of-scope task must not be mutated")

    outside = NASManager(
        OutsideClient(),  # type: ignore[arg-type]
        enable_download_actions=True,
        download_destination="Downloads",
    )
    with pytest.raises(DSMError, match="outside"):
        await outside.control_download_task("task-2", "resume")


@pytest.mark.anyio
async def test_download_action_requires_success_for_the_exact_task() -> None:
    class FakeClient:
        def __init__(self, getinfo_error: int | None, action_items: list[dict[str, object]]):
            self.getinfo_error = getinfo_error
            self.action_items = action_items

        async def call(
            self, api: str, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            if method == "getinfo":
                task: dict[str, object] = {
                    "id": "task-1",
                    "additional": {"detail": {"destination": "Downloads"}},
                }
                if self.getinfo_error is not None:
                    task["error"] = self.getinfo_error
                return {"tasks": [task]}
            return {"items": self.action_items}

    for items in ([], [{"id": "other", "error": 0}], [{"id": "task-1", "error": 404}]):
        manager = NASManager(
            FakeClient(None, items),  # type: ignore[arg-type]
            enable_download_actions=True,
            download_destination="Downloads",
        )
        with pytest.raises(DSMError, match="not confirmed"):
            await manager.control_download_task("task-1", "pause")

    manager = NASManager(
        FakeClient(401, [{"id": "task-1", "error": 0}]),  # type: ignore[arg-type]
        enable_download_actions=True,
        download_destination="Downloads",
    )
    with pytest.raises(DSMError, match="details"):
        await manager.control_download_task("task-1", "resume")


@pytest.mark.anyio
async def test_read_results_drop_secret_fields_and_download_source() -> None:
    class FakeClient:
        async def call(
            self, api: str, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            if api == "SYNO.Docker.Container":
                return {
                    "containers": [
                        {
                            "name": "web",
                            "image": "nginx:latest",
                            "status": "running",
                            "env": ["PASSWORD=hunter2"],
                            "memory": 123,
                        }
                    ]
                }
            return {
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "https://user:pass@example.com/file.zip?token=secret",
                        "status": "downloading",
                        "size": 100,
                        "username": "source-user",
                        "additional": {
                            "detail": {
                                "destination": "Downloads",
                                "uri": "https://example.com/file?token=secret",
                            },
                            "transfer": {"size_downloaded": 25},
                        },
                    }
                ]
            }

    manager = NASManager(FakeClient())  # type: ignore[arg-type]
    rendered = repr(await manager.list_containers()) + repr(await manager.list_download_tasks())
    assert "PASSWORD" not in rendered
    assert "hunter2" not in rendered
    assert "token" not in rendered
    assert "source-user" not in rendered
    assert "user:pass" not in rendered
    assert "file.zip" in rendered


@pytest.mark.anyio
async def test_metrics_omit_missing_or_non_scalar_values_and_cap_arrays() -> None:
    class FakeClient:
        async def call(
            self, api: str, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            if api == "SYNO.Core.System":
                return {
                    "model": "DS923+",
                    "serial": "should-not-leak",
                    "ram_size": {"env": "SECRET"},
                }
            if api == "SYNO.Core.System.Utilization":
                return {
                    "cpu": {"user_load": 5, "system_load": 2},
                    "memory": {"real_usage": {"env": "SECRET"}},
                    "network": [{"device": f"eth{index}"} for index in range(250)],
                }
            if api == "SYNO.Storage.CS.Storage":
                return {"volumes": [{"id": "volume_1", "size": {"total": 100}}]}
            if api == "SYNO.DownloadStation.Task":
                return {
                    "tasks": [{"id": f"task-{index}", "additional": {}} for index in range(250)]
                }
            raise AssertionError("unexpected API")

    manager = NASManager(FakeClient())  # type: ignore[arg-type]
    system = await manager.system_info()
    usage = await manager.resource_usage()
    storage = await manager.storage_info()
    downloads = await manager.list_download_tasks()
    assert system == {"model": "DS923+"}
    assert "total_percent" not in usage["cpu"]
    assert "used_percent" not in usage["memory"]
    assert len(usage["network"]) == 200
    assert storage["volumes"] == [{"name": "volume_1", "total_bytes": 100}]
    assert len(downloads["tasks"]) == 200
    assert "progress_percent" not in downloads["tasks"][0]
    assert "SECRET" not in repr((system, usage, storage, downloads))


@pytest.mark.anyio
async def test_mutation_timeout_is_not_replayed() -> None:
    mutation_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation_calls
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            return httpx.Response(200, json={"success": True, "data": {"sid": "sid"}})
        mutation_calls += 1
        raise httpx.ReadTimeout("private timeout detail", request=request)

    client = DSMClient("https://nas.example", "u", "p", transport=httpx.MockTransport(handler))
    manager = NASManager(client, allowed_containers=("web",), enable_container_actions=True)
    with pytest.raises(DSMError, match="^DSM request timed out$"):
        await manager.control_container("web", "restart")
    assert mutation_calls == 1
    await client.aclose()


@pytest.mark.anyio
async def test_http_error_does_not_expose_url_or_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data = form(request)
        if data["api"] == "SYNO.API.Info":
            queried = data["query"].split(",")
            return httpx.Response(
                200,
                json={"success": True, "data": {key: API_INFO[key] for key in queried}},
            )
        if data["api"] == "SYNO.API.Auth":
            return httpx.Response(200, json={"success": True, "data": {"sid": "hidden-sid"}})
        return httpx.Response(503, text="backend contains hidden-password")

    client = DSMClient(
        "https://private-nas.example",
        "hidden-user",
        "hidden-password",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(DSMError) as caught:
        await client.call("SYNO.Core.System", "info")
    message = str(caught.value)
    assert message == "DSM request failed"
    for secret in ("private-nas.example", "hidden-user", "hidden-password", "hidden-sid"):
        assert secret not in message
    await client.aclose()
