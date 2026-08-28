# ChatGPT OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect the remote Google Analytics MCP endpoint with Auth0-issued OAuth access tokens so ChatGPT Developer Mode can discover and invoke the existing read-only Analytics tools from a hosted deployment.

**Architecture:** `feat/chatgpt-oauth` remains layered on top of `feat/streamable-http-transport`. Auth0 is the OAuth Authorization Server; this repository is only an OAuth Resource Server. The server validates RS256 JWTs locally, publishes RFC 9728 Protected Resource Metadata with the MCP SDK, leaves `/healthz` public, and protects only the exact `/mcp` route.

**Tech Stack:** Python 3.10-3.13, MCP Python SDK `mcp>=1.24.0,<2`, Starlette, Uvicorn, PyJWT with cryptography, Auth0, Google Cloud Run, unittest, Nox, Docker.

**Spec:** `docs/superpowers/specs/2026-08-28-chatgpt-oauth-design.md`

## Global Constraints

- Keep `feat/streamable-http-transport` unchanged; all implementation work stays on `feat/chatgpt-oauth`.
- Preserve `analytics-mcp` and `google-analytics-mcp` stdio behavior unchanged.
- Preserve `analytics-mcp-http` and unauthenticated HTTP behavior when `MCP_AUTH_MODE=none`.
- Keep `mcp>=1.24.0,<2`; do not raise the lower bound to implement OAuth.
- Keep Python support at 3.10, 3.11, 3.12, and 3.13.
- Auth0 is the Authorization Server; this repository must not implement `/authorize`, `/token`, refresh-token storage, login UI, or consent UI.
- The only required application scope is `analytics:read`.
- Auth0 tokens must never be forwarded to Google APIs or logged.
- OAuth mode must fail closed at startup if issuer, resource, or required scope is missing or malformed.
- `/healthz` must remain public and must not resolve ADC, fetch JWKS, or call Google APIs.
- RFC 9728 metadata must be public at `/.well-known/oauth-protected-resource/mcp` for the default `/mcp` resource path.
- The exact `/mcp` route must remain a Starlette `Route`, not a `Mount`, so `/mcp` never redirects to `/mcp/`.
- A cryptographically invalid token returns 401. A valid token lacking `analytics:read` returns 403 `insufficient_scope`.
- Production resource URLs require HTTPS. HTTP is accepted only for localhost/loopback development and tests.
- The Cloud Run service may become platform-public only after application-layer OAuth has been proven to fail closed.
- Do not commit Auth0 secrets, tokens, Google credentials, tenant passwords, service-account keys, or generated private keys.

## File Structure

- Create `analytics_mcp/auth.py`: auth configuration, Auth0 JWT verification, and small SDK-auth helpers.
- Create `tests/auth_test.py`: configuration and JWT verifier unit tests using generated RSA keys and test doubles, with no real Auth0 dependency.
- Modify `analytics_mcp/http_server.py`: parse auth configuration and compose MCP SDK OAuth middleware/routes around only `/mcp`.
- Modify `tests/http_server_test.py`: HTTP auth discovery/gating and authenticated MCP protocol regression tests.
- Modify `pyproject.toml`: add the JWT dependency while preserving all existing dependency bounds and scripts.
- Create `docs/chatgpt-oauth.md`: operator guide for Auth0, Cloud Run, and ChatGPT Developer Mode validation.
- Modify `README.md`: add a short pointer to the OAuth integration guide without replacing the generic remote-server documentation.

---

### Task 1: Add fail-closed OAuth configuration

**Files:**
- Create: `analytics_mcp/auth.py`
- Create: `tests/auth_test.py`
- Modify: `analytics_mcp/http_server.py`

**Interfaces:**
- Produces: `AuthMode`, `AuthConfig`, `parse_auth_config(environ)`.
- `AuthConfig` fields: `mode`, `issuer`, `resource`, `required_scope`.
- `HttpServerConfig` gains an `auth: AuthConfig` field.
- `parse_http_config(argv, environ)` delegates OAuth environment parsing to `parse_auth_config`.

- [ ] **Step 1: Write the RED configuration tests**

Create `tests/auth_test.py` with the repository license header and these first tests:

```python
import unittest

from analytics_mcp import auth


class AuthConfigTest(unittest.TestCase):
    def test_defaults_to_disabled(self):
        config = auth.parse_auth_config({})
        self.assertEqual(config.mode, auth.AuthMode.NONE)
        self.assertIsNone(config.issuer)
        self.assertIsNone(config.resource)
        self.assertIsNone(config.required_scope)

    def test_auth0_mode_parses_complete_configuration(self):
        config = auth.parse_auth_config(
            {
                "MCP_AUTH_MODE": "auth0",
                "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                "MCP_AUTH_RESOURCE": "https://analytics.example.com/mcp",
                "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
            }
        )
        self.assertEqual(config.mode, auth.AuthMode.AUTH0)
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

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m unittest tests.auth_test.AuthConfigTest -v
```

