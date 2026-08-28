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
- Validate issuer, audience/resource, signature, and token lifetime.
- Enforce the required scope through the MCP SDK authorization gate.
- Preserve the existing stateless Streamable HTTP behavior and tool schemas.
- Preserve the existing stdio entrypoints.
- Preserve existing HTTP behavior when OAuth is explicitly disabled.
- Fail closed when OAuth is enabled but misconfigured.
- Remain compatible with the current lower bound `mcp>=1.24.0,<2`.
- Deploy to Cloud Run so ChatGPT can reach OAuth discovery and `/mcp` without a
  Google Cloud IAM identity token after application OAuth is verified.

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
        | OAuth authorization flow + PKCE
        | resource=<public MCP URL>
        v
Auth0 Authorization Server
        |
        | signed JWT access token
        v
Google Analytics MCP Resource Server
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

The implementation must not silently convert this value to a different origin
or path. It may normalize only trivial trailing-slash ambiguity. Production
resource URLs must use HTTPS. Localhost HTTP is allowed only for tests and local
development.

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

Enable the Auth0 Resource Parameter Compatibility Profile so clients following
RFC 8707 and sending `resource=` receive a token for the intended MCP resource.

### Login population

This integration is initially private. The tenant must not permit arbitrary
public account creation for the MCP test.

For the initial rollout:

1. create the intended test user in Auth0;
2. disable public sign-ups for the database connection, or otherwise restrict
   tenant login to explicitly approved identities;
3. do not use obscurity of the Cloud Run URL as access control.

A subject allowlist can be added later if operational testing shows a concrete
need. It is not part of the first implementation.

### Client registration

The MCP server does not implement client registration endpoints.

Auth0 remains authoritative for Authorization Server metadata and client
registration. The ChatGPT integration may use whichever registration path the
current Auth0/ChatGPT flow supports, including Auth0 MCP client-registration
features or manual client configuration when necessary.

The Resource Server implementation does not depend on DCR-specific or
CIMD-specific behavior. Those concerns are between ChatGPT and Auth0.

### Refresh tokens

Auth0 should support the refresh-token behavior required by ChatGPT. Where the
client requests `offline_access`, Auth0 should allow it. The MCP Resource Server
never receives or stores refresh tokens; it sees only Bearer access tokens sent
to `/mcp`.

## Server configuration

OAuth is controlled explicitly through environment configuration.

```text
MCP_AUTH_MODE=none|auth0
MCP_AUTH_ISSUER=https://<tenant>.auth0.com/
MCP_AUTH_RESOURCE=https://<service>.run.app/mcp
MCP_AUTH_REQUIRED_SCOPE=analytics:read
```

The initial public configuration surface stays deliberately small.

### `MCP_AUTH_MODE=none`

- preserves current HTTP behavior;
- `/mcp` is not wrapped in Bearer authentication;
- no OAuth metadata route is registered;
- local and generic deployments remain compatible.

### `MCP_AUTH_MODE=auth0`

- requires issuer, resource, and required scope;
- protects `/mcp`;
- publishes Protected Resource Metadata;
- validates Auth0-issued JWTs;
- fails startup if required configuration is missing or malformed.

Unknown auth modes fail startup.

## Code boundaries

### `analytics_mcp/auth.py`

New focused module for OAuth Resource Server concerns.

Planned units:

- `AuthConfig`
  - immutable configuration model;
  - parses and validates environment values;
  - represents disabled vs Auth0 mode explicitly.

- `Auth0TokenVerifier`
  - implements `mcp.server.auth.provider.TokenVerifier`;
  - validates JWT signature against Auth0 JWKS;
  - accepts only RS256;
  - validates exact issuer;
  - validates audience against the configured MCP resource;
  - validates token lifetime;
  - extracts OAuth scopes without enforcing them itself;
  - returns the SDK `AccessToken` model when identity validation succeeds;
  - returns `None` for authentication failures.

- helper(s) for building SDK `AuthSettings` and protected-resource metadata
  configuration without duplicating protocol logic.

The verifier uses a mature JWT library rather than custom cryptography. `PyJWT`
with JWK/JWKS support is preferred unless implementation testing identifies a
concrete incompatibility.

JWKS lookups must be cached by the JWT/JWKS client so normal MCP calls do not
perform an Auth0 network request on every tool invocation. Any synchronous JWKS
work must not block the async server event loop.

### `analytics_mcp/http_server.py`

Remains responsible for HTTP transport composition.

When OAuth is disabled, current route and lifecycle behavior remains unchanged.

When OAuth is enabled, compose the existing exact `/mcp` ASGI endpoint with MCP
SDK authentication components available in the supported v1.x SDK, including:

- `BearerAuthBackend`;
- Starlette `AuthenticationMiddleware`;
- `AuthContextMiddleware`;
- `RequireAuthMiddleware`;
- `create_protected_resource_routes`;
- `build_resource_metadata_url`.

Use SDK-provided RFC 9728 behavior instead of hand-writing 401 bodies or
Protected Resource Metadata JSON.

The exact `/mcp` endpoint remains a Starlette `Route`, not a `Mount`, so the
previously fixed `/mcp` to `/mcp/` redirect regression cannot return.

### Tests

Auth-specific tests live in a focused module such as:

```text
tests/auth_test.py
```

Existing `tests/http_server_test.py` remains the transport regression suite and
gains only route-composition integration assertions.

## JWT validation policy

The verifier accepts an identity only when all of the following are true:

- the Bearer value is a syntactically valid JWT;
- the JWT is signed by a key published by the configured Auth0 tenant;
- the signing algorithm is RS256;
- `iss` exactly matches `MCP_AUTH_ISSUER`;
- `aud` contains or equals `MCP_AUTH_RESOURCE`;
- the token is not expired;
- other time validity enforced by the JWT library succeeds;
- required identity claims needed to construct the SDK `AccessToken` are
  present.

The verifier extracts the OAuth `scope` claim and returns those scopes with the
SDK `AccessToken`. It does **not** reject an otherwise valid token merely because
`analytics:read` is absent. `RequireAuthMiddleware` is responsible for scope
authorization so that a valid token with insufficient permissions returns 403,
not 401.

A cryptographically or semantically invalid token returns `None` from the
verifier and follows the SDK 401 path.

Raw access tokens must never be logged.

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

The route is produced by MCP SDK RFC 9728 helpers.

### Unauthenticated MCP request

```http
POST /mcp
```

without `Authorization` returns 401 and a `WWW-Authenticate: Bearer` challenge
pointing at the Protected Resource Metadata URL.

### Invalid token

Malformed, forged, expired, wrong-issuer, or wrong-audience tokens return 401.

### Insufficient scope

A cryptographically valid token without `analytics:read` returns 403
`insufficient_scope`.

### Authorized MCP request

A valid Auth0 token containing `analytics:read` proceeds into the existing
Streamable HTTP session manager and preserves stateless MCP semantics.

## Google Analytics identity model

OAuth protects access to the MCP endpoint only.

The downstream Google identity remains:

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

The upstream-oriented remote-server guide protects Cloud Run with Google Cloud
IAM. That cannot remain the final access layer for the ChatGPT integration,
because ChatGPT must eventually reach OAuth discovery and `/mcp` without a
Google-signed Cloud Run identity token.

The deployment is therefore bootstrapped in two phases to avoid any interval
where an unprotected MCP endpoint is publicly reachable.

### Phase 1: private bootstrap

1. Deploy the OAuth branch to a dedicated Cloud Run service with
   `--no-allow-unauthenticated`.
2. Obtain the service's stable public URL while Google Cloud IAM still protects
   all incoming requests.
3. Configure the Auth0 API identifier to the exact `<service-url>/mcp` value.
4. Configure `MCP_AUTH_MODE=auth0`, issuer, resource, and scope on Cloud Run.
5. Redeploy while the Cloud Run IAM gate remains enabled.
6. Use an authenticated operator path such as the Cloud Run proxy to verify the
   application itself returns:
   - public-app `/healthz` behavior;
   - RFC 9728 metadata;
   - 401 from `/mcp` without an Auth0 token;
   - successful MCP initialization with a valid Auth0 token.

### Phase 2: ChatGPT reachability

Only after Phase 1 succeeds:

1. change the Cloud Run invoker policy so the Internet can reach the
   application;
2. verify `/mcp` still returns 401 without an Auth0 token from an ordinary
   unauthenticated network request;
3. verify metadata remains public;
4. connect ChatGPT Developer Mode through OAuth.

At this point Cloud Run permits requests to reach the application, but the MCP
resource itself remains authenticated by Auth0.

The service continues to run under a dedicated Google service account for ADC.
No Google service-account key is copied into the repository or image.

## Error handling

Startup failures:

- unknown `MCP_AUTH_MODE` -> fail startup;
- Auth0 mode with missing issuer -> fail startup;
- Auth0 mode with missing resource -> fail startup;
- Auth0 mode with missing scope -> fail startup;
- malformed issuer/resource URL -> fail startup.

Request failures:

- missing token -> 401;
- malformed token -> 401;
- unknown signing key -> 401;
- bad signature -> 401;
- wrong issuer -> 401;
- wrong audience -> 401;
- expired token -> 401;
- valid token without required scope -> 403;
- Auth0/JWKS temporary failure -> fail closed rather than bypass auth.

Log high-level validation failure categories only. Never log the Bearer token.

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

Use generated test RSA keys and a local/mock JWKS response. Unit tests must not
require the real Auth0 tenant.

1. valid RS256 JWT is accepted;
2. forged signature is rejected;
3. unsupported algorithm is rejected;
4. expired token is rejected;
5. wrong issuer is rejected;
6. wrong audience is rejected;
7. OAuth scopes are extracted correctly;
8. valid token without `analytics:read` remains an authenticated identity so the
   HTTP authorization layer can return 403;
9. token without required identity claims is rejected;
10. repeated verification can use cached JWKS material where this can be tested
    without coupling to private library internals.

### HTTP integration tests

1. `/healthz` remains public;
2. `/healthz` does not touch Auth0 or Google credentials;
3. OAuth metadata route exists only when auth is enabled;
4. metadata reports exact resource, issuer, and required scope;
5. `/mcp` without token returns 401;
6. 401 challenge includes `resource_metadata`;
7. invalid token returns 401;
8. valid token without scope returns 403;
9. valid token with scope reaches the MCP protocol;
10. authorized MCP `initialize`, `list_tools`, and `call_tool` work;
11. stateless operation still returns no MCP session ID;
12. canonical `/mcp` does not redirect;
13. auth disabled preserves the existing protocol suite unchanged.

### Compatibility verification

Before completion:

- run `nox -s lint`;
- run tests on Python 3.10, 3.11, 3.12, and 3.13 where available;
- run the full transport/auth suite with normal dependency resolution;
- repeat the relevant suite with `mcp==1.24.0` to prove the declared lower bound;
- build wheel and sdist;
- install the wheel in a clean environment;
- build the Docker image;
- smoke-test `/healthz`, OAuth discovery, unauthenticated `/mcp`, and authorized
  MCP initialization inside the container.

## Hosted Auth0 integration verification

After local tests are green:

1. create the dedicated Auth0 tenant;
2. restrict its user population;
3. enable the Resource Parameter Compatibility Profile;
4. perform the private Cloud Run bootstrap described above;
5. configure the Auth0 API with the exact deployed resource URL;
6. obtain a real Auth0 access token and verify the protected MCP endpoint;
7. expose the application at the Cloud Run platform layer only after the Auth0
   gate has been demonstrated;
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
- JWT algorithms are allowlisted, never inferred from untrusted headers.
- JWKS keys are trusted only from the configured Auth0 issuer.
- Audience is bound to the exact MCP resource.
- `analytics:read` is enforced by the MCP authorization gate.
- `/mcp` fails closed when authentication cannot be established.
- `/healthz` reveals only process liveness.
- Google Analytics remains read-only.
- Auth0 tokens are never reused as Google tokens.
- Cloud Run becomes publicly reachable only after the application OAuth gate is
  verified while still behind IAM.

## Rollout and rollback

The OAuth feature is opt-in and branch-isolated.

Rollback options:

- restore the Cloud Run IAM gate or delete the dedicated OAuth test service;
- disable the Auth0 application/API;
- leave `feat/streamable-http-transport` untouched;
- no migration or persistent-data rollback is required.

## Success criteria

The feature is complete only when fresh verification demonstrates:

- configuration fails closed;
- JWT validation is cryptographically correct;
- RFC 9728 discovery works;
- unauthenticated `/mcp` returns 401;
- insufficient scope returns 403;
- authorized Streamable HTTP works without sessions;
- Python 3.10-3.13 tests pass where available;
- the auth path works with `mcp==1.24.0`;
- packaging and Docker smoke tests pass;
- hosted Auth0 + Cloud Run works;
- ChatGPT Developer Mode completes OAuth and discovers the tools;
- at least one real read-only Google Analytics tool call succeeds from ChatGPT.

## References

- MCP Python SDK authorization guide:
  https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/authorization.md
- MCP Python SDK v1.24.0 authentication implementation:
  https://github.com/modelcontextprotocol/python-sdk/tree/v1.24.0/src/mcp/server/auth
- MCP authorization specification:
  https://modelcontextprotocol.io/specification/latest/basic/authorization
- Auth0 MCP authorization guidance:
  https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server
- Auth0 Resource Parameter Compatibility Profile guidance:
  https://support.auth0.com/center/s/article/mcp-audience-error-with-auth0
- Auth0 ChatGPT remote MCP integration walkthrough:
  https://auth0.com/blog/add-remote-mcp-server-chatgpt/
