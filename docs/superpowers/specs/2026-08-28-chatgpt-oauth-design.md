# ChatGPT OAuth design

**Status:** Approved design, ready for implementation planning

**Date:** 2026-08-28

**Branch:** `feat/chatgpt-oauth`

**Base:** `feat/streamable-http-transport`

## Context

The remote Streamable HTTP transport is already implemented and validated on
`feat/streamable-http-transport`. That branch is intended to remain generic and
upstream-friendly. It exposes the existing Google Analytics MCP tools over
stateless Streamable HTTP, keeps the stdio entrypoints unchanged, and delegates
remote access control to the deployment layer.

For ChatGPT Developer Mode, we need a remotely reachable MCP endpoint that can
participate in OAuth discovery and require an access token before serving MCP
requests. This feature must not alter the upstream transport branch or change
how the server authenticates to Google Analytics.

The OAuth layer therefore has two independent trust relationships:

1. ChatGPT authenticates to the MCP server with an OAuth access token issued by
   Auth0.
2. The MCP server continues to authenticate to Google Analytics with
   Application Default Credentials (ADC) or the Cloud Run service identity.

The MCP server is an OAuth Resource Server only. It does not issue OAuth tokens,
render login pages, store authorization codes, or implement `/authorize` or
`/token` endpoints.

## Goals

- Protect `/mcp` with OAuth Bearer authentication for ChatGPT.
- Keep `/healthz` public and independent of Auth0 and Google credentials.
- Publish RFC 9728 Protected Resource Metadata for OAuth discovery.
- Validate Auth0 JWT access tokens locally.
- Require a single least-privilege scope: `analytics:read`.
- Validate issuer, audience/resource, signature, expiry, and required scope.
- Preserve the existing stateless Streamable HTTP behavior and tool schemas.
- Preserve the existing stdio entrypoints.
- Preserve the existing unauthenticated HTTP behavior when OAuth is explicitly
  disabled.
- Fail closed when OAuth is enabled but misconfigured.
- Remain compatible with the current lower bound `mcp>=1.24.0,<2`.
- Deploy to Cloud Run in a way that ChatGPT can reach the OAuth discovery and
  MCP endpoints without requiring a Google Cloud IAM identity token.

## Non-goals

- End-user Google OAuth for Google Analytics.
- Per-user Google Analytics credentials.
- Multi-tenant Google Analytics identity delegation.
- A custom OAuth Authorization Server.
- OAuth token issuance, refresh-token storage, consent screens, or login UI in
  this repository.
- On-Behalf-Of token exchange to Google APIs.
- A generalized authentication framework for every possible IdP.
- Changes to the upstream-ready `feat/streamable-http-transport` branch.
- MCP v2 migration.
- Redis, database-backed sessions, sticky sessions, or an OAuth session store.

## Branch strategy

The branch graph is intentionally layered:

```text
main
  |
  +-- feat/streamable-http-transport
          |
          +-- feat/chatgpt-oauth
```

`feat/streamable-http-transport` stays suitable for the upstream Google PR.
`feat/chatgpt-oauth` is an integration branch for Auth0, ChatGPT, and hosted
deployment testing.

## Architecture

```text
ChatGPT Developer Mode
        |
        | OAuth 2.x authorization flow
        | resource=<public MCP URL>
        v
Auth0 Authorization Server
        |
        | signed JWT access token
        | scope=analytics:read
        v
Google Analytics MCP
  - /healthz                                    public
  - /.well-known/oauth-protected-resource/mcp  public
  - /mcp                                       protected
        |
        | ADC / Cloud Run service identity
        v
Google Analytics APIs
```

Auth0 owns user login, consent, authorization-code handling, PKCE, token
issuance, refresh tokens, and client registration behavior. The MCP process owns
only resource-server responsibilities.

## Public resource identity

The deployed MCP endpoint is the OAuth resource identifier and Auth0 API
identifier.

Example:

```text
https://analytics-mcp-abc123.run.app/mcp
```

The same exact value is used for:

- the public MCP URL supplied to ChatGPT;
- `MCP_AUTH_RESOURCE`;
- the Auth0 API identifier/audience;
- RFC 9728 Protected Resource Metadata `resource`;
- access-token audience validation.

The implementation must not silently normalize this value into a different
origin or path. Configuration should normalize only trivial trailing-slash
ambiguity and should reject malformed or non-HTTPS production resource URLs.
Localhost HTTP is allowed only in tests and local development.

## Auth0 tenant design

Create a new Auth0 tenant dedicated to this MCP integration.

### API / Resource Server

Create an API named `Google Analytics MCP`.

Identifier:

