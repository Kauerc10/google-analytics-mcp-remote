# ChatGPT OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect the remote Google Analytics MCP endpoint with Auth0-issued OAuth access tokens so ChatGPT Developer Mode can discover and invoke the existing read-only Analytics tools from a hosted deployment.

**Architecture:** `feat/chatgpt-oauth` stays layered on `feat/streamable-http-transport`. Auth0 is the OAuth Authorization Server and this repository is only an OAuth Resource Server. The server validates RS256 JWT access tokens locally, publishes RFC 9728 Protected Resource Metadata through the MCP SDK, leaves `/healthz` public, and protects only the exact `/mcp` route.

**Tech Stack:** Python 3.10-3.13, MCP Python SDK `mcp>=1.24.0,<2`, Starlette, Uvicorn, PyJWT with cryptography, Auth0, Google Cloud Run, unittest, Nox, Docker.

**Spec:** `docs/superpowers/specs/2026-08-28-chatgpt-oauth-design.md`

## Global Constraints

- Keep `feat/streamable-http-transport` unchanged.
- Preserve `analytics-mcp`, `google-analytics-mcp`, and the auth-disabled behavior of `analytics-mcp-http`.
- Keep `mcp>=1.24.0,<2` unchanged.
- Keep Python 3.10, 3.11, 3.12, and 3.13 support.
- Auth0 owns authorization, login, PKCE, client registration, token issuance, refresh tokens, and consent.
- This repository must not implement `/authorize`, `/token`, refresh-token storage, login UI, or consent UI.
- The only required MCP application scope is `analytics:read`.
- Invalid authentication returns 401. A valid token without `analytics:read` returns 403 `insufficient_scope`.
- Auth0 Bearer tokens must never be logged or forwarded to Google APIs.
- OAuth mode must fail closed at application startup if issuer, resource, or scope is missing or malformed.
- `/healthz` must not resolve ADC, fetch Auth0 JWKS, or call Google APIs.
- The protected resource metadata route must be public.
- `/mcp` must remain an exact Starlette `Route`, never a `Mount`.
- Production issuer and resource URLs require HTTPS. Localhost and loopback may use HTTP for tests and local development.
- The Cloud Run platform may be opened to unauthenticated invocation only after application-layer OAuth is proven to reject unauthenticated `/mcp` requests.
- Never commit Auth0 secrets, OAuth tokens, Google credentials, service-account keys, generated private keys, or runtime property IDs.

## File Map

- Create `analytics_mcp/auth.py` for auth configuration, JWT verification, and SDK OAuth route helpers.
- Create `tests/auth_test.py` for configuration and JWT verifier tests.
- Modify `analytics_mcp/http_server.py` for auth composition around `/mcp`.
- Modify `tests/http_server_test.py` for OAuth discovery, access control, and authenticated MCP protocol tests.
- Modify `pyproject.toml` to add PyJWT with cryptographic support.
- Create `docs/chatgpt-oauth.md` for the hosted integration guide.
- Modify `README.md` with a short link to the ChatGPT OAuth guide.

---

### Task 1: Add fail-closed OAuth configuration

**Files:**
- Create: `analytics_mcp/auth.py`
- Create: `tests/auth_test.py`
- Modify: `analytics_mcp/http_server.py`
- Modify: `tests/http_server_test.py`

**Interfaces:**
- Produces `AuthMode`, `AuthConfig`, and `parse_auth_config(environ)`.
- `HttpServerConfig` gains `auth: AuthConfig`.

- [ ] **Step 1: Write failing configuration tests**

Create `tests/auth_test.py` with the repository license header and this initial test module:

```python
import unittest

from analytics_mcp import auth


class AuthConfigTest(unittest.TestCase):
    def test_defaults_to_disabled(self):
        config = auth.parse_auth_config({})
        self.assertEqual(config.mode, auth.AuthMode.NONE)
        self.assertFalse(config.enabled)
        self.assertIsNone(config.issuer)
        self.assertIsNone(config.resource)
        self.assertIsNone(config.required_scope)

    def test_auth0_mode_parses_complete_configuration(self):
        config = auth.parse_auth_config(
            {
                "MCP_AUTH_MODE": "auth0",
                "MCP_AUTH_ISSUER": "https://example.us.auth0.com",
                "MCP_AUTH_RESOURCE": "https://analytics.example.com/mcp/",
                "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
            }
        )
        self.assertEqual(config.mode, auth.AuthMode.AUTH0)
        self.assertTrue(config.enabled)
        self.assertEqual(config.issuer, "https://example.us.auth0.com/")
        self.assertEqual(config.resource, "https://analytics.example.com/mcp")
        self.assertEqual(config.required_scope, "analytics:read")

    def test_auth0_mode_requires_issuer(self):
        with self.assertRaisesRegex(ValueError, "MCP_AUTH_ISSUER"):
            auth.parse_auth_config(
                {
                    "MCP_AUTH_MODE": "auth0",
                    "MCP_AUTH_RESOURCE": "https://analytics.example.com/mcp",
                    "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
                }
            )

    def test_auth0_mode_requires_resource(self):
        with self.assertRaisesRegex(ValueError, "MCP_AUTH_RESOURCE"):
            auth.parse_auth_config(
                {
                    "MCP_AUTH_MODE": "auth0",
                    "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                    "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
                }
            )

    def test_auth0_mode_requires_scope(self):
        with self.assertRaisesRegex(ValueError, "MCP_AUTH_REQUIRED_SCOPE"):
            auth.parse_auth_config(
                {
                    "MCP_AUTH_MODE": "auth0",
                    "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                    "MCP_AUTH_RESOURCE": "https://analytics.example.com/mcp",
                }
            )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "MCP_AUTH_MODE"):
            auth.parse_auth_config({"MCP_AUTH_MODE": "magic"})

    def test_production_resource_requires_https(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            auth.parse_auth_config(
                {
                    "MCP_AUTH_MODE": "auth0",
                    "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                    "MCP_AUTH_RESOURCE": "http://analytics.example.com/mcp",
                    "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
                }
            )

    def test_resource_rejects_query_or_fragment(self):
        for resource in (
            "https://analytics.example.com/mcp?debug=1",
            "https://analytics.example.com/mcp#debug",
        ):
            with self.subTest(resource=resource):
                with self.assertRaises(ValueError):
                    auth.parse_auth_config(
                        {
                            "MCP_AUTH_MODE": "auth0",
                            "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                            "MCP_AUTH_RESOURCE": resource,
                            "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
                        }
                    )

    def test_scope_must_be_one_scope_token(self):
        with self.assertRaisesRegex(ValueError, "MCP_AUTH_REQUIRED_SCOPE"):
            auth.parse_auth_config(
                {
                    "MCP_AUTH_MODE": "auth0",
                    "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                    "MCP_AUTH_RESOURCE": "https://analytics.example.com/mcp",
                    "MCP_AUTH_REQUIRED_SCOPE": "analytics:read admin:write",
                }
            )

    def test_localhost_resource_allows_http(self):
        config = auth.parse_auth_config(
            {
                "MCP_AUTH_MODE": "auth0",
                "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                "MCP_AUTH_RESOURCE": "http://localhost:8000/mcp",
                "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
            }
        )
        self.assertEqual(config.resource, "http://localhost:8000/mcp")
```