Expected: FAIL because `analytics_mcp.auth` does not exist yet.

- [ ] **Step 3: Implement the minimal configuration model**

Create `analytics_mcp/auth.py` with the project license header and this public shape:

```python
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


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
```

Implement `parse_auth_config(environ: Mapping[str, str]) -> AuthConfig` with these exact policies:

```python
mode_value = environ.get("MCP_AUTH_MODE", "none").strip().lower()
```

- `none` returns `AuthConfig(AuthMode.NONE)` and ignores unused auth variables.
- any value other than `none` or `auth0` raises `ValueError("invalid MCP_AUTH_MODE: ...")`.
- Auth0 mode requires non-empty `MCP_AUTH_ISSUER`, `MCP_AUTH_RESOURCE`, and `MCP_AUTH_REQUIRED_SCOPE`.
- issuer is normalized to one trailing `/` and must be HTTPS except localhost/127.0.0.1/::1 development endpoints.
- resource removes only a trailing slash after the path and must be an absolute URL.
- production resource URLs must be HTTPS.
- `required_scope` is one non-empty scope string; do not silently replace it with a default in Auth0 mode.

Use `urllib.parse.urlparse` for deterministic URL validation. Do not make network calls.

- [ ] **Step 4: Integrate auth parsing into `HttpServerConfig`**

Modify `analytics_mcp/http_server.py`:

```python
from analytics_mcp import auth


@dataclass(frozen=True)
class HttpServerConfig:
    host: str
    port: int
    path: str
    auth: auth.AuthConfig
```

and return:

```python
return HttpServerConfig(
    args.host,
    args.port,
    args.path,
    auth.parse_auth_config(env),
)
```

Update existing `tests/http_server_test.py` default configuration assertion to include:

```python
self.assertEqual(config.auth.mode, auth.AuthMode.NONE)
```

- [ ] **Step 5: Run focused configuration tests and existing HTTP tests**

Run:

```bash
python -m unittest tests.auth_test.AuthConfigTest -v
python -m unittest tests.http_server_test.HttpServerConfigTest -v
```

Expected: PASS.

- [ ] **Step 6: Format and commit Task 1**

Run:

```bash
black -l 80 analytics_mcp/auth.py analytics_mcp/http_server.py tests/auth_test.py tests/http_server_test.py
python -m unittest tests.auth_test.AuthConfigTest tests.http_server_test.HttpServerConfigTest -v
git add analytics_mcp/auth.py analytics_mcp/http_server.py tests/auth_test.py tests/http_server_test.py
git commit -m "feat(auth): add fail-closed OAuth configuration"
```

Expected: tests PASS and commit succeeds.

---

### Task 2: Validate Auth0 RS256 access tokens locally

**Files:**
- Modify: `analytics_mcp/auth.py`
- Modify: `tests/auth_test.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `AuthConfig` from Task 1.
- Produces: `Auth0TokenVerifier(config, jwks_client=None)` implementing MCP `TokenVerifier`.
- `verify_token(token: str) -> AccessToken | None` performs authentication only; it extracts scopes but does not enforce `required_scope`, allowing the SDK authorization layer to return 403.

- [ ] **Step 1: Add the JWT runtime dependency**

Modify `pyproject.toml` dependencies by adding:

```toml
"PyJWT[crypto]>=2.10,<3",
```

Keep `mcp>=1.24.0,<2` unchanged.

- [ ] **Step 2: Add RSA/JWT test helpers**

Extend `tests/auth_test.py` imports:

```python
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken
```

Add a helper that creates one RSA key per test class and signs deterministic access tokens:

```python
def _sign_token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://example.us.auth0.com/",
        "aud": "https://analytics.example.com/mcp",
        "sub": "auth0|test-user",
        "iat": now,
        "exp": now + 300,
        "scope": "analytics:read",
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
```

Use a fake signing-key client whose `get_signing_key_from_jwt(token)` returns an object with `.key` equal to the generated RSA public key. This keeps unit tests offline while still exercising real RS256 signature verification in PyJWT.

- [ ] **Step 3: Write RED verifier tests**

Add `Auth0TokenVerifierTest(unittest.IsolatedAsyncioTestCase)` covering:

```python
async def test_accepts_valid_rs256_token(self): ...
async def test_rejects_forged_signature(self): ...
async def test_rejects_expired_token(self): ...
async def test_rejects_wrong_issuer(self): ...
async def test_rejects_wrong_audience(self): ...
async def test_rejects_unsupported_algorithm(self): ...
async def test_extracts_space_delimited_scopes(self): ...
async def test_rejects_token_without_subject(self): ...
async def test_does_not_log_raw_token(self): ...
```

For a valid token assert:

```python
result = await verifier.verify_token(token)
self.assertIsInstance(result, AccessToken)
self.assertEqual(result.client_id, "auth0|test-user")
self.assertEqual(result.scopes, ["analytics:read"])
self.assertEqual(result.resource, "https://analytics.example.com/mcp")
```

For invalid tokens assert `result is None`.

For `test_extracts_space_delimited_scopes`, sign with:

```python
scope="openid offline_access analytics:read"
```

and expect:

```python
["openid", "offline_access", "analytics:read"]
```

- [ ] **Step 4: Run verifier tests and verify RED**

Run:

```bash
python -m unittest tests.auth_test.Auth0TokenVerifierTest -v
```

Expected: FAIL because `Auth0TokenVerifier` is not implemented.

- [ ] **Step 5: Implement `Auth0TokenVerifier`**

In `analytics_mcp/auth.py`, import:

```python
import logging

