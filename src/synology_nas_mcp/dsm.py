from __future__ import annotations

import asyncio
import json
import math
import posixpath
import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx


class DSMError(RuntimeError):
    """A safe, user-facing DSM client error."""


_SESSION_EXPIRED_CODES = {106, 107, 119}
_BTIH_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[A-Z2-7a-z]{32})\Z")
_READ_ONLY_CALLS = {
    ("SYNO.Core.System", "info"),
    ("SYNO.Core.System.Utilization", "get"),
    ("SYNO.Storage.CS.Storage", "load_info"),
    ("SYNO.Docker.Container", "list"),
    ("SYNO.DownloadStation.Task", "list"),
    ("SYNO.DownloadStation.Task", "getinfo"),
}


class DSMClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify: bool | str = True,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) URL without credentials")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self._username = username
        self._password = password
        self._session_id: str | None = None
        self._syno_token: str | None = None
        self._api_cache: dict[str, tuple[str, int, str | None]] = {}
        self._auth_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            verify=verify,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    async def call(
        self,
        api: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not api or not method:
            raise DSMError("DSM API and method are required")
        if params and {"api", "version", "method", "_sid", "SynoToken"}.intersection(params):
            raise DSMError("DSM request parameters contain a reserved field")
        endpoint, version, request_format = await self._get_api(api)
        await self._ensure_login()

        payload = self._build_payload(
            api,
            version,
            method,
            params,
            request_format,
            session_id=self._session_id,
            syno_token=self._syno_token,
        )

        response = await self._post_json(endpoint, payload)
        if response.get("success") is True:
            return self._response_data(response)

        error_code = self._error_code(response)
        if error_code in _SESSION_EXPIRED_CODES:
            self._session_id = None
            self._syno_token = None
            if (api, method) not in _READ_ONLY_CALLS:
                raise DSMError(
                    "DSM session expired during a state-changing request; retry explicitly"
                )
            await self._ensure_login()
            payload = self._build_payload(
                api,
                version,
                method,
                params,
                request_format,
                session_id=self._session_id,
                syno_token=self._syno_token,
            )
            response = await self._post_json(endpoint, payload)
            if response.get("success") is True:
                return self._response_data(response)
            error_code = self._error_code(response)

        raise DSMError(f"DSM API request failed (code {error_code})")

    async def aclose(self) -> None:
        session_id = self._session_id
        syno_token = self._syno_token
        self._session_id = None
        self._syno_token = None
        auth_api = self._api_cache.get("SYNO.API.Auth")
        if session_id is not None and auth_api is not None and not self._client.is_closed:
            endpoint, version, request_format = auth_api
            try:
                async with asyncio.timeout(3.0):
                    await self._post_json(
                        endpoint,
                        self._build_payload(
                            "SYNO.API.Auth",
                            version,
                            "logout",
                            None,
                            request_format,
                            session_id=session_id,
                            syno_token=syno_token,
                        ),
                    )
            except Exception:
                pass
        self._password = ""
        await self._client.aclose()

    async def _get_api(self, api: str) -> tuple[str, int, str | None]:
        cached = self._api_cache.get(api)
        if cached is not None:
            return cached

        query = api if api == "SYNO.API.Auth" else f"SYNO.API.Auth,{api}"
        response = await self._post_json(
            "webapi/query.cgi",
            {
                "api": "SYNO.API.Info",
                "version": 1,
                "method": "query",
                "query": query,
            },
        )
        if response.get("success") is not True:
            raise DSMError(f"DSM API discovery failed (code {self._error_code(response)})")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise DSMError("DSM API discovery returned an invalid response")

        for discovered_api, raw_info in data.items():
            if not isinstance(discovered_api, str) or not isinstance(raw_info, Mapping):
                continue
            path = raw_info.get("path")
            version = raw_info.get("maxVersion")
            request_format = raw_info.get("requestFormat")
            if not isinstance(path, str) or not isinstance(version, int):
                continue
            if request_format is not None and not isinstance(request_format, str):
                continue
            self._api_cache[discovered_api] = (
                self._validate_api_path(path),
                version,
                request_format,
            )

        found = self._api_cache.get(api)
        if found is None:
            raise DSMError("Requested DSM API is unavailable")
        return found

    async def _ensure_login(self) -> None:
        if self._session_id is not None:
            return
        async with self._auth_lock:
            if self._session_id is not None:
                return
            endpoint, version, request_format = await self._get_api("SYNO.API.Auth")
            response = await self._post_json(
                endpoint,
                self._build_payload(
                    "SYNO.API.Auth",
                    version,
                    "login",
                    {
                        "account": self._username,
                        "passwd": self._password,
                        "session": "SynologyNASMCP",
                        "format": "sid",
                        "enable_syno_token": "yes",
                    },
                    request_format,
                ),
            )
            if response.get("success") is not True:
                raise DSMError(f"DSM authentication failed (code {self._error_code(response)})")
            data = response.get("data")
            sid = data.get("sid") if isinstance(data, Mapping) else None
            if not isinstance(sid, str) or not sid:
                raise DSMError("DSM authentication returned an invalid response")
            self._session_id = sid
            syno_token = data.get("synotoken") if isinstance(data, Mapping) else None
            self._syno_token = syno_token if isinstance(syno_token, str) and syno_token else None

    @staticmethod
    def _build_payload(
        api: str,
        version: int,
        method: str,
        params: Mapping[str, Any] | None,
        request_format: str | None,
        *,
        session_id: str | None = None,
        syno_token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"api": api, "version": version, "method": method}
        if session_id is not None:
            payload["_sid"] = session_id
        if syno_token is not None:
            payload["SynoToken"] = syno_token
        if params:
            if request_format == "JSON":
                try:
                    payload.update(
                        {
                            key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                            for key, value in params.items()
                        }
                    )
                except (TypeError, ValueError):
                    raise DSMError("DSM request parameters are not JSON serializable") from None
            else:
                payload.update(params)
        return payload

    async def _post_json(self, endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(endpoint, data=payload)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise DSMError("DSM request timed out") from None
        except httpx.HTTPError:
            raise DSMError("DSM request failed") from None
        try:
            result = response.json()
        except ValueError:
            raise DSMError("DSM returned an invalid response") from None
        if not isinstance(result, dict):
            raise DSMError("DSM returned an invalid response")
        return result

    @staticmethod
    def _validate_api_path(path: str) -> str:
        try:
            parsed = urlsplit(path)
        except ValueError:
            raise DSMError("DSM API discovery returned an unsafe endpoint") from None
        normalized = parsed.path.lstrip("/")
        decoded_parts = unquote(normalized).replace("\\", "/").split("/")
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not normalized
            or ".." in decoded_parts
            or _has_control_chars(unquote(normalized))
        ):
            raise DSMError("DSM API discovery returned an unsafe endpoint")
        return normalized if normalized.startswith("webapi/") else f"webapi/{normalized}"

    @staticmethod
    def _error_code(response: Mapping[str, Any]) -> int | str:
        error = response.get("error")
        if isinstance(error, int) and not isinstance(error, bool):
            return error
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, int) and not isinstance(code, bool):
                return code
        return "unknown"

    @staticmethod
    def _response_data(response: Mapping[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if isinstance(data, Mapping):
            return dict(data)
        if isinstance(data, list):
            return {"items": [dict(item) for item in _mapping_list(data)]}
        return {}


class NASManager:
    def __init__(
        self,
        client: DSMClient,
        *,
        allowed_containers: tuple[str, ...] = (),
        enable_container_actions: bool = False,
        enable_download_actions: bool = False,
        download_destination: str | None = None,
    ) -> None:
        self._client = client
        self._allowed_containers = frozenset(allowed_containers)
        self._enable_container_actions = enable_container_actions
        self._enable_download_actions = enable_download_actions
        self._download_destination = (
            self._normalize_destination(download_destination)
            if download_destination is not None
            else None
        )

    async def system_info(self) -> dict[str, Any]:
        info = await self._client.call("SYNO.Core.System", "info", {"type": ""})
        return _compact(
            {
                "model": _scalar(info.get("model")),
                "dsm_version": _scalar(info.get("firmware_ver")),
                "cpu_model": _scalar(info.get("cpu_series")),
                "cpu_cores": _number(info.get("cpu_cores")),
                "memory_mb": _number(info.get("ram_size")),
                "temperature_c": _number(info.get("sys_temp")),
                "temperature_warning": _scalar(info.get("temperature_warning")),
                "uptime": _scalar(info.get("up_time")),
                "time": _scalar(info.get("time")),
            }
        )

    async def resource_usage(self) -> dict[str, Any]:
        data = await self._client.call("SYNO.Core.System.Utilization", "get", {"type": "current"})
        cpu = _mapping(data.get("cpu"))
        memory = _mapping(data.get("memory"))
        cpu_parts = [_number(cpu.get(key)) for key in ("user_load", "system_load", "other_load")]
        cpu_total = sum(cpu_parts) if all(part is not None for part in cpu_parts) else None
        network = [
            _compact(
                {
                    "device": _scalar(item.get("device")),
                    "receive_bytes_per_second": _number(item.get("rx")),
                    "transmit_bytes_per_second": _number(item.get("tx")),
                }
            )
            for item in _mapping_list(data.get("network"))
        ]
        disk_data = _mapping(data.get("disk"))
        disks = [
            _compact(
                {
                    "device": _scalar(item.get("device")),
                    "read_bytes_per_second": _number(item.get("read_byte")),
                    "write_bytes_per_second": _number(item.get("write_byte")),
                    "utilization_percent": _number(item.get("utilization")),
                }
            )
            for item in _mapping_list(disk_data.get("disk"))
        ]
        return {
            "cpu": _compact(
                {
                    "total_percent": cpu_total,
                    "user_percent": _number(cpu.get("user_load")),
                    "system_percent": _number(cpu.get("system_load")),
                    "load_average_1m": _number(cpu.get("1min_load")),
                    "load_average_5m": _number(cpu.get("5min_load")),
                }
            ),
            "memory": _compact(
                {
                    "used_percent": _number(memory.get("real_usage")),
                    "total_kib": _number(memory.get("total_real")),
                    "available_kib": _number(memory.get("avail_real")),
                    "cached_kib": _number(memory.get("cached")),
                }
            ),
            "network": network,
            "disks": disks,
        }

    async def storage_info(self) -> dict[str, Any]:
        data = await self._client.call("SYNO.Storage.CS.Storage", "load_info")
        volumes: list[dict[str, Any]] = []
        for volume in _mapping_list(data.get("volumes")):
            size = _mapping(volume.get("size"))
            total = _number(size.get("total"))
            used = _number(size.get("used"))
            free = max(total - used, 0) if total is not None and used is not None else None
            used_percent = (
                round((used / total) * 100, 1)
                if total is not None and used is not None and total > 0
                else None
            )
            volumes.append(
                _compact(
                    {
                        "name": _scalar(volume.get("id")),
                        "description": _scalar(volume.get("display_name"))
                        or _scalar(volume.get("desc")),
                        "file_system": _scalar(volume.get("fs_type")),
                        "status": _scalar(volume.get("status")),
                        "total_bytes": total,
                        "used_bytes": used,
                        "free_bytes": free,
                        "used_percent": used_percent,
                        "raid_type": _scalar(volume.get("raid_type")),
                    }
                )
            )
        disks = [
            _compact(
                {
                    "slot": _scalar(disk.get("id")),
                    "model": _scalar(disk.get("model")),
                    "vendor": _scalar(disk.get("vendor")),
                    "capacity_bytes": _number(disk.get("size_total")),
                    "temperature_c": _number(disk.get("temp")),
                    "smart_status": _scalar(disk.get("smart_status")),
                    "health": _scalar(disk.get("status")),
                    "bad_sectors": _number(disk.get("num_bad_sector")),
                }
            )
            for disk in _mapping_list(data.get("disks"))
        ]
        return {"volumes": volumes, "disks": disks}

    async def list_containers(self) -> dict[str, Any]:
        data = await self._client.call(
            "SYNO.Docker.Container",
            "list",
            {"offset": 0, "limit": 200, "type": "all"},
        )
        containers = [
            _compact(
                {
                    "name": _scalar(container.get("name")),
                    "image": _scalar(container.get("image")),
                    "status": _scalar(container.get("status")),
                    "running": _container_running(container),
                    "cpu_percent": _number(container.get("cpu")),
                    "memory_bytes": _number(container.get("memory")),
                    "memory_percent": _number(container.get("memory_percent")),
                }
            )
            for container in _mapping_list(data.get("containers"))
        ]
        return {
            "total": _number(data.get("total")) or len(containers),
            "containers": containers,
        }

    async def control_container(
        self, name: str, action: Literal["start", "stop", "restart"]
    ) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise DSMError("Unsupported container action")
        if (
            not self._enable_container_actions
            or not self._allowed_containers
            or name not in self._allowed_containers
        ):
            raise DSMError("Container action is not permitted")
        await self._client.call("SYNO.Docker.Container", action, {"name": name})
        return {"container": name, "action": action, "accepted": True}

    async def list_download_tasks(self) -> dict[str, Any]:
        data = await self._client.call(
            "SYNO.DownloadStation.Task",
            "list",
            {"offset": 0, "limit": 200, "additional": "detail,transfer"},
        )
        tasks = [self._summarize_download(task) for task in _mapping_list(data.get("tasks"))]
        return {"total": _number(data.get("total")) or len(tasks), "tasks": tasks}

    async def create_download_task(self, uri: str) -> dict[str, Any]:
        destination = self._assert_download_actions_enabled()
        self._validate_download_uri(uri)
        await self._client.call(
            "SYNO.DownloadStation.Task",
            "create",
            {"uri": uri, "destination": destination},
        )
        return {"accepted": True, "destination": destination}

    async def control_download_task(
        self, task_id: str, action: Literal["pause", "resume"]
    ) -> dict[str, Any]:
        destination = self._assert_download_actions_enabled()
        if action not in {"pause", "resume"}:
            raise DSMError("Unsupported download action")
        if not task_id or "," in task_id or _has_control_chars(task_id):
            raise DSMError("Invalid download task ID")

        data = await self._client.call(
            "SYNO.DownloadStation.Task",
            "getinfo",
            {"id": task_id, "additional": "detail"},
        )
        tasks = _mapping_list(data.get("tasks"))
        task = next((item for item in tasks if item.get("id") == task_id), None)
        if task is None:
            raise DSMError("Download task was not found")
        if "error" in task and not _download_item_succeeded(task):
            raise DSMError("Download task details could not be retrieved")
        detail = _mapping(_mapping(task.get("additional")).get("detail"))
        task_destination_raw = detail.get("destination")
        if not isinstance(task_destination_raw, str):
            raise DSMError("Download task destination could not be verified")
        task_destination = self._normalize_destination(task_destination_raw)
        if not (task_destination == destination or task_destination.startswith(destination + "/")):
            raise DSMError("Download task is outside the permitted destination")

        result = await self._client.call("SYNO.DownloadStation.Task", action, {"id": task_id})
        action_items = _mapping_list(result.get("items"))
        action_item = next((item for item in action_items if item.get("id") == task_id), None)
        if action_item is None or not _download_item_succeeded(action_item):
            raise DSMError("Download action was not confirmed by DSM")
        return {"task_id": task_id, "action": action, "accepted": True}

    def _assert_download_actions_enabled(self) -> str:
        if not self._enable_download_actions or self._download_destination is None:
            raise DSMError("Download action is not permitted")
        return self._download_destination

    @staticmethod
    def _validate_download_uri(uri: str) -> None:
        if not uri or _has_control_chars(uri):
            raise DSMError("Invalid download URI")
        try:
            parsed = urlsplit(uri)
        except ValueError:
            raise DSMError("Invalid download URI") from None
        if parsed.username is not None or parsed.password is not None:
            raise DSMError("Download URI credentials are not permitted")
        if parsed.scheme in {"http", "https"}:
            raise DSMError(
                "HTTP(S) downloads are disabled because DNS rebinding cannot be reliably "
                "prevented; use a magnet URI"
            )
        if parsed.scheme != "magnet" or parsed.netloc or parsed.path or parsed.fragment:
            raise DSMError("Only magnet download URIs are supported")
        if not parsed.query or re.search(r"%(?![0-9a-fA-F]{2})", parsed.query):
            raise DSMError("Invalid magnet URI")
        try:
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise DSMError("Invalid magnet URI") from None
        if any(_has_control_chars(key) or _has_control_chars(value) for key, value in pairs):
            raise DSMError("Invalid magnet URI")
        if any(key not in {"xt", "dn"} for key, _ in pairs):
            raise DSMError("Magnet URI contains unsupported parameters")
        xt_values = [value for key, value in pairs if key == "xt"]
        dn_values = [value for key, value in pairs if key == "dn"]
        if len(xt_values) != 1 or len(dn_values) > 1:
            raise DSMError("Invalid magnet URI")
        prefix = "urn:btih:"
        xt = xt_values[0]
        if not xt.lower().startswith(prefix) or not _BTIH_PATTERN.fullmatch(xt[len(prefix) :]):
            raise DSMError("Magnet URI must contain a valid BitTorrent info hash")
        if dn_values and (not dn_values[0] or len(dn_values[0]) > 255):
            raise DSMError("Invalid magnet display name")

    @staticmethod
    def _normalize_destination(destination: str) -> str:
        if not destination or _has_control_chars(destination):
            raise DSMError("Invalid download destination")
        relative = destination.replace("\\", "/").lstrip("/")
        if ".." in relative.split("/"):
            raise DSMError("Invalid download destination")
        normalized = posixpath.normpath(relative)
        if normalized in {"", ".", ".."} or normalized.startswith("../"):
            raise DSMError("Invalid download destination")
        return normalized.rstrip("/")

    @staticmethod
    def _summarize_download(task: Mapping[str, Any]) -> dict[str, Any]:
        additional = _mapping(task.get("additional"))
        detail = _mapping(additional.get("detail"))
        transfer = _mapping(additional.get("transfer"))
        size = _number(task.get("size"))
        downloaded = _number(transfer.get("size_downloaded"))
        progress = (
            round((downloaded / size) * 100, 1)
            if size is not None and downloaded is not None and size > 0
            else None
        )
        return _compact(
            {
                "id": _scalar(task.get("id")),
                "title": _safe_download_title(task.get("title")),
                "status": _scalar(task.get("status")),
                "type": _scalar(task.get("type")),
                "size_bytes": size,
                "downloaded_bytes": downloaded,
                "progress_percent": progress,
                "download_bytes_per_second": _number(transfer.get("speed_download")),
                "upload_bytes_per_second": _number(transfer.get("speed_upload")),
                "destination": _scalar(detail.get("destination")),
            }
        )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:200] if isinstance(item, Mapping)]


def _number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scalar(value: object) -> str | int | float | bool | None:
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _container_running(container: Mapping[str, Any]) -> bool | None:
    enabled = container.get("enable_service")
    if isinstance(enabled, bool):
        return enabled
    status = container.get("status")
    return status == "running" if isinstance(status, str) else None


def _download_item_succeeded(item: Mapping[str, Any]) -> bool:
    error = item.get("error")
    return isinstance(error, int) and not isinstance(error, bool) and error == 0


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _safe_download_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    without_query = value.split("?", 1)[0].split("#", 1)[0]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return without_query
    if parsed.scheme.lower() in {"http", "https", "magnet"}:
        title = posixpath.basename(parsed.path.rstrip("/"))
        return title or "[source omitted]"
    return without_query


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
