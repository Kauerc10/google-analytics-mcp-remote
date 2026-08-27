# Streamable HTTP Transport Design

## Summary

Add an optional remote MCP transport to the Google Analytics MCP server while
preserving the existing stdio workflow and authentication model.

The new transport uses MCP Streamable HTTP in stateless mode, exposes a small
ASGI application suitable for self-hosting, and provides a production container
path that works naturally on Cloud Run and other OCI-compatible platforms.

This change intentionally does not introduce a hosted service, end-user OAuth,
multi-tenant credential handling, or an MCP SDK major-version migration. Those
are separate concerns that should remain independently reviewable.

## Motivation

The current server is local-only and communicates over stdio. That works well
for clients that can spawn local processes, but prevents direct use from remote
MCP clients and hosted agent environments.

Remote transport requests have already appeared in upstream discussions,
including issues #17, #47, #106, and #113. A self-hostable Streamable HTTP
transport addresses that interoperability gap without requiring the project to
operate a managed service.

The design should satisfy the following goals:

- Preserve `pipx run analytics-mcp` behavior without user-visible changes.
- Reuse the existing MCP `Server` and tool registrations without duplication.
- Support MCP clients that connect through Streamable HTTP.
- Remain deployable as a single stateless container.
- Scale horizontally without shared session state.
- Continue using Google Application Default Credentials (ADC).
- Keep configuration small and predictable.
- Follow the repository's Python, testing, formatting, and CI conventions.

## Non-goals

This change does not attempt to:

- Provide a Google-hosted or project-hosted public MCP service.
- Implement per-user Google OAuth or refresh-token storage.
- Support multi-tenant credentials within a single server process.
- Replace stdio or make HTTP the default transport.
- Migrate the project from MCP Python SDK 1.x to 2.x.
- Add Redis, databases, external session stores, or service discovery.
- Add authorization logic beyond the deployment platform's existing controls.
- Change Google Analytics tool behavior or schemas except where required for
  transport compatibility.

## Current Architecture

The existing implementation has a useful separation already:

- `analytics_mcp/coordinator.py` owns the singleton MCP `Server`, tool
  registration, tool discovery, and tool execution.
- `analytics_mcp/server.py` adapts that MCP server to stdio.
- Google Analytics API clients obtain read-only credentials using ADC.

The HTTP implementation should preserve those boundaries. Transport-specific
code must not move into `coordinator.py` or the Analytics tool modules.

## Proposed Architecture

The project will expose two transport entrypoints backed by the same MCP
server:

```text
                         coordinator.app
                               |
                 +-------------+-------------+
                 |                           |
              stdio                     Streamable HTTP
                 |                           |
       analytics-mcp command       analytics-mcp-http command
                 |                           |
       local MCP clients          remote MCP clients / HTTPS
```

`analytics-mcp` remains the existing stdio command.

A new `analytics-mcp-http` command starts an ASGI server that exposes the MCP
endpoint. Both commands use the same `coordinator.app`, so tool definitions and
Google Analytics behavior remain single-sourced.

## HTTP Transport

### Protocol

Use MCP Streamable HTTP from the currently supported MCP Python SDK 1.x range.
The project already depends on `mcp>=1.24.0,<2`, and SDK 1.24 provides the
required Streamable HTTP session manager and ASGI-compatible transport.

No SDK major-version upgrade is required for this feature.

### Stateless mode

The HTTP transport should run with stateless MCP sessions.

Google Analytics tools are request-oriented: their output depends on explicit
arguments plus Google credentials available to the process. They do not need
MCP session state between requests.

Stateless mode provides important deployment properties:

- Requests can land on any replica.
- No session affinity is required.
- No shared state store is required.
- Instances can scale to zero and restart independently.
- The container remains compatible with ordinary load balancers.

### Endpoint

Default MCP path:

```text
/mcp
```

A lightweight health endpoint should also be exposed:

```text
/healthz
```

`/healthz` verifies that the HTTP process is alive and able to serve requests.
It must not call Google Analytics APIs or require Analytics credentials, so
platform health checks cannot create API traffic or fail solely because a
credential refresh is temporarily unavailable.

### Response behavior

Prefer JSON responses when supported by the SDK's Streamable HTTP transport.
The server should still conform to MCP Streamable HTTP negotiation and should
not invent a proprietary protocol wrapper.