- [ ] **Step 2: Run the tests to verify RED**

```bash
python -m unittest tests.auth_test.AuthConfigTest -v
```

Expected: import failure because `analytics_mcp.auth` does not exist.

- [ ] **Step 3: Implement the configuration model**

Create `analytics_mcp/auth.py` with the repository license header and this implementation shape:

```python
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1"}


class AuthMode(str, Enum):
    NONE = "none"
    AUTH0 = "auth0"


@dataclass(frozen=True)
class AuthConfig:
    mode: AuthMode
    issuer: str | None = None
    resource: str | None = None
    required_scope: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode is AuthMode.AUTH0


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when MCP_AUTH_MODE=auth0")
    return value


def _normalize_url(name: str, value: str, issuer: bool) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not include credentials, query, or fragment")
    if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HTTP_HOSTS:
        raise ValueError(f"{name} must use HTTPS outside localhost")
    normalized = value.rstrip("/")
    return f"{normalized}/" if issuer else normalized


def parse_auth_config(environ: Mapping[str, str]) -> AuthConfig:
    mode_value = environ.get("MCP_AUTH_MODE", "none").strip().lower()
    if mode_value == AuthMode.NONE.value:
        return AuthConfig(AuthMode.NONE)
    if mode_value != AuthMode.AUTH0.value:
        raise ValueError(f"invalid MCP_AUTH_MODE: {mode_value}")

    issuer = _normalize_url(
        "MCP_AUTH_ISSUER",
        _required(environ, "MCP_AUTH_ISSUER"),
        issuer=True,
    )
    resource = _normalize_url(
        "MCP_AUTH_RESOURCE",
        _required(environ, "MCP_AUTH_RESOURCE"),
        issuer=False,
    )
    required_scope = _required(environ, "MCP_AUTH_REQUIRED_SCOPE")
    if len(required_scope.split()) != 1:
        raise ValueError("MCP_AUTH_REQUIRED_SCOPE must contain exactly one scope")

    return AuthConfig(
        mode=AuthMode.AUTH0,
        issuer=issuer,
        resource=resource,
        required_scope=required_scope,
    )
```

- [ ] **Step 4: Integrate config into HTTP parsing**

Modify `analytics_mcp/http_server.py` to import the module and extend the dataclass:

```python
from analytics_mcp import auth


@dataclass(frozen=True)
class HttpServerConfig:
    host: str
    port: int
    path: str
    auth: auth.AuthConfig
```

Return auth config from `parse_http_config`:

```python
return HttpServerConfig(
    args.host,
    args.port,
    args.path,
    auth.parse_auth_config(env),
)
```

Update the existing default config test in `tests/http_server_test.py`:

```python
self.assertEqual(config.auth.mode, auth.AuthMode.NONE)
```

- [ ] **Step 5: Run focused GREEN tests**

```bash
python -m unittest tests.auth_test.AuthConfigTest -v
python -m unittest tests.http_server_test.HttpServerConfigTest -v
```

Expected: PASS.

- [ ] **Step 6: Format and commit**

```bash
black -l 80 analytics_mcp/auth.py analytics_mcp/http_server.py tests/auth_test.py tests/http_server_test.py
python -m unittest tests.auth_test.AuthConfigTest tests.http_server_test.HttpServerConfigTest -v
git add analytics_mcp/auth.py analytics_mcp/http_server.py tests/auth_test.py tests/http_server_test.py
git commit -m "feat(auth): add fail-closed OAuth configuration"
```

---

### Task 2: Validate Auth0 RS256 access tokens

**Files:**
- Modify: `analytics_mcp/auth.py`
- Modify: `tests/auth_test.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces `Auth0TokenVerifier(config, jwks_client=None)` implementing MCP `TokenVerifier`.
- `verify_token(token)` validates authentication and extracts scopes but does not enforce `analytics:read`; the MCP authorization middleware enforces scope and returns 403.

- [ ] **Step 1: Add PyJWT dependency**

Add to the runtime dependency list in `pyproject.toml`:

```toml
"PyJWT[crypto]>=2.10,<3",
```

Keep the MCP bound unchanged.

- [ ] **Step 2: Add offline RSA test helpers**

Append these helpers to `tests/auth_test.py`:

```python
import time
from types import SimpleNamespace
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken


