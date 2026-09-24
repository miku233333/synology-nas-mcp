"""Environment configuration with explicit opt-in for state-changing tools."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _boolean(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _secret(name: str) -> str:
    value = os.environ.get(name, "")
    filename = os.environ.get(f"{name}_FILE", "")
    if value and filename:
        raise ValueError(f"Set only {name} or {name}_FILE")
    if filename:
        value = Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
    return value


@dataclass(frozen=True)
class Settings:
    data_root: Path = Path("/data")
    transport: str = "streamable-http"
    host: str = "0.0.0.0"
    port: int = 8000
    auth_token: str = field(default="", repr=False)
    allowed_hosts: tuple[str, ...] = (
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "nas-mcp:*",
        "testserver",
    )
    max_file_bytes: int = 2 * 1024 * 1024
    max_text_chars: int = 50_000
    max_search_entries: int = 10_000
    dsm_url: str = ""
    dsm_username: str = ""
    dsm_password: str = field(default="", repr=False)
    dsm_ca_bundle: str = ""
    enable_container_actions: bool = False
    allowed_containers: tuple[str, ...] = ()
    enable_download_actions: bool = False
    download_destination: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            data_root=Path(os.environ.get("NAS_DATA_ROOT", "/data")),
            transport=os.environ.get("MCP_TRANSPORT", "streamable-http"),
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=_integer("MCP_PORT", 8000, 1, 65535),
            auth_token=_secret("MCP_AUTH_TOKEN"),
            allowed_hosts=tuple(
                item.strip()
                for item in os.environ.get(
                    "MCP_ALLOWED_HOSTS",
                    "localhost:*,127.0.0.1:*,[::1]:*,nas-mcp:*",
                ).split(",")
                if item.strip()
            ),
            max_file_bytes=_integer("NAS_MAX_FILE_BYTES", 2097152, 1, 20 * 1024 * 1024),
            max_text_chars=_integer("NAS_MAX_TEXT_CHARS", 50000, 1, 200000),
            max_search_entries=_integer("NAS_MAX_SEARCH_ENTRIES", 10000, 1, 100000),
            dsm_url=os.environ.get("DSM_URL", "").rstrip("/"),
            dsm_username=os.environ.get("DSM_USERNAME", ""),
            dsm_password=_secret("DSM_PASSWORD"),
            dsm_ca_bundle=os.environ.get("DSM_CA_BUNDLE", ""),
            enable_container_actions=_boolean("NAS_ENABLE_CONTAINER_ACTIONS"),
            allowed_containers=tuple(
                item.strip()
                for item in os.environ.get("NAS_ALLOWED_CONTAINERS", "").split(",")
                if item.strip()
            ),
            enable_download_actions=_boolean("NAS_ENABLE_DOWNLOAD_ACTIONS"),
            download_destination=os.environ.get("NAS_DOWNLOAD_DESTINATION", ""),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.transport not in {"stdio", "streamable-http"}:
            raise ValueError("MCP_TRANSPORT must be stdio or streamable-http")
        if self.transport == "streamable-http" and (
            len(self.auth_token) < 32
            or not self.auth_token.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in self.auth_token)
        ):
            raise ValueError("MCP_AUTH_TOKEN must contain at least 32 printable ASCII characters")
        if not self.allowed_hosts or any("/" in host for host in self.allowed_hosts):
            raise ValueError("MCP_ALLOWED_HOSTS must contain hostnames with optional ports")
        dsm_values = (self.dsm_url, self.dsm_username, self.dsm_password)
        if any(dsm_values) and not all(dsm_values):
            raise ValueError("DSM_URL, DSM_USERNAME and DSM_PASSWORD must be set together")
        if self.dsm_url:
            url = urlsplit(self.dsm_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                raise ValueError("DSM_URL must be an HTTPS origin without credentials or a path")
        if self.enable_container_actions and (not self.dsm_url or not self.allowed_containers):
            raise ValueError("Container actions require DSM credentials and NAS_ALLOWED_CONTAINERS")
        if self.enable_download_actions and (not self.dsm_url or not self.download_destination):
            raise ValueError(
                "Download actions require DSM credentials and NAS_DOWNLOAD_DESTINATION"
            )
        if self.download_destination and (
            self.download_destination.startswith("/")
            or "\\" in self.download_destination
            or ".." in self.download_destination.split("/")
            or any(ord(c) < 32 or ord(c) == 127 for c in self.download_destination)
            or self.download_destination.strip("./") == ""
        ):
            raise ValueError("NAS_DOWNLOAD_DESTINATION must be a shared-folder-relative path")