## Server Configuration

The HTTP entrypoint should use a deliberately small configuration surface.

Expected command:

```shell
analytics-mcp-http --host 0.0.0.0 --port 8080 --path /mcp
```

Defaults:

- `host`: `127.0.0.1`
- `port`: `8000`, unless the `PORT` environment variable is set
- `path`: `/mcp`

Binding to localhost by default prevents accidental network exposure during
local development. Container and Cloud Run examples explicitly bind to
`0.0.0.0`.

Configuration validation should fail fast with an actionable error when values
are invalid. The implementation should avoid introducing a new CLI framework
unless the standard library becomes insufficient.

## ASGI Application Boundary

Create a focused HTTP module, expected to be
`analytics_mcp/http_server.py`, responsible only for:

- parsing HTTP server configuration,
- constructing the Streamable HTTP session manager,
- constructing the ASGI application,
- exposing `/mcp` and `/healthz`,
- starting the ASGI server from the CLI entrypoint.

It must not contain Analytics tool definitions, credential policy, or business
logic.

Where practical, application construction should be separated from process
startup so tests can instantiate the ASGI app without opening a real network
port.

## Authentication and Security

### Google credentials

Continue using the existing ADC flow and the read-only Analytics scope:

```text
https://www.googleapis.com/auth/analytics.readonly
```

This preserves local credential files, workload identity, service accounts,
and Cloud Run service identities without adding transport-specific credential
logic.

The HTTP server is therefore a single-identity deployment: all MCP requests use
the Google identity configured for that process.

### Multi-user OAuth

Per-user Google OAuth is explicitly out of scope.

The existing global credential cache is suitable for a single-identity
self-hosted server, but it is not a valid basis for a multi-tenant hosted
service. A future OAuth design must make Google credentials request-scoped and
must address authorization, token refresh, token storage, and MCP authorization
semantics independently.

### Network access

The application should not pretend that transport availability is equivalent
to authorization. Operators remain responsible for restricting access using
their deployment environment, such as authenticated ingress, identity-aware
proxies, private networking, or other platform controls.

Documentation must warn users not to expose an unauthenticated deployment to
the public Internet when it can access Analytics data.

### Secrets

No credential file, OAuth secret, API token, or service-account key is copied
into the container image or committed to the repository.

Cloud Run documentation should prefer workload identity / attached service
identity over service-account key files.

## Containerization

Add a production-oriented `Dockerfile` that:

- uses an official slim Python runtime compatible with the project's supported
  Python versions,
- installs the package from the repository,
- runs the HTTP entrypoint,
- binds to `0.0.0.0`,
- honors the `PORT` environment variable,
- contains no credentials,
- avoids unnecessary build tooling in the runtime image where practical.

The image must remain platform-neutral. Cloud Run is a documented deployment
target, not a hard dependency of the container.

## Cloud Run Deployment

Documentation should include a minimal self-hosting path for Cloud Run because
it naturally supports containerized Streamable HTTP workloads and ADC through
the service identity.

The guide should cover:

1. enabling the Google Analytics Admin and Data APIs,
2. selecting or creating a service identity,
3. granting that identity read access to the required Analytics properties,
4. building/deploying the container,
5. choosing an ingress/authentication policy appropriate to the client,
6. verifying `/healthz`,
7. connecting an MCP client to `/mcp`.

The guide must not require downloading a long-lived service-account key when
Cloud Run workload credentials are available.

## Compatibility

### Stdio

The existing stdio behavior is a compatibility contract for this change.

The following command must continue working unchanged:

```shell
pipx run analytics-mcp
```

The existing `google-analytics-mcp` compatibility script should also remain
unchanged.

### MCP SDK

Remain on the repository's existing MCP Python SDK major-version constraint:

```text
mcp>=1.24.0,<2
```

Any future SDK 2.x migration should be handled independently so transport
support is not coupled to a major dependency migration.

### Python

Preserve the current Python support matrix:

- Python 3.10
- Python 3.11
- Python 3.12
- Python 3.13

## Error Handling and Observability

The HTTP server should:

- fail startup clearly on invalid host/port/path configuration,
- preserve MCP protocol errors instead of converting them into custom formats,
- log startup and fatal transport errors to stderr or the normal logging
  stream,
