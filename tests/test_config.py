from dataclasses import replace

import pytest

from synology_nas_mcp.config import Settings


def test_http_requires_strong_token():
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN"):
        Settings().validate()
    Settings(auth_token="a" * 32).validate()
    Settings(transport="stdio").validate()


def test_oauth_requires_scoped_https_configuration():
    with pytest.raises(ValueError, match="OAuth requires"):
        Settings(auth_mode="oauth").validate()
    settings = Settings(
        auth_mode="oauth",
        oauth_issuer_url="https://login.example.test/",
        oauth_jwks_url="https://login.example.test/.well-known/jwks.json",
        oauth_resource_url="https://mcp.example.test/mcp",
        oauth_allowed_subjects=("owner-123",),
    )
    settings.validate()
    with pytest.raises(ValueError, match="issuer origin"):
        replace(settings, oauth_jwks_url="https://other.example.test/jwks.json").validate()
    with pytest.raises(ValueError, match="HTTPS URL"):
        replace(settings, oauth_resource_url="http://mcp.example.test/mcp").validate()
    with pytest.raises(ValueError, match="HTTPS URL"):
        replace(settings, oauth_resource_url="https://mcp.example.test/other").validate()
    with pytest.raises(ValueError, match="one scope"):
        replace(settings, oauth_required_scope="nas.read nas.admin").validate()


def test_cloudflare_access_requires_team_and_owner():
    with pytest.raises(ValueError, match="Cloudflare Access requires"):
        Settings(auth_mode="cloudflare-access").validate()
    settings = Settings(
        auth_mode="cloudflare-access",
        cf_access_issuer_url="https://team.cloudflareaccess.com",
        cf_access_audience="a" * 64,
        cf_access_allowed_emails=("owner@example.com",),
    )
    settings.validate()
    with pytest.raises(ValueError, match="Cloudflare Access team domain"):
        replace(settings, cf_access_issuer_url="https://attacker.example").validate()
    with pytest.raises(ValueError, match="HTTPS URL"):
        replace(settings, cf_access_issuer_url="http://team.cloudflareaccess.com").validate()
    with pytest.raises(ValueError, match="HTTPS URL"):
        replace(settings, cf_access_issuer_url="https://team.cloudflareaccess.com/").validate()
    with pytest.raises(ValueError, match="email addresses"):
        replace(settings, cf_access_allowed_emails=("not-an-email",)).validate()


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


def test_status_snapshot_opt_in_requires_safe_path_and_age(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("NAS_STATUS_SNAPSHOT_PATH", "/run/nas-status/status.json")
    assert Settings.from_env().status_max_age_seconds == 600
    for path in ("relative/status.json", "/run/../data/status.json", "/run/status\n.json"):
        with pytest.raises(ValueError, match="NAS_STATUS_SNAPSHOT_PATH"):
            replace(Settings(transport="stdio"), status_snapshot_path=path).validate()
    with pytest.raises(ValueError, match="NAS_STATUS_MAX_AGE_SECONDS"):
        replace(Settings(transport="stdio"), status_max_age_seconds=3601).validate()