import anyio
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
```

Implement:

```python
class Auth0TokenVerifier(TokenVerifier):
    def __init__(self, config: AuthConfig, jwks_client=None):
        if not config.enabled:
            raise ValueError("Auth0TokenVerifier requires auth0 mode")
        self._config = config
        self._jwks_client = jwks_client or jwt.PyJWKClient(
            f"{config.issuer}.well-known/jwks.json"
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        ...
```

Use `anyio.to_thread.run_sync` around `PyJWKClient.get_signing_key_from_jwt` so synchronous JWKS lookup never blocks the event loop. Decode with an explicit algorithm allowlist:

```python
claims = jwt.decode(
    token,
    signing_key.key,
    algorithms=["RS256"],
    audience=self._config.resource,
    issuer=self._config.issuer,
)
```

Require `sub` to be a non-empty string and `exp` to be an integer. Extract `scope` from a space-delimited string. Return:

```python
AccessToken(
    token=token,
    client_id=subject,
    scopes=scopes,
    expires_at=claims["exp"],
    resource=self._config.resource,
)
```

Catch PyJWT authentication/validation exceptions and return `None`. Log only the exception class or a short category at debug/warning level. Never interpolate the raw token.

Do not enforce `analytics:read` here.

- [ ] **Step 6: Run verifier tests GREEN**

Run:

```bash
python -m unittest tests.auth_test.Auth0TokenVerifierTest -v
```

Expected: PASS.

- [ ] **Step 7: Prove the configured JWKS endpoint and cache-capable client**

Add a test that patches `jwt.PyJWKClient` and constructs `Auth0TokenVerifier(config)`. Assert it receives exactly:

```text
https://example.us.auth0.com/.well-known/jwks.json
```

Do not assert private PyJWT cache internals. PyJWKClient's built-in key-set caching remains enabled by default; the contract test only ensures one reusable client instance is created per verifier rather than one per request.

Run:

```bash
python -m unittest tests.auth_test -v
```

Expected: PASS.

- [ ] **Step 8: Format and commit Task 2**

Run:

```bash
black -l 80 analytics_mcp/auth.py tests/auth_test.py
python -m unittest tests.auth_test -v
git add analytics_mcp/auth.py tests/auth_test.py pyproject.toml
git commit -m "feat(auth): validate Auth0 access tokens"
```

Expected: PASS and commit succeeds.

---

### Task 3: Protect only `/mcp` and publish RFC 9728 metadata

**Files:**
- Modify: `analytics_mcp/auth.py`
- Modify: `analytics_mcp/http_server.py`
- Modify: `tests/http_server_test.py`

**Interfaces:**
- Consumes: `AuthConfig`, `Auth0TokenVerifier`.
- Produces: OAuth-enabled `create_http_app(..., auth_config=None, token_verifier=None)`.
- Public routes remain `/healthz` and the RFC 9728 metadata route.
- Only `/mcp` receives `AuthenticationMiddleware`, `AuthContextMiddleware`, and `RequireAuthMiddleware`.

- [ ] **Step 1: Write RED discovery and gating tests**

Extend `tests/http_server_test.py` imports:

```python
from mcp.server.auth.provider import AccessToken
from analytics_mcp import auth
```

Add a deterministic fake verifier:

```python
class _StaticTokenVerifier:
    def __init__(self, access_token):
        self.access_token = access_token
        self.calls = []

    async def verify_token(self, token):
        self.calls.append(token)
        return self.access_token
```

Add `OAuthHttpApplicationTest(unittest.TestCase)` with tests:

1. `test_healthz_stays_public_when_oauth_enabled`
2. `test_oauth_metadata_reports_resource_issuer_and_scope`
3. `test_mcp_without_token_returns_401`
4. `test_401_challenge_includes_resource_metadata`
5. `test_invalid_token_returns_401`
6. `test_valid_token_without_required_scope_returns_403`
7. `test_auth_disabled_does_not_publish_metadata`
8. `test_auth_disabled_keeps_mcp_unprotected`
9. `test_oauth_mcp_path_does_not_redirect`

Use this config in tests:

```python
AUTH_CONFIG = auth.AuthConfig(
    mode=auth.AuthMode.AUTH0,
    issuer="https://example.us.auth0.com/",
    resource="https://analytics.example.com/mcp",
    required_scope="analytics:read",
)
```

Expected metadata path:

```text
/.well-known/oauth-protected-resource/mcp
```

Expected metadata fields:

```python
self.assertEqual(body["resource"], "https://analytics.example.com/mcp")
self.assertEqual(
    body["authorization_servers"],
    ["https://example.us.auth0.com/"],
)
self.assertEqual(body["scopes_supported"], ["analytics:read"])
```

- [ ] **Step 2: Run OAuth HTTP tests and verify RED**

Run:

```bash
python -m unittest tests.http_server_test.OAuthHttpApplicationTest -v
```

Expected: FAIL because `create_http_app` does not accept auth configuration and does not compose auth middleware/routes.

- [ ] **Step 3: Add small SDK-auth helper functions**

In `analytics_mcp/auth.py`, add helpers that use MCP SDK v1.24 APIs rather than reimplementing protocol JSON:

```python
def protected_resource_routes(config: AuthConfig):
    ...


def resource_metadata_url(config: AuthConfig):
    ...
```

Use:

```python
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_protected_resource_routes,
)
from pydantic import AnyHttpUrl
```

`protected_resource_routes` returns:

```python
create_protected_resource_routes(
    resource_url=AnyHttpUrl(config.resource),
    authorization_servers=[AnyHttpUrl(config.issuer)],
    scopes_supported=[config.required_scope],
    resource_name="Google Analytics MCP",
)
```

- [ ] **Step 4: Compose authentication around only the MCP endpoint**

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

Default `auth_config` to `AuthConfig(AuthMode.NONE)`.

For Auth0 mode, require the configured resource path to match the configured MCP route path. Compare `urlparse(auth_config.resource).path.rstrip("/") or "/"` with `normalized_path`; mismatch must raise `ValueError` at app creation instead of publishing misleading discovery metadata.

Build the existing `_McpEndpoint(session_manager)`, then wrap it in this effective request order:

```text
AuthenticationMiddleware
  -> AuthContextMiddleware
    -> RequireAuthMiddleware
      -> _McpEndpoint
