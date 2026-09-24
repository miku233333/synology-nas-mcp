import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient
from starlette.testclient import TestClient

from synology_nas_mcp.config import Settings
from synology_nas_mcp.oauth import OAuthTokenVerifier
from synology_nas_mcp.server import create_app


@pytest.fixture
def oauth_setup(monkeypatch, tmp_path):
    (tmp_path / "note.txt").write_text("NAS sample", encoding="utf-8")
    settings = Settings(
        data_root=tmp_path,
        auth_mode="oauth",
        oauth_issuer_url="https://login.example.test/",
        oauth_jwks_url="https://login.example.test/.well-known/jwks.json",
        oauth_resource_url="https://mcp.example.test/mcp",
        oauth_allowed_subjects=("owner-123",),
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "owner-key", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: {"keys": [public_jwk]})

    def issue(**overrides):
        now = int(time.time())
        claims = {
            "iss": settings.oauth_issuer_url,
            "aud": settings.oauth_resource_url,
            "sub": "owner-123",
            "azp": "chatgpt-test",
            "scope": "nas.read",
            "iat": now - 1,
            "nbf": now - 1,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "owner-key"})

    return settings, issue


def test_oauth_metadata_challenge_and_mcp_roundtrip(oauth_setup):
    settings, issue = oauth_setup
    with TestClient(create_app(settings)) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == settings.oauth_resource_url
        assert metadata.json()["authorization_servers"] == [settings.oauth_issuer_url]
        root_metadata = client.get("/.well-known/oauth-protected-resource")
        assert root_metadata.status_code == 200
        assert root_metadata.json() == metadata.json()
        challenge = client.post("/mcp", json={})
        assert challenge.status_code == 401
        assert "resource_metadata=" in challenge.headers["WWW-Authenticate"]
        assert settings.oauth_resource_url not in challenge.text

        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {issue()}",
                "Accept": "application/json, text/event-stream",
            },
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
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "Synology NAS MCP"

        invalid_token = issue(sub="other-user")
        rejected = client.post(
            "/mcp", headers={"Authorization": f"Bearer {invalid_token}"}, json={}
        )
        assert rejected.status_code == 401
        assert invalid_token not in rejected.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://other.example.test/"},
        {"aud": "https://other.example.test/mcp"},
        {"sub": "other-user"},
        {"scope": "other.scope"},
        {"exp": 1},
        {"nbf": 4102444800},
    ],
)
def test_oauth_rejects_wrong_claims(oauth_setup, overrides):
    settings, issue = oauth_setup
    verifier = OAuthTokenVerifier(settings)
    assert asyncio.run(verifier.verify_token(issue(**overrides))) is None


def test_oauth_rejects_wrong_signature_and_missing_exp(oauth_setup):
    settings, issue = oauth_setup
    verifier = OAuthTokenVerifier(settings)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "iss": settings.oauth_issuer_url,
            "aud": settings.oauth_resource_url,
            "sub": "owner-123",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "scope": "nas.read",
        },
        other_key,
        algorithm="RS256",
        headers={"kid": "owner-key"},
    )
    assert asyncio.run(verifier.verify_token(forged)) is None
    assert asyncio.run(verifier.verify_token(issue(exp=None))) is None
