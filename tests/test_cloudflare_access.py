import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from synology_nas_mcp.config import Settings
from synology_nas_mcp.server import CloudflareAccessAuth, create_app


@pytest.fixture
def access_setup(monkeypatch, tmp_path):
    (tmp_path / "note.txt").write_text("NAS sample", encoding="utf-8")
    settings = Settings(
        data_root=tmp_path,
        auth_mode="cloudflare-access",
        cf_access_issuer_url="https://team.cloudflareaccess.com",
        cf_access_audience="a" * 64,
        cf_access_allowed_emails=("owner@example.com",),
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "access-key", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: {"keys": [public_jwk]})

    def issue(key=private_key, **overrides):
        now = int(time.time())
        claims = {
            "iss": settings.cf_access_issuer_url,
            "aud": [settings.cf_access_audience],
            "exp": now + 300,
            "iat": now - 1,
            "nbf": now - 1,
            "sub": "owner-id",
            "email": "owner@example.com",
            "type": "app",
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "access-key"})

    return settings, issue


def test_access_assertion_allows_mcp_and_strips_credentials(access_setup):
    settings, issue = access_setup
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        response = client.post(
            "/mcp",
            headers={
                "Cf-Access-Jwt-Assertion": issue(),
                "Authorization": "Bearer oauth:opaque-secret",
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
        assert "opaque-secret" not in response.text
        assert (
            client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Cf-Access-Jwt-Assertion": issue()},
            ).status_code
            == 404
        )

    async def probe(request):
        return JSONResponse({"headers": list(request.headers)})

    app = CloudflareAccessAuth(Starlette(routes=[Route("/probe", probe)]), settings)
    with TestClient(app) as client:
        response = client.get(
            "/probe",
            headers={
                "Cf-Access-Jwt-Assertion": issue(),
                "Authorization": "Bearer oauth:opaque-secret",
                "Cookie": "CF_Authorization=private",
            },
        )
        assert response.status_code == 200
        assert "authorization" not in response.json()["headers"]
        assert "cf-access-jwt-assertion" not in response.json()["headers"]
        assert "cookie" not in response.json()["headers"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://other.cloudflareaccess.com"},
        {"aud": ["wrong-app"]},
        {"email": "other@example.com"},
        {"type": "org"},
        {"sub": ""},
        {"exp": 1},
        {"nbf": 4102444800},
        {"iat": 4102444800},
    ],
)
def test_access_rejects_wrong_claims(access_setup, overrides):
    settings, issue = access_setup
    assertion = issue(**overrides)
    with TestClient(create_app(settings)) as client:
        response = client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": assertion}, json={})
        assert response.status_code == 401
        assert assertion not in response.text


def test_access_rejects_missing_duplicate_and_forged_headers(access_setup):
    settings, issue = access_setup
    valid = issue()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = issue(key=other_key)
    with TestClient(create_app(settings)) as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert (
            client.post(
                "/mcp",
                headers=[("Cf-Access-Jwt-Assertion", valid), ("Cf-Access-Jwt-Assertion", valid)],
                json={},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/mcp",
                headers=[
                    ("Cf-Access-Jwt-Assertion", valid),
                    ("Authorization", "Bearer one"),
                    ("Authorization", "Bearer two"),
                ],
                json={},
            ).status_code
            == 401
        )
        response = client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": forged}, json={})
        assert response.status_code == 401
        assert forged not in response.text