```

Construct wrappers inside-out so Starlette executes them in that order:

```python
protected = RequireAuthMiddleware(
    mcp_endpoint,
    required_scopes=[auth_config.required_scope],
    resource_metadata_url=auth.resource_metadata_url(auth_config),
)
protected = AuthContextMiddleware(protected)
protected = AuthenticationMiddleware(
    protected,
    backend=BearerAuthBackend(verifier),
)
```

Use imports from MCP SDK v1.24:

```python
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import TokenVerifier
from starlette.middleware.authentication import AuthenticationMiddleware
```

Append `auth.protected_resource_routes(auth_config)` to the Starlette route list only in Auth0 mode.

Do not wrap `/healthz` or metadata routes.

- [ ] **Step 5: Wire production startup to parsed auth config**

Update `run_http_server`:

```python
app = create_http_app(
    path=config.path,
    host=config.host,
    auth_config=config.auth,
)
```

If auth is enabled and no test verifier is injected, create `Auth0TokenVerifier(auth_config)` once during app construction. This provides one reusable PyJWKClient per process.

- [ ] **Step 6: Run the HTTP auth suite GREEN**

Run:

```bash
python -m unittest tests.http_server_test.OAuthHttpApplicationTest -v
python -m unittest tests.http_server_test.HttpApplicationTest -v
```

Expected: PASS.

- [ ] **Step 7: Re-run the complete HTTP transport suite**

Run:

```bash
python -m unittest tests.http_server_test -v
```

Expected: all tests PASS, including existing stateless and tool-discovery tests with auth disabled.

- [ ] **Step 8: Format and commit Task 3**

Run:

```bash
black -l 80 analytics_mcp/auth.py analytics_mcp/http_server.py tests/http_server_test.py
python -m unittest tests.auth_test tests.http_server_test -v
git add analytics_mcp/auth.py analytics_mcp/http_server.py tests/http_server_test.py
git commit -m "feat(http): protect Streamable HTTP with OAuth"
```

Expected: PASS and commit succeeds.

---

### Task 4: Prove authenticated MCP protocol behavior end to end

**Files:**
- Modify: `tests/http_server_test.py`

**Interfaces:**
- Consumes: OAuth-enabled `create_http_app` from Task 3.
- Produces: protocol-level proof that a valid token preserves initialize/list/call/stateless behavior.

- [ ] **Step 1: Write the RED authenticated protocol test**

Add `AuthenticatedStreamableHttpProtocolTest(unittest.IsolatedAsyncioTestCase)`.

Build a valid static `AccessToken`:

```python
access_token = AccessToken(
    token="test-token",
    client_id="auth0|test-user",
    scopes=["analytics:read"],
    expires_at=None,
    resource="https://analytics.example.com/mcp",
)
```

Create the app with the isolated `create_test_mcp_server()`, Auth0 config, and `_StaticTokenVerifier(access_token)`.

Create `httpx.AsyncClient` with:

```python
headers={"Authorization": "Bearer test-token"}
```

and use the existing MCP client:

```python
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
```

Assert:

```python
self.assertEqual(initialized.serverInfo.name, "test-analytics-mcp")
self.assertEqual([tool.name for tool in tools.tools], ["echo_property"])
self.assertFalse(result.isError)
self.assertEqual(result.content[0].text, "123456")
self.assertIsNone(get_session_id())
```

- [ ] **Step 2: Run the authenticated protocol test**

Run:

```bash
python -m unittest \
  tests.http_server_test.AuthenticatedStreamableHttpProtocolTest -v
