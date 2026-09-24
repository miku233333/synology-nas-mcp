from dataclasses import replace

import pytest

from synology_nas_mcp.config import Settings


def test_http_requires_strong_token():
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN"):
        Settings().validate()
    Settings(auth_token="a" * 32).validate()
    Settings(transport="stdio").validate()


def test_management_actions_require_scopes():
    settings = Settings(
        transport="stdio",
        dsm_url="https://nas.example:5001",
        dsm_username="mcp-user",
        dsm_password="fixture-password",
    )
    with pytest.raises(ValueError, match="NAS_ALLOWED_CONTAINERS"):
        replace(settings, enable_container_actions=True).validate()
    with pytest.raises(ValueError, match="NAS_DOWNLOAD_DESTINATION"):
        replace(settings, enable_download_actions=True).validate()


@pytest.mark.parametrize(
    "url",
    [
        "http://nas.example",
        "https://user:secret@nas.example",
        "https://nas.example/?token=secret",
        "https://nas.example/webapi",
    ],
)
def test_rejects_unsafe_dsm_origins(url):
    with pytest.raises(ValueError, match="HTTPS origin"):
        Settings(
            transport="stdio",
            dsm_url=url,
            dsm_username="mcp-user",
            dsm_password="secret",
        ).validate()


def test_secret_files_and_repr(monkeypatch, tmp_path):
    secret = tmp_path / "password"
    secret.write_text("test-secret\n")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("DSM_URL", "https://nas.example")
    monkeypatch.setenv("DSM_USERNAME", "mcp-user")
    monkeypatch.setenv("DSM_PASSWORD_FILE", str(secret))
    settings = Settings.from_env()
    assert settings.dsm_password == "test-secret"
    assert "test-secret" not in repr(settings)
    monkeypatch.setenv("DSM_PASSWORD", "duplicate-secret")
    with pytest.raises(ValueError, match="Set only"):
        Settings.from_env()


def test_invalid_boolean_fails_closed(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("NAS_ENABLE_CONTAINER_ACTIONS", "yes")
    with pytest.raises(ValueError, match="true or false"):
        Settings.from_env()


@pytest.mark.parametrize("path", ["../Downloads", "/volume1/Downloads", "a\\b", ".", "a\nb"])
def test_invalid_download_destination_fails_at_startup(path):
    with pytest.raises(ValueError, match="shared-folder-relative"):
        Settings(transport="stdio", download_destination=path).validate()