ISSUER = "https://example.us.auth0.com/"
RESOURCE = "https://analytics.example.com/mcp"


def _auth0_config():
    return auth.AuthConfig(
        mode=auth.AuthMode.AUTH0,
        issuer=ISSUER,
        resource=RESOURCE,
        required_scope="analytics:read",
    )


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "auth0|test-user",
        "iat": now,
        "exp": now + 300,
        "scope": "analytics:read",
    }
    claims.update(overrides)
    return claims


def _sign_rs256(private_key, **overrides):
    return jwt.encode(
        _claims(**overrides),
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


class _StaticJwksClient:
    def __init__(self, public_key):
        self.public_key = public_key
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        return SimpleNamespace(key=self.public_key)
```

- [ ] **Step 3: Write failing verifier tests**

Append this concrete test class:

```python
class Auth0TokenVerifierTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self.jwks = _StaticJwksClient(self.private_key.public_key())
        self.verifier = auth.Auth0TokenVerifier(_auth0_config(), self.jwks)

    async def test_accepts_valid_rs256_token(self):
        token = _sign_rs256(self.private_key)
        result = await self.verifier.verify_token(token)
        self.assertIsInstance(result, AccessToken)
        self.assertEqual(result.client_id, "auth0|test-user")
        self.assertEqual(result.scopes, ["analytics:read"])
        self.assertEqual(result.resource, RESOURCE)

    async def test_rejects_forged_signature(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _sign_rs256(other_key)
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_rejects_expired_token(self):
        token = _sign_rs256(self.private_key, exp=int(time.time()) - 10)
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_rejects_wrong_issuer(self):
        token = _sign_rs256(self.private_key, iss="https://other.auth0.com/")
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_rejects_wrong_audience(self):
        token = _sign_rs256(
            self.private_key,
            aud="https://other.example.com/mcp",
        )
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_rejects_unsupported_algorithm(self):
        token = jwt.encode(
            _claims(),
            "test-secret",
            algorithm="HS256",
            headers={"kid": "test-key"},
        )
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_extracts_space_delimited_scopes(self):
        token = _sign_rs256(
            self.private_key,
            scope="openid offline_access analytics:read",
        )
        result = await self.verifier.verify_token(token)
        self.assertEqual(
            result.scopes,
            ["openid", "offline_access", "analytics:read"],
        )

    async def test_missing_scope_remains_authenticated(self):
        token = _sign_rs256(self.private_key, scope="")
        result = await self.verifier.verify_token(token)
        self.assertIsInstance(result, AccessToken)
        self.assertEqual(result.scopes, [])

    async def test_rejects_missing_subject(self):
        claims = _claims()
        claims.pop("sub")
        token = jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        self.assertIsNone(await self.verifier.verify_token(token))

    async def test_does_not_log_raw_token(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _sign_rs256(other_key)
        with mock.patch.object(auth._LOGGER, "debug") as debug_log:
            self.assertIsNone(await self.verifier.verify_token(token))
        for call in debug_log.call_args_list:
            self.assertNotIn(token, str(call))
```

- [ ] **Step 4: Run verifier tests to verify RED**

```bash
python -m unittest tests.auth_test.Auth0TokenVerifierTest -v
```

Expected: failure because `Auth0TokenVerifier` is not implemented.

- [ ] **Step 5: Implement the verifier**

Add these imports and implementation to `analytics_mcp/auth.py`:

```python
import asyncio
import logging

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

_LOGGER = logging.getLogger(__name__)


class Auth0TokenVerifier(TokenVerifier):
    def __init__(self, config: AuthConfig, jwks_client=None):
        if not config.enabled:
            raise ValueError("Auth0TokenVerifier requires auth0 mode")
        if not config.issuer or not config.resource:
            raise ValueError("Auth0TokenVerifier requires issuer and resource")
        self._config = config
        self._jwks_client = jwks_client or jwt.PyJWKClient(
            f"{config.issuer}.well-known/jwks.json"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
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
            )
            subject = claims.get("sub")
            expires_at = claims.get("exp")
            scope_claim = claims.get("scope", "")
            if not isinstance(subject, str) or not subject:
                return None
            if not isinstance(expires_at, int) or isinstance(expires_at, bool):
                return None
            if not isinstance(scope_claim, str):
                return None
            scopes = [scope for scope in scope_claim.split() if scope]
            return AccessToken(
                token=token,
                client_id=subject,
                scopes=scopes,
                expires_at=expires_at,
                resource=self._config.resource,
            )
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            _LOGGER.debug(
                "Auth0 access token validation failed: %s",
                type(exc).__name__,
            )
            return None
```

- [ ] **Step 6: Prove the default JWKS URL and reusable client**

Add this test:

```python
    def test_default_jwks_client_uses_issuer(self):
        with mock.patch("analytics_mcp.auth.jwt.PyJWKClient") as client_type:
            verifier = auth.Auth0TokenVerifier(_auth0_config())
        client_type.assert_called_once_with(
            "https://example.us.auth0.com/.well-known/jwks.json"
        )
        self.assertIs(verifier._jwks_client, client_type.return_value)
```

The verifier owns one PyJWKClient instance for its lifetime; it must not construct a new JWKS client per request.

- [ ] **Step 7: Run GREEN tests and commit**

```bash
python -m unittest tests.auth_test -v
black -l 80 analytics_mcp/auth.py tests/auth_test.py
git add analytics_mcp/auth.py tests/auth_test.py pyproject.toml
git commit -m "feat(auth): validate Auth0 access tokens"
```

---

### Task 3: Protect `/mcp` and publish RFC 9728 metadata

**Files:**
- Modify: `analytics_mcp/auth.py`
- Modify: `analytics_mcp/http_server.py`
- Modify: `tests/http_server_test.py`

**Interfaces:**
- `create_http_app` gains `auth_config` and injectable `token_verifier` arguments.
- Auth-disabled behavior remains unchanged.
- Auth0 mode publishes metadata and wraps only `/mcp`.

- [ ] **Step 1: Add deterministic HTTP auth test helpers**

Add to `tests/http_server_test.py`:

```python
from analytics_mcp import auth
from mcp.server.auth.provider import AccessToken


AUTH_CONFIG = auth.AuthConfig(
    mode=auth.AuthMode.AUTH0,
    issuer="https://example.us.auth0.com/",
    resource="https://analytics.example.com/mcp",
    required_scope="analytics:read",
)


class _StaticTokenVerifier:
    def __init__(self, access_token):
        self.access_token = access_token
        self.calls = []

    async def verify_token(self, token):
        self.calls.append(token)
        return self.access_token


def _access_token(scopes):
    return AccessToken(
        token="test-token",
        client_id="auth0|test-user",
        scopes=scopes,
        expires_at=None,
        resource="https://analytics.example.com/mcp",
    )
```

- [ ] **Step 2: Write failing HTTP OAuth tests**

Add this test class:

```python
class OAuthHttpApplicationTest(unittest.TestCase):
    def test_healthz_stays_public_when_oauth_enabled(self):
        verifier = _StaticTokenVerifier(None)
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=verifier,
        )
        with TestClient(app) as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        self.assertEqual(verifier.calls, [])

    def test_oauth_metadata_reports_resource_issuer_and_scope(self):
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(None),
        )
        with TestClient(app) as client:
            response = client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["resource"], AUTH_CONFIG.resource)
        self.assertEqual(body["authorization_servers"], [AUTH_CONFIG.issuer])
        self.assertEqual(body["scopes_supported"], ["analytics:read"])

    def test_mcp_without_token_returns_401_with_metadata_challenge(self):
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(None),
        )
        with TestClient(app, follow_redirects=False) as client:
            response = client.post("/mcp")
        self.assertEqual(response.status_code, 401)
        challenge = response.headers["www-authenticate"]
        self.assertIn("Bearer", challenge)
        self.assertIn("resource_metadata=", challenge)
        self.assertNotEqual(response.status_code, 307)

    def test_invalid_token_returns_401(self):
        verifier = _StaticTokenVerifier(None)
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=verifier,
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                headers={"Authorization": "Bearer invalid"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(verifier.calls, ["invalid"])

    def test_valid_token_without_scope_returns_403(self):
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(_access_token([])),
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("insufficient_scope", response.text)

    def test_auth_disabled_does_not_publish_metadata(self):
        app = http_server.create_http_app()
        with TestClient(app) as client:
            response = client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
        self.assertEqual(response.status_code, 404)

    def test_resource_path_must_match_mcp_path(self):
        with self.assertRaisesRegex(ValueError, "resource path"):
            http_server.create_http_app(
                path="/analytics",
                auth_config=AUTH_CONFIG,
                token_verifier=_StaticTokenVerifier(None),
            )
```

The existing auth-disabled `/mcp` no-redirect test remains unchanged and continues to prove compatibility.

- [ ] **Step 3: Run tests to verify RED**

```bash
python -m unittest tests.http_server_test.OAuthHttpApplicationTest -v
```

Expected: failure because OAuth composition does not exist.

- [ ] **Step 4: Add MCP SDK resource metadata helpers**

Add to `analytics_mcp/auth.py`:

```python
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_protected_resource_routes,
)