```

Expected: PASS if Task 3 composition is correct. If it fails, treat the failure as a defect in the new OAuth path and debug before proceeding; do not weaken the test.

- [ ] **Step 3: Add authenticated production tool discovery**

Add a second async test using the production `coordinator.app` with the same static verifier/token. Initialize and call `list_tools()` only; do not call Google APIs.

Assert that the names include:

```text
get_account_summaries
run_report
run_realtime_report
run_funnel_report
run_conversions_report
```

This proves OAuth composition did not fork or replace the existing tool registry.

- [ ] **Step 4: Run both authenticated and unauthenticated protocol suites**

Run:

```bash
python -m unittest tests.http_server_test -v
```

Expected: PASS for both old auth-disabled protocol tests and new auth-enabled protocol tests.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add tests/http_server_test.py
git commit -m "test(auth): cover authenticated MCP protocol"
```

Expected: commit succeeds.

---

### Task 5: Document Auth0, Cloud Run, and ChatGPT setup

**Files:**
- Create: `docs/chatgpt-oauth.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the exact environment variables and HTTP behavior implemented in Tasks 1-4.
- Produces: a reproducible operator guide for a dedicated private Auth0 tenant and OAuth-enabled Cloud Run service.

- [ ] **Step 1: Write the integration guide**

Create `docs/chatgpt-oauth.md` with these exact sections:

```text
# ChatGPT OAuth integration
## Architecture
## Prerequisites
## Create the dedicated Auth0 tenant
## Create the Google Analytics MCP API
## Restrict tenant login
## Bootstrap Cloud Run behind IAM
## Configure the MCP OAuth environment
## Validate fail-closed behavior
## Make the Cloud Run service reachable
## Add the MCP app in ChatGPT Developer Mode
## Verify tools
## Test a read-only Analytics call
## Rollback
## Troubleshooting
```

Document these Auth0 API settings:

```text
Name: Google Analytics MCP
Identifier: the exact deployed https://SERVICE_HOST/mcp URL
Signing algorithm: RS256
Permission: analytics:read
Resource Parameter Compatibility Profile: enabled
```

Document server environment:

```text
MCP_AUTH_MODE=auth0
MCP_AUTH_ISSUER=https://TENANT_DOMAIN/
MCP_AUTH_RESOURCE=https://SERVICE_HOST/mcp
MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

Use `TENANT_DOMAIN` and `SERVICE_HOST` as shell variable names defined before commands, not as values to commit. Example:

