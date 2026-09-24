"""Verify externally issued OAuth access tokens for the public MCP resource."""

import asyncio

import jwt
from jwt import PyJWKClient, PyJWTError
from mcp.server.auth.provider import AccessToken

from synology_nas_mcp.config import Settings


class OAuthTokenVerifier:
    def __init__(self, settings: Settings):
        self.issuer = settings.oauth_issuer_url
        self.resource = settings.oauth_resource_url
        self.allowed_subjects = frozenset(settings.oauth_allowed_subjects)
        self.required_scope = settings.oauth_required_scope
        self.jwks = PyJWKClient(settings.oauth_jwks_url, timeout=5, lifespan=300)

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 16384:
            return None
        return await asyncio.to_thread(self._verify_token, token)

    def _verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except (PyJWTError, OSError, ValueError):
            return None

        subject = claims.get("sub")
        scopes = claims.get("scope")
        if (
            not isinstance(subject, str)
            or subject not in self.allowed_subjects
            or not isinstance(scopes, str)
        ):
            return None
        scope_list = scopes.split()
        if self.required_scope not in scope_list:
            return None
        client_id = claims.get("azp") or claims.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            client_id = subject
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scope_list,
            expires_at=claims["exp"],
            resource=self.resource,
            subject=subject,
            claims={"iss": self.issuer},
        )