- avoid logging credentials, authorization headers, tokens, or Analytics
  response data by default,
- allow infrastructure logs to identify transport failures without exposing
  sensitive request payloads.

Health checks should return conventional HTTP status codes and minimal bodies.

## Testing Strategy

Follow the repository's current `unittest` and `nox` conventions.

Development should proceed test-first where behavior is new.

### Unit and integration coverage

Add tests for at least:

1. HTTP configuration defaults.
2. `PORT` environment variable handling.
3. Invalid configuration failures.
4. `/healthz` returning success without Analytics credentials.
5. MCP initialization over Streamable HTTP.
6. MCP tool discovery over Streamable HTTP.
7. A mocked tool invocation through the HTTP transport.
8. Stateless request behavior.
9. Existing stdio entrypoint compatibility at the import/configuration level.
10. ASGI application lifecycle and shutdown behavior.

At least one test must exercise the real Streamable HTTP stack in-process rather
than mocking the MCP transport itself.

Tests must not require live Google Analytics accounts or credentials.

### CI

Existing presubmit behavior remains the source of truth:

- Black formatting with 80-column line length.
- Tests on Python 3.10 through 3.13.
- `nox -s lint`.
- `nox -s tests-<python-version>`.

No new CI platform or parallel test framework should be introduced for this
feature.

## Documentation

The README should remain concise and continue leading with the current local
setup.

Add a short remote-server section that explains the capability and links to a
focused deployment guide rather than turning the README into a Cloud Run
manual.

The focused guide should cover:

- local HTTP execution,
- Docker execution,
- Cloud Run deployment,
- security expectations,
- ADC behavior,
- example MCP endpoint configuration,
- troubleshooting common startup and credential failures.

References to specific MCP clients should be examples, not implementation
requirements. The transport should remain client-neutral.

## Proposed File Changes

Expected implementation surface:

```text
analytics_mcp/
├── coordinator.py          # unchanged unless initialization reuse requires it
├── server.py               # existing stdio transport, preserved
└── http_server.py          # new HTTP transport and ASGI app

tests/
└── http_server_test.py     # new transport coverage

docs/
└── remote-server.md        # operator and deployment guide

Dockerfile                  # portable HTTP server image
README.md                   # concise remote transport entry point
pyproject.toml              # new HTTP console script if required
```

Unrelated refactors should not be included.

## Implementation Sequence

The implementation should be divided into reviewable commits that tell a clear
story. The exact boundaries may change based on test feedback, but the expected
sequence is:

1. Add failing tests defining HTTP configuration and application behavior.
2. Add the stateless Streamable HTTP application and CLI entrypoint.
3. Add protocol-level tests for initialization, discovery, and tool calls.
4. Add the portable container definition and container-focused checks.
5. Add remote-server documentation and README navigation.
6. Run the complete supported-version test and formatting matrix.

Commit messages should follow the repository's existing concise conventional
style and describe the intent rather than implementation trivia.

## Upstream Submission Strategy

Keep the upstream proposal focused on self-hostable transport support.

The pull request should emphasize that it:

- responds to existing remote-MCP and installation-friction requests,
- does not require Google to host a service,
- preserves all current local workflows,
- reuses the project's current MCP SDK major version,
- does not introduce multi-user authentication complexity,
- remains useful to any Streamable HTTP MCP client rather than targeting one
  vendor.

The PR description should reference relevant upstream discussions, especially
#17, #47, #106, and #113.

## Acceptance Criteria

The feature is complete when all of the following are true:

- Existing stdio commands behave as before.
- A documented command starts a Streamable HTTP MCP server.
- The default MCP endpoint is `/mcp`.
- The server operates statelessly and does not require sticky sessions.
- `/healthz` works without contacting Google APIs.
- An in-process MCP client can initialize, list tools, and invoke a mocked tool
  over the HTTP transport.
- The Docker image runs the HTTP server without embedding credentials.
- Cloud Run deployment can use ADC through a service identity.
- Security documentation clearly warns against unintentionally exposing
  Analytics access.
- Tests and formatting pass on the repository's supported Python matrix.
- No OAuth, multi-tenancy, SDK 2.x migration, or unrelated tool refactor is
  included in the change.