```bash
export TENANT_DOMAIN="tenant-name.region.auth0.com"
export SERVICE_HOST="analytics-mcp-oauth-abc123.us-central1.run.app"
```

Explicitly state that actual values come from the newly created Auth0 tenant and Cloud Run service and are deployment configuration, not repository constants.

- [ ] **Step 2: Document the safe two-phase Cloud Run bootstrap**

The guide must require this order:

1. Deploy the service initially with Cloud Run IAM protection enabled.
2. Read the stable Cloud Run service URL.
3. Create/configure the Auth0 API using the exact `${SERVICE_URL}/mcp` identifier.
4. Update Cloud Run env vars with OAuth mode, issuer, resource, and scope while IAM still protects the service.
5. Through an authenticated Cloud Run proxy or Google-signed invocation, verify `/healthz`, RFC 9728 metadata, 401 without Bearer token, and a valid Auth0 token reaching `/mcp`.
6. Only after the OAuth gate is proven, allow unauthenticated platform invocation so ChatGPT can reach the application-layer OAuth challenge.
7. Re-run unauthenticated and authorized checks from the public Internet.

Document that `--allow-unauthenticated` at the platform layer does not make `/mcp` anonymous because the application itself returns 401 without Auth0 Bearer authorization.

- [ ] **Step 3: Document ChatGPT Developer Mode setup**

Include the product flow:

```text
Settings -> Apps -> Advanced settings -> Developer mode
Create app -> Server URL -> https://SERVICE_HOST/mcp
Authentication -> OAuth
Verify tools
```

State expected behavior:

- ChatGPT discovers Protected Resource Metadata.
- ChatGPT opens Auth0 Universal Login.
- The approved Auth0 user authorizes the connection.
- Tool verification returns the existing Google Analytics tools.

Do not promise a specific DCR/CIMD implementation path. State that client registration is handled by Auth0 and ChatGPT and may evolve independently of this Resource Server.

- [ ] **Step 4: Add a short README pointer**

Under the remote MCP section in `README.md`, add a paragraph that says OAuth testing with ChatGPT is documented separately and links to `docs/chatgpt-oauth.md`. Keep the existing generic `docs/remote-server.md` security guidance intact.

- [ ] **Step 5: Format/check docs and commit Task 5**

Run:

```bash
nox -s lint
git diff --check
git add README.md docs/chatgpt-oauth.md
git commit -m "docs(auth): add ChatGPT OAuth deployment guide"
```

Expected: lint and whitespace checks PASS and commit succeeds.

---

### Task 6: Prove compatibility, packaging, and container behavior

**Files:**
- No intended source changes unless verification exposes a defect.
- Verification may regenerate local `dist/` artifacts, which must remain untracked.

**Interfaces:**
- Consumes: complete local implementation from Tasks 1-5.
- Produces: fresh evidence for lint, Python matrix, MCP lower bound, wheel/sdist, clean install, and Docker smoke tests.

- [ ] **Step 1: Run formatting gate**

Run:

```bash
nox -s lint
git diff --check
```

Expected: PASS with no formatting or whitespace errors.

- [ ] **Step 2: Run the repository Python matrix**

Run:

```bash
nox -s tests-3.10 tests-3.11 tests-3.12 tests-3.13
```

Expected: all available configured runtimes PASS. Record exact test counts from fresh output.

- [ ] **Step 3: Prove the declared MCP lower bound**

Create a clean Python 3.13 virtual environment outside the repository's normal environment, install the project, then force MCP 1.24.0:

```bash
python3.13 -m venv /tmp/analytics-mcp-lower-bound
/tmp/analytics-mcp-lower-bound/bin/python -m pip install --upgrade pip
/tmp/analytics-mcp-lower-bound/bin/python -m pip install -e .
/tmp/analytics-mcp-lower-bound/bin/python -m pip install "mcp==1.24.0"
/tmp/analytics-mcp-lower-bound/bin/python -m unittest \
  tests.auth_test tests.http_server_test -v
```

Expected: auth and HTTP suites PASS with `mcp==1.24.0`.

On Windows execution environments, use an equivalent temporary venv path and `Scripts/python.exe`; do not change the test semantics.

- [ ] **Step 4: Build wheel and sdist**

Run in a clean build environment:

```bash
python -m pip install --upgrade build
rm -rf dist build
python -m build
```

Expected artifacts:

```text
dist/analytics_mcp-0.7.0-py3-none-any.whl
dist/analytics_mcp-0.7.0.tar.gz
```

Inspect archive listings and confirm neither artifact contains:

```text
docs/superpowers/
.env
credentials
private keys
```