```text
https://<cloud-run-service>/mcp
```

Signing algorithm:

```text
RS256
```

Required permission/scope:

```text
analytics:read
```

Enable the Auth0 Resource Parameter Compatibility Profile so OAuth clients that
follow RFC 8707 and send `resource=` receive a token for the intended MCP
resource rather than falling back to an unrelated audience.

### Login population

This integration is initially private. The Auth0 tenant must not permit
arbitrary public account creation for the MCP test.

For the initial rollout:

1. create the intended test user in Auth0;
2. disable public sign-ups for the database connection, or otherwise restrict
   tenant login to explicitly approved identities;
3. do not rely on obscurity of the Cloud Run URL as an access control.

An optional subject allowlist may be added later if operational testing shows a
need for defense in depth, but it is not required for the first implementation
because the tenant itself is private and the `analytics:read` scope is already
mandatory.

### Client registration

The MCP server does not implement client registration endpoints.

Auth0 remains authoritative for Authorization Server metadata and client
registration. The initial ChatGPT integration may use whichever registration
path the current Auth0/ChatGPT flow supports, including Auth0's current MCP
client-registration capabilities or manual client configuration if necessary.

The implementation must not depend on DCR-specific or CIMD-specific behavior.
Those are concerns between ChatGPT and Auth0, not between ChatGPT and the MCP
Resource Server.

### Refresh tokens

Auth0 should support the refresh-token behavior required by the client. Where
the current ChatGPT OAuth flow requests `offline_access`, Auth0 should allow it.
The MCP Resource Server never receives or stores a refresh token; it sees only
Bearer access tokens presented to `/mcp`.

## Server configuration

OAuth is controlled explicitly through environment configuration.

```text
MCP_AUTH_MODE=none|auth0
MCP_AUTH_ISSUER=https://<tenant>.auth0.com/
MCP_AUTH_RESOURCE=https://<service>.run.app/mcp
MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

Optional implementation-only configuration may include a JWKS cache lifetime,
but the initial public configuration surface should stay minimal.

### Mode behavior

`MCP_AUTH_MODE=none`

- preserves the current HTTP behavior;
- `/mcp` is not wrapped in Bearer authentication;
- no OAuth metadata route is registered;
- local tests and generic deployments remain compatible.

`MCP_AUTH_MODE=auth0`

- requires issuer, resource, and required scope;
- protects `/mcp`;
- publishes Protected Resource Metadata;
- validates Auth0-issued JWTs;
- fails application startup if required configuration is missing or malformed.

Unknown values fail startup.

## Code boundaries

### `analytics_mcp/auth.py`

New focused module responsible for OAuth Resource Server concerns.

Planned units:

- `AuthConfig`
  - immutable configuration model;
  - parses and validates environment values;
  - represents disabled vs Auth0 mode explicitly.

- `Auth0TokenVerifier`
  - implements `mcp.server.auth.provider.TokenVerifier`;
  - validates JWT signature using Auth0 JWKS;
  - accepts only RS256;
  - validates exact issuer;
  - validates audience against the configured MCP resource;
  - validates token lifetime;
  - extracts OAuth scopes;
  - returns the SDK `AccessToken` model when valid;
  - returns `None` for authentication failures.

- helper(s) for building the SDK `AuthSettings` and protected-resource metadata
  configuration without duplicating protocol logic.

The verifier should use a mature JWT library rather than custom cryptography.
`PyJWT` with its JWK/JWKS support is the preferred implementation dependency
unless implementation testing identifies a concrete incompatibility.

JWKS lookups must be cached by the JWT/JWKS client so normal MCP calls do not
perform an Auth0 network request on every tool invocation.

### `analytics_mcp/http_server.py`

Remains responsible for HTTP transport composition.

When OAuth is disabled, it retains the current route and lifecycle behavior.

When OAuth is enabled, it composes the existing exact `/mcp` ASGI endpoint with
the MCP SDK's authentication building blocks:

- `BearerAuthBackend`;
- Starlette `AuthenticationMiddleware`;
- `AuthContextMiddleware`;
- `RequireAuthMiddleware`;
- `create_protected_resource_routes`;
- `build_resource_metadata_url`.

The project should use the SDK-provided RFC 9728 behavior instead of hand-writing
401 bodies or Protected Resource Metadata JSON.

The exact `/mcp` route must remain a `Route`, not a Starlette `Mount`, so the
previously fixed `/mcp` -> `/mcp/` redirect regression cannot return.

### Tests

Auth-specific tests should live in a separate focused test module, for example:

```text
tests/auth_test.py
```

Existing `tests/http_server_test.py` remains the regression suite for transport
behavior and gains only integration assertions that are specific to HTTP route
composition.

## JWT validation policy

A token is accepted only when all of the following are true:

- the Bearer token is syntactically valid;
- the JWT is signed by a key published by the configured Auth0 tenant;
- the signing algorithm is RS256;
- `iss` exactly matches `MCP_AUTH_ISSUER`;
- `aud` contains or equals `MCP_AUTH_RESOURCE`;
- the token is not expired;
- any time-based validity enforced by the JWT library is satisfied;
- the token contains `analytics:read` in its OAuth `scope` claim.

A token that fails cryptographic or identity validation is treated as invalid and
results in the SDK's 401 path.

A valid token missing the required scope reaches the SDK's authorization gate
and returns 403 `insufficient_scope`.

The JWT verifier must not log raw access tokens.

## HTTP behavior

### Public liveness

```http
GET /healthz
```

Expected:

```text
200 ok
```

This path must not resolve ADC, contact Google APIs, fetch Auth0 JWKS, or require
OAuth.

### OAuth discovery

For resource:

```text
https://example.run.app/mcp
```

publish:

```text
/.well-known/oauth-protected-resource/mcp
```

Expected metadata includes:

```json
{
  "resource": "https://example.run.app/mcp",
  "authorization_servers": [
    "https://tenant.auth0.com/"
  ],
  "scopes_supported": [
    "analytics:read"
  ],
  "bearer_methods_supported": [
    "header"
  ]
}
```

The route should be produced by the MCP SDK's RFC 9728 helpers.

### Unauthenticated MCP request

```http
POST /mcp
```

without `Authorization` returns 401 and a `WWW-Authenticate: Bearer` challenge
that points at the resource metadata URL.

### Invalid token

Malformed, forged, expired, wrong-issuer, or wrong-audience token returns 401.

### Insufficient scope

A valid token without `analytics:read` returns 403 `insufficient_scope`.

### Authorized MCP request

A valid Auth0 token with `analytics:read` proceeds into the existing
Streamable HTTP session manager and preserves the current stateless MCP
semantics.

## Google Analytics identity model

OAuth protects access to the MCP endpoint only.

The server's Google Analytics identity remains unchanged:

```text
Cloud Run service identity / ADC
        |
        v
