# Copyright 2025 Google LLC All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OAuth resource-server support for Google Analytics MCP."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse, urlunparse

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_protected_resource_routes,
)
from pydantic import AnyHttpUrl

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_LOGGER = logging.getLogger(__name__)


class AuthMode(str, Enum):
    """Supported HTTP authentication modes."""

    NONE = "none"
    AUTH0 = "auth0"


@dataclass(frozen=True)
class AuthConfig:
    """OAuth resource-server configuration."""

    mode: AuthMode
    issuer: str | None = None
    resource: str | None = None
    required_scope: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether OAuth protection is enabled."""
        return self.mode is AuthMode.AUTH0


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when MCP_AUTH_MODE=auth0")
    return value


def _normalize_url(value: str, name: str, *, issuer: bool) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain query or fragment")
    if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError(f"{name} must use HTTPS outside localhost")

    path = parsed.path
    if issuer:
        path = path.rstrip("/") + "/"
    elif path != "/":
        path = path.rstrip("/")

    return urlunparse(parsed._replace(path=path))


def parse_auth_config(environ: Mapping[str, str]) -> AuthConfig:
    """Parse OAuth configuration from environment values."""
    mode_value = environ.get("MCP_AUTH_MODE", "none").strip().lower()
    try:
        mode = AuthMode(mode_value)
    except ValueError as exc:
        raise ValueError(f"invalid MCP_AUTH_MODE: {mode_value}") from exc

    if mode is AuthMode.NONE:
        return AuthConfig(mode=mode)

    issuer = _normalize_url(
        _required(environ, "MCP_AUTH_ISSUER"),
        "MCP_AUTH_ISSUER",
        issuer=True,
    )
    resource = _normalize_url(
        _required(environ, "MCP_AUTH_RESOURCE"),
        "MCP_AUTH_RESOURCE",
        issuer=False,
    )
    required_scope = _required(environ, "MCP_AUTH_REQUIRED_SCOPE")

    return AuthConfig(
        mode=mode,
        issuer=issuer,
        resource=resource,
        required_scope=required_scope,
    )


def _auth0_value(value: str | None, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} is required in auth0 mode")
    return value


def resource_metadata_url(config: AuthConfig) -> AnyHttpUrl:
    """Return the RFC 9728 metadata URL for the MCP resource."""
    resource = AnyHttpUrl(_auth0_value(config.resource, "resource"))
    return build_resource_metadata_url(resource)


def protected_resource_routes(config: AuthConfig):
    """Create RFC 9728 discovery routes for the MCP resource."""
    resource = AnyHttpUrl(_auth0_value(config.resource, "resource"))
    issuer = AnyHttpUrl(_auth0_value(config.issuer, "issuer"))
    required_scope = _auth0_value(config.required_scope, "required_scope")
    return create_protected_resource_routes(
        resource_url=resource,
        authorization_servers=[issuer],
        scopes_supported=[required_scope],
        resource_name="Google Analytics MCP",
    )


class Auth0TokenVerifier(TokenVerifier):
    """Validate Auth0-issued RS256 access tokens for the MCP resource."""

    def __init__(self, config: AuthConfig, jwks_client=None):
        if not config.enabled:
            raise ValueError("Auth0TokenVerifier requires auth0 mode")
        issuer = _auth0_value(config.issuer, "issuer")
        self._config = config
        self._jwks_client = jwks_client or jwt.PyJWKClient(
            f"{issuer}.well-known/jwks.json"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Validate a Bearer token and return MCP access metadata."""
        try:
            signing_key = await asyncio.to_thread(
                self._jwks_client.get_signing_key_from_jwt,
                token,
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._config.resource,
                issuer=self._config.issuer,
                options={"require": ["iss", "aud", "sub", "exp"]},
            )
        except jwt.PyJWTError as exc:
            _LOGGER.debug(
                "Rejected Auth0 access token: %s",
                exc.__class__.__name__,
            )
            return None

        subject = claims.get("sub")
        expiration = claims.get("exp")
        if not isinstance(subject, str) or not subject:
            _LOGGER.debug("Rejected Auth0 access token: invalid subject")
            return None
        if not isinstance(expiration, int) or isinstance(expiration, bool):
            _LOGGER.debug("Rejected Auth0 access token: invalid expiry")
            return None

        scope_claim = claims.get("scope", "")
        scopes = scope_claim.split() if isinstance(scope_claim, str) else []
        return AccessToken(
            token=token,
            client_id=subject,
            scopes=scopes,
            expires_at=expiration,
            resource=self._config.resource,
        )