If current setuptools includes `docs/superpowers/` in the sdist, add explicit package/build exclusion before proceeding; do not ship internal planning material unintentionally.

- [ ] **Step 5: Clean-install the wheel and verify scripts**

Create a second clean Python 3.13 environment and install the built wheel:

```bash
python3.13 -m venv /tmp/analytics-mcp-wheel
/tmp/analytics-mcp-wheel/bin/python -m pip install \
  dist/analytics_mcp-0.7.0-py3-none-any.whl
```

Verify:

```bash
/tmp/analytics-mcp-wheel/bin/analytics-mcp-http --help
/tmp/analytics-mcp-wheel/bin/python -c \
  "from analytics_mcp import auth; print(auth.AuthMode.AUTH0.value)"
```

Expected output includes `auth0`, and the HTTP entrypoint help succeeds.

- [ ] **Step 6: Build the Docker image**

Run:

```bash
docker build -t analytics-mcp-oauth-validation:local .
```

Expected: build succeeds.

- [ ] **Step 7: Smoke-test OAuth configuration fail closed in Docker**

Run the image with:

```bash
docker run --rm \
  -e MCP_AUTH_MODE=auth0 \
  analytics-mcp-oauth-validation:local
```

Expected: process exits non-zero because issuer/resource/scope are missing.

Then run with deterministic local-test configuration:

```bash
docker run --rm -d \
  --name analytics-mcp-oauth-smoke \
  -p 8080:8080 \
  -e MCP_AUTH_MODE=auth0 \
  -e MCP_AUTH_ISSUER=https://example.us.auth0.com/ \
  -e MCP_AUTH_RESOURCE=http://localhost:8080/mcp \
  -e MCP_AUTH_REQUIRED_SCOPE=analytics:read \
  analytics-mcp-oauth-validation:local
```

Verify:

```bash
curl -i http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/.well-known/oauth-protected-resource/mcp
curl -i -X POST http://127.0.0.1:8080/mcp
```

Expected:

- `/healthz` -> `200 ok`.
- metadata -> `200` with resource, authorization server, and `analytics:read`.
- unauthenticated `/mcp` -> `401` with `WWW-Authenticate: Bearer` and `resource_metadata`.

Stop the container:

```bash
docker rm -f analytics-mcp-oauth-smoke
```

- [ ] **Step 8: Run final local verification and commit only real fixes**

Run again after any necessary corrections:

```bash
nox -s lint
nox -s tests-3.10 tests-3.11 tests-3.12 tests-3.13
git status --short
```

Expected: all gates PASS and no generated artifacts are staged. If verification required source changes, commit each logically with a humanized English message; otherwise create no empty verification commit.

---

### Task 7: Validate the real Auth0 -> Cloud Run -> ChatGPT -> Google Analytics path

**Files:**
- Modify `docs/chatgpt-oauth.md` only if real hosted behavior differs from the documented instructions.
- No credentials or environment-specific secrets are committed.

**Interfaces:**
- Consumes: verified container image/application from Task 6.
- Produces: hosted proof that ChatGPT can complete OAuth, discover tools, and invoke at least one real read-only Google Analytics tool.

- [ ] **Step 1: Create the dedicated Auth0 tenant**

In the Auth0 Dashboard, create a new free tenant dedicated to this MCP test. Record its tenant domain privately as `TENANT_DOMAIN`.

Create only the intended test user and disable public database-connection signups or otherwise restrict login to explicitly approved identities.

Do not place Auth0 account passwords or management tokens in this repository.

- [ ] **Step 2: Bootstrap the dedicated Cloud Run service behind IAM**

Use a separate service name from any upstream/generic deployment, for example shell variable:

```bash
export SERVICE_NAME="analytics-mcp-chatgpt-oauth"
```

Deploy from `feat/chatgpt-oauth` with a dedicated runtime service account and Cloud Run IAM protection still enabled:

```bash
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated
```

Read the stable service URL:

```bash
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format='value(status.url)')"
echo "${SERVICE_URL}"
```

Set privately:

```bash
export MCP_RESOURCE="${SERVICE_URL}/mcp"
```

- [ ] **Step 3: Create the Auth0 API for the exact MCP resource**

In Auth0 Dashboard create API/resource server:

```text
Name: Google Analytics MCP
Identifier: exact value of MCP_RESOURCE
Signing algorithm: RS256
```

Add permission:

```text
analytics:read
```

Enable Auth0 Resource Parameter Compatibility Profile for the API/tenant path used by the current Auth0 MCP integration.

Configure refresh/offline access according to the current Auth0 ChatGPT MCP guidance so ChatGPT can retain the connection when it requests `offline_access`.