def resource_metadata_url(config: AuthConfig):
    if not config.enabled or not config.resource:
        raise ValueError("resource metadata requires auth0 mode")
    return build_resource_metadata_url(config.resource)


def protected_resource_routes(config: AuthConfig):
    if (
        not config.enabled
        or not config.resource
        or not config.issuer
        or not config.required_scope
    ):
        raise ValueError("protected resource routes require complete auth0 config")
    return create_protected_resource_routes(
        resource_url=config.resource,
        authorization_servers=[config.issuer],
        scopes_supported=[config.required_scope],
        resource_name="Google Analytics MCP",
    )
```

Use the SDK helpers so RFC 9728 path construction and metadata JSON are not hand-written.

- [ ] **Step 5: Compose middleware around only the exact MCP endpoint**

Modify imports in `analytics_mcp/http_server.py`:

```python
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import TokenVerifier
from starlette.middleware.authentication import AuthenticationMiddleware
```

Modify `create_http_app` signature:

```python
def create_http_app(
    mcp_server: Server = coordinator.app,
    path: str = "/mcp",
    host: str = "127.0.0.1",
    auth_config: auth.AuthConfig | None = None,
    token_verifier: TokenVerifier | None = None,
) -> Starlette:
```

After creating the session manager, build route composition with this logic:

```python
resolved_auth = auth_config or auth.AuthConfig(auth.AuthMode.NONE)
mcp_endpoint = _McpEndpoint(session_manager)
routes = [Route("/healthz", healthz, methods=["GET"])]

if resolved_auth.enabled:
    resource_path = urlparse(resolved_auth.resource).path.rstrip("/") or "/"
    if resource_path != normalized_path:
        raise ValueError(
            "MCP_AUTH_RESOURCE resource path must match the configured MCP path"
        )
    verifier = token_verifier or auth.Auth0TokenVerifier(resolved_auth)
    protected_endpoint = RequireAuthMiddleware(
        mcp_endpoint,
        required_scopes=[resolved_auth.required_scope],
        resource_metadata_url=auth.resource_metadata_url(resolved_auth),
    )
    protected_endpoint = AuthContextMiddleware(protected_endpoint)
    protected_endpoint = AuthenticationMiddleware(
        protected_endpoint,
        backend=BearerAuthBackend(verifier),
    )
    routes.extend(auth.protected_resource_routes(resolved_auth))
    routes.append(
        Route(
            normalized_path,
            endpoint=protected_endpoint,
            methods=["GET", "POST", "DELETE"],
        )
    )
