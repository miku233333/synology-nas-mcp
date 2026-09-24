"""Verify Cloudflare Access identity assertions at the private MCP origin."""

import asyncio

import jwt
from jwt import PyJWKClient, PyJWTError

from synology_nas_mcp.config import Settings


class CloudflareAccessVerifier:
    def __init__(self, settings: Settings):
        self.issuer = settings.cf_access_issuer_url
        self.audience = settings.cf_access_audience
        self.allowed_emails = frozenset(settings.cf_access_allowed_emails)
        self.jwks = PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", timeout=5, lifespan=300)

    async def verify(self, assertion: str) -> bool:
        if len(assertion) > 16384:
            return False
        return await asyncio.to_thread(self._verify, assertion)

    def _verify(self, assertion: str) -> bool:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(assertion)
            claims = jwt.decode(
                assertion,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["iss", "aud", "exp", "iat", "nbf", "sub", "email", "type"]},
            )
        except (PyJWTError, OSError, ValueError):
            return False
        return (
            claims.get("type") == "app"
            and isinstance(claims.get("sub"), str)
            and bool(claims["sub"])
            and isinstance(claims.get("email"), str)
            and claims["email"] in self.allowed_emails
        )