- [ ] **Step 4: Enable application OAuth while Cloud Run IAM is still closed**

Update Cloud Run environment variables:

```bash
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --update-env-vars \
MCP_AUTH_MODE=auth0,MCP_AUTH_ISSUER=https://${TENANT_DOMAIN}/,MCP_AUTH_RESOURCE=${MCP_RESOURCE},MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

Expected: revision becomes healthy. A malformed/missing configuration must instead fail startup.

- [ ] **Step 5: Verify OAuth behavior behind the IAM airlock**

Use:

```bash
gcloud run services proxy "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --port 8080
```

Through the local proxy verify:

```bash
curl -i http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/.well-known/oauth-protected-resource/mcp
curl -i -X POST http://127.0.0.1:8080/mcp
```

Expected: 200, 200, 401 respectively.

Obtain one real Auth0 access token for `${MCP_RESOURCE}` with `analytics:read` using Auth0's supported test/application flow. Do not paste the token into git, issues, PRs, or documentation.

Send an MCP `initialize` request with:

```http
Authorization: Bearer <runtime access token>
```

Expected: MCP JSON-RPC initialization succeeds.

- [ ] **Step 6: Make the Cloud Run platform reachable only after OAuth passes**

After Step 5 succeeds, update Cloud Run invocation policy so public internet requests can reach the application. The exact command depends on the current Cloud Run IAM generation; use the current Google Cloud supported method for allowing unauthenticated platform invocation on this dedicated service only.

Immediately repeat from a network path that is not the authenticated Cloud Run proxy:

```text
GET  /healthz                                    -> 200
GET  /.well-known/oauth-protected-resource/mcp  -> 200
POST /mcp without token                         -> 401
POST /mcp with valid Auth0 token                -> MCP response
```

If unauthenticated `/mcp` returns anything other than 401, restore Cloud Run IAM protection immediately and treat the deployment as failed.

- [ ] **Step 7: Add the remote MCP in ChatGPT Developer Mode**

In ChatGPT Web:

```text
Settings -> Apps -> Advanced settings -> Developer mode
Create app
Name: Google Analytics MCP
Connection: Server URL
Server URL: exact MCP_RESOURCE
Authentication: OAuth
Verify tools
```

Expected flow:

1. ChatGPT discovers RFC 9728 metadata.
2. ChatGPT discovers Auth0 authorization metadata/client-registration behavior.
3. Auth0 Universal Login opens.
4. Sign in with the explicitly approved Auth0 test identity.
5. ChatGPT returns to app creation and verifies tools.

Expected discovered tool set includes at least:

```text
get_account_summaries
run_report
run_realtime_report
run_funnel_report
run_conversions_report
```

- [ ] **Step 8: Prove a real Google Analytics call from ChatGPT**

With the app enabled in a new ChatGPT conversation, first request a low-risk discovery operation:

```text
List the Google Analytics accounts and properties available to this MCP.
```

Expected: ChatGPT invokes the existing account/property discovery tool through the remote MCP and returns data visible to the Cloud Run runtime Google identity.

Then request one bounded report:

```text
For property <an authorized GA4 property selected from the previous result>,
show sessions and active users for the last 7 days.
```

Expected: ChatGPT invokes `run_report` and returns a successful read-only Analytics result.

Do not hardcode the selected property ID into repository files; it is runtime test data.

- [ ] **Step 9: Reconcile documentation with actual hosted behavior**

If Auth0 or ChatGPT required a materially different current setting than documented, update `docs/chatgpt-oauth.md` with the verified behavior. Do not document temporary secrets or tokens.

Run:

```bash
nox -s lint
git diff --check
```

If documentation changed:

```bash
git add docs/chatgpt-oauth.md
git commit -m "docs(auth): record verified ChatGPT OAuth flow"
```

- [ ] **Step 10: Final verification-before-completion**

Before claiming the feature complete, collect fresh evidence for:

```text
nox lint PASS
Python 3.10 PASS
Python 3.11 PASS
Python 3.12 PASS
Python 3.13 PASS
mcp==1.24.0 auth/HTTP PASS
wheel/sdist inspection PASS
clean wheel install PASS
Docker build PASS
Docker OAuth smoke PASS
Cloud Run fail-closed check PASS
Auth0 real token -> MCP initialize PASS
ChatGPT OAuth PASS
ChatGPT tool discovery PASS
real read-only Google Analytics tool call PASS
```

Use `superpowers:verification-before-completion` before writing the final completion report.

Do not open an upstream PR from `feat/chatgpt-oauth`. The upstream contribution remains `feat/streamable-http-transport`.