else:
    routes.append(
        Route(
            normalized_path,
            endpoint=mcp_endpoint,
            methods=["GET", "POST", "DELETE"],
        )
    )
```

Return `Starlette(routes=routes, lifespan=lifespan)`.

Update `run_http_server`:

```python
app = create_http_app(
    path=config.path,
    host=config.host,
    auth_config=config.auth,
)
```

- [ ] **Step 6: Run GREEN OAuth and regression tests**

```bash
python -m unittest tests.http_server_test.OAuthHttpApplicationTest -v
python -m unittest tests.http_server_test -v
python -m unittest tests.auth_test tests.http_server_test -v
```

Expected: PASS.

- [ ] **Step 7: Format and commit**

```bash
black -l 80 analytics_mcp/auth.py analytics_mcp/http_server.py tests/http_server_test.py
nox -s lint
git add analytics_mcp/auth.py analytics_mcp/http_server.py tests/http_server_test.py
git commit -m "feat(http): protect Streamable HTTP with OAuth"
```

---

### Task 4: Prove authenticated MCP protocol semantics

**Files:**
- Modify: `tests/http_server_test.py`

**Interfaces:**
- Proves OAuth preserves `initialize`, `list_tools`, `call_tool`, production tool discovery, and stateless operation.

- [ ] **Step 1: Add authenticated isolated-server protocol test**

Add:

```python
class AuthenticatedStreamableHttpProtocolTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_initializes_lists_calls_and_remains_stateless(self):
        verifier = _StaticTokenVerifier(_access_token(["analytics:read"]))
        app = http_server.create_http_app(
            mcp_server=create_test_mcp_server(),
            auth_config=AUTH_CONFIG,
            token_verifier=verifier,
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://analytics.example.com",
                headers={"Authorization": "Bearer test-token"},
            ) as http_client:
                async with streamable_http_client(
                    "https://analytics.example.com/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, get_session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool(
                            "echo_property",
                            {"property_id": "123456"},
                        )

        self.assertEqual(initialized.serverInfo.name, "test-analytics-mcp")
        self.assertEqual([tool.name for tool in tools.tools], ["echo_property"])
        self.assertFalse(result.isError)
        self.assertEqual(result.content[0].text, "123456")
        self.assertIsNone(get_session_id())
        self.assertGreaterEqual(len(verifier.calls), 1)
```

- [ ] **Step 2: Add authenticated production tool discovery test**

Add a second test that creates the default production MCP server with the same static token and verifier, initializes a session, calls `list_tools()`, and asserts these names are present:

```python
expected = {
    "get_account_summaries",
    "run_report",
    "run_realtime_report",
    "run_funnel_report",
    "run_conversions_report",
}
self.assertTrue(expected.issubset({tool.name for tool in tools.tools}))
```

Do not call Google APIs in this test.

- [ ] **Step 3: Run protocol suites**

```bash
python -m unittest \
  tests.http_server_test.AuthenticatedStreamableHttpProtocolTest -v
python -m unittest tests.http_server_test -v
```

Expected: PASS for both auth-enabled and auth-disabled flows.

- [ ] **Step 4: Commit**

```bash
git add tests/http_server_test.py
git commit -m "test(auth): cover authenticated MCP protocol"
```

---

### Task 5: Add operator documentation for Auth0, Cloud Run, and ChatGPT

**Files:**
- Create: `docs/chatgpt-oauth.md`
- Modify: `README.md`

**Interfaces:**
- Documents only implemented behavior and current external setup requirements.

- [ ] **Step 1: Create integration guide structure**

Create `docs/chatgpt-oauth.md` with these headings:

```text
# ChatGPT OAuth integration
## Architecture
## Prerequisites
## Create a dedicated Auth0 tenant
## Enable Resource Parameter Compatibility Profile
## Create the Google Analytics MCP API
## Restrict tenant login
## Bootstrap Cloud Run behind IAM
## Configure OAuth environment variables
## Validate the OAuth gate behind IAM
## Open only the Cloud Run invocation layer
## Add the MCP app in ChatGPT Developer Mode
## Verify tools
## Test a read-only Analytics call
## Rollback
## Troubleshooting
```

- [ ] **Step 2: Document exact Auth0 tenant settings**

Document:

```text
Tenant Settings -> Advanced -> Settings
Resource Parameter Compatibility Profile: Enabled
```

Document API settings:

```text
Name: Google Analytics MCP
Identifier: exact Cloud Run service URL plus /mcp
Signing algorithm: RS256
Permission: analytics:read
```

State that public database signup is disabled and only explicitly approved test identities may log in.

State that client registration between ChatGPT and Auth0 is controlled by Auth0. Prefer Auth0's current MCP/CIMD registration mechanism when ChatGPT exposes a CIMD client identifier; otherwise use Auth0's supported MCP client registration flow visible at test time. Do not add DCR or CIMD code to this Resource Server.

- [ ] **Step 3: Document server environment contract**

Document these variables exactly:

```text
MCP_AUTH_MODE=auth0
MCP_AUTH_ISSUER=https://TENANT_DOMAIN/
MCP_AUTH_RESOURCE=https://SERVICE_HOST/mcp
MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

Explain that `TENANT_DOMAIN` and `SERVICE_HOST` are runtime values discovered during deployment and must be stored in Cloud Run configuration, not source control.

- [ ] **Step 4: Document the two-phase Cloud Run airlock**

The guide must require:

1. Deploy with Cloud Run IAM enabled and `--no-allow-unauthenticated`.
2. Discover the stable service URL.
3. Create the Auth0 API using the exact service URL plus `/mcp`.
4. Configure OAuth env vars while Cloud Run IAM is still closed.
5. Validate application OAuth by sending the Google Cloud identity token in `X-Serverless-Authorization` and the Auth0 token in `Authorization`.
6. Only after missing Auth0 authorization returns 401 and a valid Auth0 token reaches MCP, grant `allUsers` the Cloud Run Invoker role on this dedicated service.
7. Repeat public checks immediately.

Explain why two different headers are required during the IAM-protected validation phase.

- [ ] **Step 5: Document ChatGPT product flow**

Document:

```text
Settings -> Apps -> Advanced settings -> Developer mode
Create app
Connection: Server URL
Server URL: deployed MCP resource URL
Authentication: OAuth
Verify tools
```

Document that the OAuth/OIDC provider must support refresh tokens and advertise `offline_access` or the current Auth0 equivalent so ChatGPT can maintain connectivity after access-token expiry.

- [ ] **Step 6: Add README pointer and commit**

Add one short paragraph under the remote MCP section in `README.md` linking to `docs/chatgpt-oauth.md` without changing the generic `docs/remote-server.md` guidance.

Run:

```bash
nox -s lint
git diff --check
git add README.md docs/chatgpt-oauth.md
git commit -m "docs(auth): add ChatGPT OAuth deployment guide"
```

---

### Task 6: Verify compatibility, packaging, and Docker

**Files:**
- No intended source changes unless a verification gate reveals a defect.

**Interfaces:**
- Produces fresh local evidence for all supported Python versions, the MCP lower bound, packaging, and container behavior.

- [ ] **Step 1: Run formatting and full Python matrix**

```bash
nox -s lint
git diff --check
nox -s tests-3.10 tests-3.11 tests-3.12 tests-3.13
```

Record exact pass counts from fresh output.

- [ ] **Step 2: Prove `mcp==1.24.0` lower-bound compatibility**

On Unix-like hosts:

```bash
python3.13 -m venv /tmp/analytics-mcp-lower-bound
/tmp/analytics-mcp-lower-bound/bin/python -m pip install --upgrade pip
/tmp/analytics-mcp-lower-bound/bin/python -m pip install -e .
/tmp/analytics-mcp-lower-bound/bin/python -m pip install "mcp==1.24.0"
/tmp/analytics-mcp-lower-bound/bin/python -m unittest \
  tests.auth_test tests.http_server_test -v
```

On Windows use an equivalent temporary venv and `Scripts/python.exe`. The test selection must remain `tests.auth_test tests.http_server_test`.

Expected: PASS with MCP 1.24.0.

- [ ] **Step 3: Build and inspect wheel and sdist**

```bash
python -m pip install --upgrade build
rm -rf dist build
python -m build
python -m zipfile -l dist/analytics_mcp-0.7.0-py3-none-any.whl
python -m tarfile -l dist/analytics_mcp-0.7.0.tar.gz
```

Confirm artifacts do not contain credentials, `.env` files, private keys, or `docs/superpowers/` planning material. If `docs/superpowers/` enters the sdist, add an explicit setuptools/MANIFEST exclusion and rebuild before proceeding.

- [ ] **Step 4: Clean-install the wheel**

```bash
python3.13 -m venv /tmp/analytics-mcp-wheel
/tmp/analytics-mcp-wheel/bin/python -m pip install \
  dist/analytics_mcp-0.7.0-py3-none-any.whl
/tmp/analytics-mcp-wheel/bin/analytics-mcp-http --help
/tmp/analytics-mcp-wheel/bin/python -c \
  "from analytics_mcp import auth; print(auth.AuthMode.AUTH0.value)"
```

Expected: HTTP help succeeds and the last command prints `auth0`.

- [ ] **Step 5: Build Docker image and prove fail-closed startup**

```bash
docker build -t analytics-mcp-oauth-validation:local .
docker run --rm \
  -e MCP_AUTH_MODE=auth0 \
  analytics-mcp-oauth-validation:local
```

Expected: the container exits non-zero because issuer/resource/scope are missing.

- [ ] **Step 6: Smoke-test public routes and unauthenticated `/mcp` in Docker**

```bash
docker run --rm -d \
  --name analytics-mcp-oauth-smoke \
  -p 8080:8080 \
  -e MCP_AUTH_MODE=auth0 \
  -e MCP_AUTH_ISSUER=https://example.us.auth0.com/ \
  -e MCP_AUTH_RESOURCE=http://localhost:8080/mcp \
  -e MCP_AUTH_REQUIRED_SCOPE=analytics:read \
  analytics-mcp-oauth-validation:local

curl -i http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/.well-known/oauth-protected-resource/mcp
curl -i -X POST http://127.0.0.1:8080/mcp

docker rm -f analytics-mcp-oauth-smoke
```

Expected: 200, 200, and 401 respectively.

- [ ] **Step 7: Prove authorized MCP initialization inside the built image**

Run the image with its default command overridden by a Python script. The script injects a static verifier only inside this disposable smoke process; no production bypass is added to the repository:

```bash
docker run --rm -i analytics-mcp-oauth-validation:local python - <<'PY'
import asyncio

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from analytics_mcp import auth, http_server


class StaticVerifier:
    async def verify_token(self, token):
        if token != "container-smoke-token":
            return None
        return AccessToken(
            token=token,
            client_id="container-smoke",
            scopes=["analytics:read"],
            expires_at=None,
            resource="http://localhost:8080/mcp",
        )


async def main():
    config = auth.AuthConfig(
        mode=auth.AuthMode.AUTH0,
        issuer="https://example.us.auth0.com/",
        resource="http://localhost:8080/mcp",
        required_scope="analytics:read",
    )
    app = http_server.create_http_app(
        auth_config=config,
        token_verifier=StaticVerifier(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:8080",
            headers={"Authorization": "Bearer container-smoke-token"},
        ) as client:
            async with streamable_http_client(
                "http://localhost:8080/mcp",
                http_client=client,
            ) as (read_stream, write_stream, get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    result = await session.initialize()
                    assert result.serverInfo.name
                    assert get_session_id() is None


asyncio.run(main())
print("authorized container MCP smoke: PASS")
PY
```