Google Analytics read-only APIs
```

Every authorized ChatGPT request uses the same server-side Google identity.
There is no per-ChatGPT-user Google credential delegation in this design.

The Auth0 access token must never be forwarded to Google APIs.

## Cloud Run deployment model

The current upstream-oriented guide protects Cloud Run with Google Cloud IAM.
That model cannot be used unchanged for this ChatGPT OAuth integration because
ChatGPT must reach both OAuth discovery and `/mcp` without first possessing a
Google-signed Cloud Run identity token.

For the OAuth integration branch:

- Cloud Run is internet reachable at the platform layer;
- `/healthz` and RFC 9728 metadata are intentionally public;
- `/mcp` is protected by application-layer OAuth;
- the service still runs under a dedicated Google service account for ADC;
- the service account receives only the Google Analytics property access it
  needs;
- no Google service-account key is stored in the image or repository.

Using Cloud Run's `--allow-unauthenticated` for this deployment means the
platform permits requests to reach the application. It does **not** mean MCP
access is unauthenticated; `/mcp` remains closed by the Auth0 Bearer gate.

OAuth-enabled Cloud Run deployment must not occur until tests demonstrate that
missing or malformed auth configuration fails closed.

## Error handling

Startup errors:

- unknown `MCP_AUTH_MODE` -> fail startup;
- Auth0 mode with missing issuer -> fail startup;
- Auth0 mode with missing resource -> fail startup;
- Auth0 mode with missing scope -> fail startup;
- malformed issuer/resource URL -> fail startup.

Request errors:

- missing token -> 401;
- malformed token -> 401;
- unknown signing key -> 401;
- bad signature -> 401;
- wrong issuer -> 401;
- wrong audience -> 401;
- expired token -> 401;
- valid token without required scope -> 403;
- Auth0/JWKS temporary failure -> fail closed rather than bypass auth.

The application should log high-level validation failures without logging the
Bearer token itself.

## TDD strategy

Implementation follows RED -> GREEN -> REFACTOR.

### Configuration tests

1. default auth mode preserves current behavior;
2. Auth0 mode requires issuer;
3. Auth0 mode requires resource;
4. Auth0 mode requires scope;
5. unknown auth mode is rejected;
6. resource and issuer URL validation is deterministic.

### Token verifier tests

Use generated test RSA keys and a local/mock JWKS response. No unit test should
require the real Auth0 tenant.

1. valid RS256 JWT is accepted;
2. forged signature is rejected;
3. unsupported algorithm is rejected;
4. expired token is rejected;
5. wrong issuer is rejected;
6. wrong audience is rejected;
7. scope extraction is correct;
8. token without the required identity claims is rejected;
9. verifier does not require a network call after a cached key is available,
   where this can be asserted reliably without coupling to library internals.

### HTTP integration tests

1. `/healthz` remains public;
2. `/healthz` does not touch Auth0 or Google credentials;
3. OAuth metadata route is present only when auth is enabled;
4. metadata reports the exact resource, issuer, and required scope;
5. `/mcp` without token returns 401;
6. 401 challenge includes `resource_metadata`;
7. invalid token returns 401;
8. valid token without scope returns 403;
9. valid token with scope reaches the MCP protocol;
10. authorized MCP `initialize`, `list_tools`, and `call_tool` still work;
11. stateless operation still returns no MCP session ID;
12. canonical `/mcp` still does not redirect;
13. auth disabled preserves the existing protocol suite unchanged.

### Compatibility verification

Before completion:

- run `nox -s lint`;
- run tests on Python 3.10, 3.11, 3.12, and 3.13 where available;
- run the full transport/auth suite with the normal dependency resolution;
- repeat the relevant suite with `mcp==1.24.0` to prove the declared lower bound;
- build wheel and sdist;
- install the wheel in a clean environment;
- build the Docker image;
- smoke-test `/healthz`, OAuth discovery, unauthenticated `/mcp`, and authorized
  MCP initialization inside the container.

## Auth0 integration verification

After local tests are green, create the dedicated Auth0 tenant and configure the
API/resource server.

The hosted verification sequence is:

1. deploy the OAuth branch to a dedicated Cloud Run service;
2. set the Auth0 API identifier to the exact deployed `/mcp` URL;
3. configure the Cloud Run environment with issuer, resource, and scope;
4. confirm public `/healthz`;
5. confirm RFC 9728 metadata from the deployed service;
6. confirm unauthenticated `/mcp` returns 401;
7. obtain a real Auth0 access token and confirm authorized MCP initialization;
8. connect the URL in ChatGPT Developer Mode with OAuth;
9. complete Auth0 Universal Login;
10. verify ChatGPT discovers the Google Analytics tools;
11. call a low-risk read tool such as account/property discovery;
12. call `run_report` against an authorized Analytics property.

No upstream PR is opened as part of this branch.

## Security invariants

- The transport branch remains unchanged.
- OAuth credentials and tokens never enter git.
- Access tokens are never logged.
- JWT algorithms are allowlisted, never inferred from untrusted token headers.
- JWKS keys are trusted only from the configured Auth0 issuer.
- Audience is bound to the exact MCP resource.
- `analytics:read` is mandatory in Auth0 mode.
- `/mcp` fails closed when authorization cannot be established.
- `/healthz` reveals only process liveness.
- Google Analytics remains read-only.
- Auth0 tokens are never reused as Google tokens.
- Public Cloud Run reachability is acceptable only after the application OAuth
  gate is verified.

## Rollout and rollback

The OAuth feature is opt-in and branch-isolated.

Rollback options are intentionally simple:

- disable or delete the dedicated Cloud Run OAuth test service;
- disable the Auth0 application/API;
- leave `feat/streamable-http-transport` untouched;
- no migration or persistent data rollback is required.

## Success criteria

The feature is complete when all of the following are demonstrated with fresh
verification output:

- configuration fails closed;
- JWT validation is cryptographically correct;
- RFC 9728 discovery works;
- unauthenticated `/mcp` returns 401;
- insufficient scope returns 403;
- authorized Streamable HTTP works without sessions;
- Python 3.10-3.13 tests pass where available;
- the auth path is verified with `mcp==1.24.0`;
- Docker smoke tests pass;
- the hosted Auth0 + Cloud Run path works;
- ChatGPT Developer Mode completes OAuth and discovers the tools;
- at least one real read-only Google Analytics tool call succeeds from ChatGPT.

## References

- MCP Python SDK authorization guide:
  https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/authorization.md
- MCP Python SDK v1.24.0 auth settings and middleware:
  https://github.com/modelcontextprotocol/python-sdk/tree/v1.24.0/src/mcp/server/auth
- MCP authorization specification:
  https://modelcontextprotocol.io/specification/latest/basic/authorization
- Auth0 MCP authorization guidance:
  https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server
- Auth0 Resource Parameter Compatibility Profile guidance:
  https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0
- Auth0 ChatGPT remote MCP integration walkthrough:
  https://auth0.com/blog/add-remote-mcp-server-chatgpt/