Expected: `authorized container MCP smoke: PASS`.

- [ ] **Step 8: Re-run all local gates**

```bash
nox -s lint
nox -s tests-3.10 tests-3.11 tests-3.12 tests-3.13
git status --short
```

Only commit source changes if a real defect was found and fixed. Do not create an empty verification commit.

---

### Task 7: Validate real Auth0, Cloud Run, ChatGPT, and Google Analytics

**Files:**
- Modify `docs/chatgpt-oauth.md` only if hosted behavior materially differs from the guide.

**Interfaces:**
- Produces end-to-end proof that ChatGPT completes OAuth, discovers tools, and invokes a real read-only Google Analytics tool.

- [ ] **Step 1: Create the dedicated Auth0 tenant and test identity**

Create a new free Auth0 tenant dedicated to this MCP integration. Record the tenant domain privately in the shell variable `TENANT_DOMAIN`.

Enable:

```text
Tenant Settings -> Advanced -> Settings
Resource Parameter Compatibility Profile: Enabled
```

Create only the intended test identity and disable public signup for the database connection used by this test.

- [ ] **Step 2: Bootstrap a dedicated Cloud Run service behind IAM**

Set deployment variables in the operator shell:

```bash
export SERVICE_NAME="analytics-mcp-chatgpt-oauth"
```

Use the already selected `PROJECT_ID`, `REGION`, and dedicated `SERVICE_ACCOUNT` values for this deployment.

Deploy:

```bash
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated

export SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format='value(status.url)')"
export MCP_RESOURCE="${SERVICE_URL}/mcp"
```

- [ ] **Step 3: Create the Auth0 API and temporary smoke client**

In Auth0 create an API/resource server:

```text
Name: Google Analytics MCP
Identifier: exact current value of MCP_RESOURCE
Signing algorithm: RS256
Permission: analytics:read
```

Create a temporary Machine-to-Machine application named `Google Analytics MCP Smoke Test`, authorize it for the new API with `analytics:read`, and keep its client ID and client secret only in local shell variables:

```bash
export AUTH0_SMOKE_CLIENT_ID="value-copied-from-auth0-dashboard"
export AUTH0_SMOKE_CLIENT_SECRET="value-copied-from-auth0-dashboard"
```

These values must never enter git, ChatGPT messages, issue comments, or PRs.

- [ ] **Step 4: Enable OAuth application mode while IAM is still closed**

```bash
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --update-env-vars \
MCP_AUTH_MODE=auth0,MCP_AUTH_ISSUER=https://${TENANT_DOMAIN}/,MCP_AUTH_RESOURCE=${MCP_RESOURCE},MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

Expected: revision becomes healthy. Missing or malformed variables must make the process fail rather than silently disabling auth.

- [ ] **Step 5: Obtain a real Auth0 smoke access token**

Request a token from the Auth0 token endpoint:

```bash
export AUTH0_ACCESS_TOKEN="$(curl --silent --request POST \
  --url "https://${TENANT_DOMAIN}/oauth/token" \
  --header 'content-type: application/json' \
  --data "{\"client_id\":\"${AUTH0_SMOKE_CLIENT_ID}\",\"client_secret\":\"${AUTH0_SMOKE_CLIENT_SECRET}\",\"resource\":\"${MCP_RESOURCE}\",\"audience\":\"${MCP_RESOURCE}\",\"scope\":\"analytics:read\",\"grant_type\":\"client_credentials\"}" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
```

The Resource Parameter Compatibility Profile allows the standards-based `resource` value; `audience` is supplied in this smoke request for Auth0 client-credentials compatibility and must resolve to the same API identifier.

Do not print the resulting token.

- [ ] **Step 6: Validate application OAuth behind Cloud Run IAM using separate headers**

Obtain a Google Cloud identity token for Cloud Run platform authentication:

```bash
export GOOGLE_ID_TOKEN="$(gcloud auth print-identity-token \
  --audiences="${SERVICE_URL}")"
```

Use `X-Serverless-Authorization` for the Google-signed Cloud Run identity token. Cloud Run validates that header at the platform layer and removes it before the request reaches the container. Reserve the normal `Authorization` header for the Auth0 Bearer token consumed by the MCP application.

Public liveness through the IAM airlock:

```bash
curl -i "${SERVICE_URL}/healthz" \
  -H "X-Serverless-Authorization: Bearer ${GOOGLE_ID_TOKEN}"
```

OAuth metadata through the IAM airlock:

```bash
curl -i "${SERVICE_URL}/.well-known/oauth-protected-resource/mcp" \
  -H "X-Serverless-Authorization: Bearer ${GOOGLE_ID_TOKEN}"
```

Unauthenticated-at-application-layer MCP request:

```bash
curl -i -X POST "${MCP_RESOURCE}" \
  -H "X-Serverless-Authorization: Bearer ${GOOGLE_ID_TOKEN}"
```

Expected: 200, 200, and 401.

Now prove real Auth0 authentication using a short Python MCP client:

```bash
GOOGLE_ID_TOKEN="${GOOGLE_ID_TOKEN}" \
AUTH0_ACCESS_TOKEN="${AUTH0_ACCESS_TOKEN}" \
MCP_RESOURCE="${MCP_RESOURCE}" \
python - <<'PY'
import asyncio
import os

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    headers = {
        "X-Serverless-Authorization": (
            "Bearer " + os.environ["GOOGLE_ID_TOKEN"]
        ),
        "Authorization": "Bearer " + os.environ["AUTH0_ACCESS_TOKEN"],
    }
    async with httpx.AsyncClient(headers=headers) as client:
        async with streamable_http_client(
            os.environ["MCP_RESOURCE"],
            http_client=client,
        ) as (read_stream, write_stream, get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                assert initialized.serverInfo.name
                assert len(tools.tools) > 0
                assert get_session_id() is None


asyncio.run(main())
print("Auth0 + Cloud Run IAM MCP smoke: PASS")
PY
```

Expected: `Auth0 + Cloud Run IAM MCP smoke: PASS`.

- [ ] **Step 7: Open only the Cloud Run invocation layer**

After Step 6 passes, grant public platform invocation on this dedicated service:

```bash
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --member="allUsers" \
  --role="roles/run.invoker"
```

Immediately verify from the public endpoint without the Google identity header:

```bash
curl -i "${SERVICE_URL}/healthz"
curl -i "${SERVICE_URL}/.well-known/oauth-protected-resource/mcp"
curl -i -X POST "${MCP_RESOURCE}"
```

Expected: 200, 200, and 401. If `/mcp` does not return 401, restore IAM immediately:

```bash
gcloud run services remove-iam-policy-binding "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --member="allUsers" \
  --role="roles/run.invoker"
```

Then diagnose before continuing.

Prove the Auth0 token works without Cloud Run IAM headers:

```bash
AUTH0_ACCESS_TOKEN="${AUTH0_ACCESS_TOKEN}" \
MCP_RESOURCE="${MCP_RESOURCE}" \
python - <<'PY'
import asyncio
import os

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with httpx.AsyncClient(
        headers={
            "Authorization": "Bearer " + os.environ["AUTH0_ACCESS_TOKEN"]
        }
    ) as client:
        async with streamable_http_client(
            os.environ["MCP_RESOURCE"],
            http_client=client,
        ) as (read_stream, write_stream, get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name
                assert get_session_id() is None


asyncio.run(main())
print("public Auth0 MCP smoke: PASS")
PY
```

- [ ] **Step 8: Configure the ChatGPT OAuth client relationship in Auth0**

Use Auth0's current MCP client-registration workflow. Auth0 recommends manual CIMD registration when the MCP client exposes a CIMD URL. If the ChatGPT OAuth flow presents a CIMD client identifier, import that exact HTTPS client metadata URL in Auth0 and review the imported redirect URIs before approving it. If ChatGPT uses Auth0's supported dynamic MCP registration path instead, enable only the current Auth0 setting required for that registration and record the verified setting in `docs/chatgpt-oauth.md`.

Do not change MCP server code for this client-registration step. Client registration is an Authorization Server concern.

Ensure Auth0's discovery metadata advertises refresh-token capability and `offline_access` when required by the current ChatGPT flow.

- [ ] **Step 9: Add the remote app in ChatGPT Developer Mode**

In ChatGPT Web:

```text
Settings -> Apps -> Advanced settings -> Developer mode
Create app
Name: Google Analytics MCP
Connection: Server URL
Server URL: current MCP_RESOURCE value
Authentication: OAuth
Verify tools
```

Expected flow:

1. ChatGPT reads RFC 9728 resource metadata.
2. ChatGPT discovers the Auth0 authorization server.
3. Auth0 Universal Login opens.
4. Sign in using the explicitly approved Auth0 test identity.
5. ChatGPT returns to app setup.
6. Tool verification completes.

Expected tools include:

```text
get_account_summaries
run_report
run_realtime_report
run_funnel_report
run_conversions_report
```

- [ ] **Step 10: Prove a real read-only Google Analytics call from ChatGPT**

In a new ChatGPT conversation with the development app enabled, request:

```text
List the Google Analytics accounts and properties available to this MCP.
```

Select one property returned by that live result and then request:

```text
For the property you just listed, show sessions and active users for the last 7 days.
```

Expected: ChatGPT invokes the existing read-only Google Analytics tools, and the second request uses `run_report` against the property selected from the first live result. Do not copy the property ID into repository files.

- [ ] **Step 11: Clean temporary smoke credentials and reconcile docs**

Delete or disable the temporary Auth0 Machine-to-Machine smoke application after ChatGPT user OAuth is proven. Clear local shell variables containing secrets and tokens.

If actual Auth0 or ChatGPT setup differed materially from the guide, update `docs/chatgpt-oauth.md`, run:

```bash
nox -s lint
git diff --check
```

and commit only verified documentation changes:

```bash
git add docs/chatgpt-oauth.md
git commit -m "docs(auth): record verified ChatGPT OAuth flow"
```

- [ ] **Step 12: Verification before completion**

Invoke `superpowers:verification-before-completion` and collect fresh evidence for every item:

```text
nox lint PASS
Python 3.10 PASS
Python 3.11 PASS
Python 3.12 PASS
Python 3.13 PASS
mcp==1.24.0 auth/HTTP PASS
wheel and sdist inspection PASS
clean wheel installation PASS
Docker build PASS
Docker fail-closed smoke PASS
Docker authorized MCP smoke PASS
Cloud Run IAM airlock checks PASS
real Auth0 access token -> MCP initialize PASS
public unauthenticated /mcp -> 401 PASS
public Auth0 access token -> MCP initialize PASS
ChatGPT OAuth PASS
ChatGPT tool discovery PASS
real read-only Google Analytics call PASS
```

Do not open an upstream PR from `feat/chatgpt-oauth`. The upstream candidate remains `feat/streamable-http-transport`.
