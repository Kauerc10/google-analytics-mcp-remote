# Streamable HTTP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, self-hostable, stateless Streamable HTTP transport to the Google Analytics MCP server without changing the existing stdio workflow.

**Architecture:** Keep `analytics_mcp.coordinator.app` as the single MCP server and add a focused `analytics_mcp/http_server.py` adapter around the MCP SDK 1.x `StreamableHTTPSessionManager`. The HTTP adapter owns CLI parsing, Starlette lifecycle, `/mcp`, `/healthz`, and Uvicorn startup; Google Analytics tools and ADC remain unchanged.

**Tech Stack:** Python 3.10-3.13, MCP Python SDK `>=1.24.0,<2`, Starlette, Uvicorn, httpx, `unittest`, `nox`, Black, Docker/OCI, Google Cloud Run.

**Spec:** `docs/superpowers/specs/2026-08-27-streamable-http-transport-design.md`

## Global Constraints

- Preserve `pipx run analytics-mcp` and `google-analytics-mcp` behavior unchanged.
- Keep `mcp>=1.24.0,<2`; do not introduce an MCP SDK 2.x migration.
- Preserve Python 3.10, 3.11, 3.12, and 3.13 support.
- Keep the transport stateless; no Redis, database, sticky sessions, or event store.
- Continue using existing ADC and `https://www.googleapis.com/auth/analytics.readonly`.
- Do not implement per-user OAuth, token storage, multi-tenancy, or a hosted service.
- Do not move transport concerns into `coordinator.py` or Analytics tool modules.
- Use `unittest`, `nox`, and Black with the repository's existing 80-column limit.
- Tests must not require live Google Analytics credentials or properties.
- Do not enable wildcard CORS by default. Server-to-server MCP clients do not require it, and permissive browser access would unnecessarily widen the attack surface.
- `/healthz` must not resolve ADC or call Google APIs.
- If code imports Starlette or Uvicorn directly, declare them as direct project dependencies rather than relying on MCP's transitive dependency graph.

---

## File Structure

- `analytics_mcp/http_server.py`: HTTP configuration, health handler, Streamable HTTP session manager, Starlette app factory, and Uvicorn CLI entrypoint.
- `tests/http_server_test.py`: configuration, health, lifecycle, protocol, statelessness, and stdio compatibility tests.
- `pyproject.toml`: direct HTTP runtime dependencies and `analytics-mcp-http` console script.
- `Dockerfile`: portable non-root runtime image for the HTTP entrypoint.
- `.dockerignore`: prevent local environments, VCS data, caches, and credentials from entering Docker build context.
- `docs/remote-server.md`: local HTTP, Docker, Cloud Run, ADC, security, client connection, and troubleshooting guide.
- `README.md`: concise discovery link for the remote transport while preserving the local-first README flow.

---

### Task 1: Define HTTP configuration behavior

**Files:**
- Create: `analytics_mcp/http_server.py`
- Create: `tests/http_server_test.py`

**Interfaces:**
- Produces: `HttpServerConfig(host: str, port: int, path: str)`.
- Produces: `parse_http_config(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> HttpServerConfig`.
- Later tasks consume `HttpServerConfig` from `run_http_server()`.

- [ ] **Step 1: Write failing configuration tests**

Create `tests/http_server_test.py` with the repository copyright header and tests equivalent to:

```python
import unittest

from analytics_mcp import http_server


class HttpServerConfigTest(unittest.TestCase):
    def test_defaults_bind_locally(self):
        config = http_server.parse_http_config([], {})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.path, "/mcp")

    def test_port_environment_variable_sets_default_port(self):
        config = http_server.parse_http_config([], {"PORT": "8080"})
        self.assertEqual(config.port, 8080)

    def test_cli_port_overrides_environment(self):
        config = http_server.parse_http_config(
            ["--port", "9000"], {"PORT": "8080"}
        )
        self.assertEqual(config.port, 9000)

    def test_normalizes_path_without_trailing_slash(self):
        config = http_server.parse_http_config(
            ["--path", "/analytics/"], {}
        )
        self.assertEqual(config.path, "/analytics")

    def test_rejects_path_without_leading_slash(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config(["--path", "mcp"], {})

    def test_rejects_out_of_range_port(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config(["--port", "70000"], {})

    def test_rejects_invalid_port_environment_variable(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config([], {"PORT": "not-a-port"})
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```shell
python -m unittest tests.http_server_test.HttpServerConfigTest -v
```

Expected: FAIL because `analytics_mcp.http_server` does not exist yet.

- [ ] **Step 3: Implement minimal configuration parsing**

Create `analytics_mcp/http_server.py` using only standard-library configuration code at this stage:

```python
import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class HttpServerConfig:
    host: str
    port: int
    path: str


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _path(value: str) -> str:
    if not value.startswith("/"):
        raise argparse.ArgumentTypeError("path must start with '/'")
    normalized = value.rstrip("/")
    return normalized or "/"


def parse_http_config(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> HttpServerConfig:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(
        description="Run Google Analytics MCP over Streamable HTTP."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=_port(env.get("PORT", "8000")))
    parser.add_argument("--path", type=_path, default="/mcp")
    args = parser.parse_args(argv)
    return HttpServerConfig(args.host, args.port, args.path)
```

Keep lines Black-compatible at 80 columns; split the `--port` declaration if Black does not do so automatically.

- [ ] **Step 4: Run focused tests and format check**

Run:

```shell
python -m unittest tests.http_server_test.HttpServerConfigTest -v
nox -s lint
```

Expected: configuration tests PASS and Black check exits 0.

- [ ] **Step 5: Commit**

```shell
git add analytics_mcp/http_server.py tests/http_server_test.py
git commit -m "feat(http): define remote server configuration"
```

---

### Task 2: Add the stateless Streamable HTTP ASGI application

**Files:**
- Modify: `analytics_mcp/http_server.py`
- Modify: `tests/http_server_test.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `HttpServerConfig`, `parse_http_config()` from Task 1.
- Produces: `create_http_app(mcp_server: Server = coordinator.app, path: str = "/mcp") -> Starlette`.
- Produces: `run_http_server(argv: Sequence[str] | None = None) -> None`.
- Produces console command: `analytics-mcp-http`.

- [ ] **Step 1: Write failing health and lifecycle tests**

Add imports and tests equivalent to:

```python
from unittest import mock

from starlette.testclient import TestClient


class HttpApplicationTest(unittest.TestCase):
    def test_healthz_does_not_resolve_google_credentials(self):
        app = http_server.create_http_app()
        with mock.patch(
            "analytics_mcp.tools.client._get_credentials"
        ) as credentials:
            with TestClient(app) as client:
                response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        credentials.assert_not_called()

    def test_custom_mcp_path_is_mounted(self):
        app = http_server.create_http_app(path="/analytics")
        with TestClient(app) as client:
            response = client.get("/mcp")
            self.assertEqual(response.status_code, 404)
```

Add a lifecycle test that patches `StreamableHTTPSessionManager.run` with an async context manager and confirms it is entered once when `TestClient` starts and exits once when the client closes.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```shell
python -m unittest tests.http_server_test.HttpApplicationTest -v
```

Expected: FAIL because `create_http_app()` is not implemented.

- [ ] **Step 3: Declare direct HTTP runtime dependencies and script**

Modify `pyproject.toml` dependencies to include direct imports with floors compatible with MCP 1.24:

```toml
"starlette>=0.27",
"uvicorn>=0.31.1",
```

Add without changing the existing scripts:

```toml
analytics-mcp-http = "analytics_mcp.http_server:run_http_server"
```

Do not alter `analytics-mcp` or `google-analytics-mcp`.

- [ ] **Step 4: Implement the ASGI app factory**

Use the SDK's low-level transport pattern from the MCP Python SDK 1.24 stateless example:

```python
import contextlib
from collections.abc import AsyncIterator

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

import analytics_mcp.coordinator as coordinator


def create_http_app(
    mcp_server: Server = coordinator.app,
    path: str = "/mcp",
) -> Starlette:
    normalized_path = _path(path)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=True,
        stateless=True,
    )

    async def handle_mcp(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        await session_manager.handle_request(scope, receive, send)

    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Mount(normalized_path, app=handle_mcp),
        ],
        lifespan=lifespan,
    )
```

Do not add CORS middleware by default. Do not touch `coordinator.py`.

- [ ] **Step 5: Implement Uvicorn startup**

Add:

```python
def run_http_server(argv: Sequence[str] | None = None) -> None:
    config = parse_http_config(argv)
    app = create_http_app(path=config.path)

    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)
```

Keep Uvicorn import near startup if that makes import-only tests lighter, but it remains a direct dependency in `pyproject.toml`.

- [ ] **Step 6: Verify GREEN and console-script compatibility**

Run:

```shell
python -m unittest tests.http_server_test.HttpApplicationTest -v
python -m unittest discover --buffer -s tests -p "*_test.py"
python -m pip install -e .
analytics-mcp-http --help
```

Expected: tests PASS, editable install succeeds, and the new command shows argparse help without resolving Google credentials.

- [ ] **Step 7: Commit**

```shell
git add analytics_mcp/http_server.py tests/http_server_test.py pyproject.toml
git commit -m "feat(http): add stateless Streamable HTTP transport"
```

---

### Task 3: Exercise MCP initialization, tool discovery, and calls over HTTP

**Files:**
- Modify: `tests/http_server_test.py`

**Interfaces:**
- Consumes: `create_http_app(mcp_server, path)` from Task 2.
- Uses MCP SDK client interfaces: `streamable_http_client(...)` and `ClientSession`.
- No production API is added in this task.

- [ ] **Step 1: Add an in-process Streamable HTTP client helper**

Use `httpx.ASGITransport` so tests cross the real MCP HTTP client/server transport without opening a TCP port:

```python
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def open_mcp_session(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    )
```

Do not keep this exact helper if returning an unentered async client makes cleanup awkward. The implementation may instead use nested context managers directly in each test; whichever form is chosen must close the client and enter `app.router.lifespan_context(app)` explicitly because ASGITransport does not own application lifespan.

- [ ] **Step 2: Write a failing initialization and real-tool-discovery test**

Add `unittest.IsolatedAsyncioTestCase` coverage equivalent to:

```python
class StreamableHttpProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_and_lists_google_analytics_tools(self):
        app = http_server.create_http_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, get_session_id):
                    async with ClientSession(
                        read_stream, write_stream
                    ) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()

        names = {tool.name for tool in tools.tools}
        self.assertEqual(
            initialized.serverInfo.name,
            "Google Analytics MCP Server",
        )
        self.assertIn("run_report", names)
        self.assertIn("run_realtime_report", names)
        self.assertIsNone(get_session_id())
```

The final `None` assertion is the explicit statelessness regression check: the server must not issue an MCP session ID.

- [ ] **Step 3: Run the protocol test and confirm RED if transport/path behavior is wrong**

Run:

```shell
python -m unittest \
  tests.http_server_test.StreamableHttpProtocolTest.test_initializes_and_lists_google_analytics_tools \
  -v
```

Expected before any required transport correction: either PASS immediately if Task 2 exactly matches SDK behavior, or FAIL with a concrete protocol/path/lifecycle error. If it passes immediately, keep the test because it is still new regression coverage; do not manufacture a fake failure.

- [ ] **Step 4: Add a mocked tool-call server fixture**

Construct an isolated low-level MCP `Server` in the test module so no Google credentials are touched:

```python
from mcp import types as mcp_types
from mcp.server.lowlevel import Server


def create_test_mcp_server() -> Server:
    server = Server("test-analytics-mcp")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="echo_property",
                description="Echo a property ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "property_id": {"type": "string"},
                    },
                    "required": ["property_id"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return [
            mcp_types.TextContent(
                type="text",
                text=arguments["property_id"],
            )
        ]

    return server
```

- [ ] **Step 5: Add a real HTTP tool-call test**

Use the same in-process client path and assert:

```python
result = await session.call_tool(
    "echo_property", {"property_id": "123456"}
)
self.assertFalse(result.isError)
self.assertEqual(result.content[0].text, "123456")
```

This test must use `create_http_app(mcp_server=create_test_mcp_server())`, not patch the Streamable HTTP manager.

- [ ] **Step 6: Make only transport-level corrections required by the tests**

If the official SDK `Mount` behavior requires the client URL to include a trailing slash, change the ASGI routing so the documented `/mcp` URL works without a redirect. Preserve Streamable HTTP semantics; do not change the public endpoint to `/mcp/` merely to satisfy Starlette defaults.

If no correction is needed, make no production change in this step.

- [ ] **Step 7: Run protocol and complete Python tests**

Run:

```shell
python -m unittest tests.http_server_test.StreamableHttpProtocolTest -v
python -m unittest discover --buffer -s tests -p "*_test.py"
nox -s lint
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit**

```shell
git add tests/http_server_test.py analytics_mcp/http_server.py
git commit -m "test(http): cover MCP protocol over Streamable HTTP"
```

If no production file changed, omit it from `git add`.

---

### Task 4: Protect existing stdio entrypoints from regressions

**Files:**
- Modify: `tests/http_server_test.py`
- Do not modify: `analytics_mcp/server.py` unless a failing compatibility test demonstrates a real need.

**Interfaces:**
- Verifies existing `analytics_mcp.server.run_server` script mapping remains intact.
- Verifies new HTTP entrypoint is additive only.

- [ ] **Step 1: Add packaging compatibility tests**

Use `importlib.metadata` after editable installation to assert all scripts are present:

```python
from importlib import metadata


class ConsoleScriptCompatibilityTest(unittest.TestCase):
    def test_stdio_and_http_console_scripts_are_registered(self):
        scripts = {
            entry.name: entry.value
            for entry in metadata.entry_points(group="console_scripts")
            if entry.name in {
                "analytics-mcp",
                "google-analytics-mcp",
                "analytics-mcp-http",
            }
        }
        self.assertEqual(
            scripts,
            {
                "analytics-mcp": "analytics_mcp.server:run_server",
                "google-analytics-mcp": "analytics_mcp.server:run_server",
                "analytics-mcp-http": (
                    "analytics_mcp.http_server:run_http_server"
                ),
            },
        )
```

- [ ] **Step 2: Run compatibility test**

Run:

```shell
python -m pip install -e .
python -m unittest tests.http_server_test.ConsoleScriptCompatibilityTest -v
```

Expected: PASS. A failure means packaging changed unexpectedly and must be fixed in `pyproject.toml`, not by altering stdio behavior.

- [ ] **Step 3: Run existing test suite**

Run:

```shell
python -m unittest discover --buffer -s tests -p "*_test.py"
```

Expected: PASS with no credentials required.

- [ ] **Step 4: Commit**

```shell
git add tests/http_server_test.py
git commit -m "test(http): preserve existing stdio entrypoints"
```

---

### Task 5: Add a portable, non-root container image

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `analytics-mcp-http` from Task 2.
- Produces: OCI image listening on `0.0.0.0:$PORT`, with default `PORT=8080`.

- [ ] **Step 1: Add `.dockerignore` before building**

Use:

```text
.git
.github
.venv
venv
__pycache__
*.pyc
.pytest_cache
.nox
.coverage
htmlcov
build
dist
*.egg-info
.env
.env.*
*.json
!skills-lock.json
```

The `*.json` exclusion is intentionally defensive against local credential JSON files; `skills-lock.json` is explicitly restored because it is repository content.

- [ ] **Step 2: Add the Dockerfile**

Use a small supported Python runtime and a non-root user:

```dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN addgroup --system analytics-mcp \
    && adduser --system --ingroup analytics-mcp analytics-mcp

COPY . .
RUN pip install --no-cache-dir .

USER analytics-mcp

EXPOSE 8080

CMD ["analytics-mcp-http", "--host", "0.0.0.0"]
```

Do not `COPY` credentials, set `GOOGLE_APPLICATION_CREDENTIALS`, or add a service-account key to the image.

- [ ] **Step 3: Build the image**

Run:

```shell
docker build -t analytics-mcp-http:test .
```

Expected: build exits 0.

If Docker is unavailable in the execution environment, record that limitation and do not claim container verification; still inspect the generated context with `git status --ignored` and verify credential-pattern files are excluded by `.dockerignore`.

- [ ] **Step 4: Run a container smoke test when Docker is available**

Run:

```shell
docker run --rm -d \
  --name analytics-mcp-http-test \
  -p 18080:8080 \
  analytics-mcp-http:test
curl --fail http://127.0.0.1:18080/healthz
docker stop analytics-mcp-http-test
```

Expected: `curl` prints `ok`; no Google credential is required for health.

- [ ] **Step 5: Commit**

```shell
git add Dockerfile .dockerignore
git commit -m "build(container): add remote MCP runtime image"
```

---

### Task 6: Document local HTTP, Docker, Cloud Run, and security

**Files:**
- Create: `docs/remote-server.md`
- Modify: `README.md`

**Interfaces:**
- Documents `analytics-mcp-http`, `/mcp`, `/healthz`, ADC, Docker, and Cloud Run.
- Does not promise Google-hosted service or per-user OAuth.

- [ ] **Step 1: Add a concise README section**

Keep the existing local setup first. Add a section after the current client setup similar to:

```markdown
### Run as a remote MCP server

The server can also expose the same tools over stateless MCP Streamable HTTP:

```shell
analytics-mcp-http
```

The default MCP endpoint is `http://127.0.0.1:8000/mcp`. Remote deployments
continue to use Application Default Credentials and should be protected by the
deployment environment. See [Remote server deployment](docs/remote-server.md)
for Docker, Cloud Run, and security guidance.
```

Do not rewrite unrelated README sections.

- [ ] **Step 2: Write `docs/remote-server.md` local usage**

Document:

```shell
pipx install analytics-mcp
analytics-mcp-http
analytics-mcp-http --host 0.0.0.0 --port 8080 --path /mcp
```

Explain that `PORT` supplies the default port when `--port` is absent and that CLI flags win over the environment.

State explicitly that this mode uses one Google identity for the whole deployment.

- [ ] **Step 3: Document Docker**

Include:

```shell
docker build -t analytics-mcp .
docker run --rm -p 8080:8080 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/credentials/application_default_credentials.json \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/credentials/application_default_credentials.json:ro" \
  analytics-mcp
```

Mark credential-file mounting as a local-development example only. Explain that managed platforms should use workload/service identity instead of long-lived JSON keys.

- [ ] **Step 4: Document Cloud Run using service identity**

Use shell variables rather than unexplained placeholder text:

```shell
export PROJECT_ID="$(gcloud config get-value project)"
export REGION="us-central1"
export SERVICE_NAME="analytics-mcp"
export SERVICE_ACCOUNT="analytics-mcp@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable \
  analyticsadmin.googleapis.com \
  analyticsdata.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com

gcloud iam service-accounts create analytics-mcp \
  --display-name="Google Analytics MCP"

gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated
```

Then explain that the service-account email must be added as a Viewer to the intended Google Analytics account/property.

Make the authentication limitation explicit: Cloud Run IAM-protected ingress works only for MCP clients capable of presenting the required Google identity token. Clients that cannot do so need an authentication layer they support. Do **not** recommend `--allow-unauthenticated` for a server that can read Analytics data merely to make a client connect.

- [ ] **Step 5: Document client-neutral connection and security expectations**

Document endpoint shape:

```text
https://<protected-host>/mcp
```

State:

- Streamable HTTP is client-neutral.
- The server itself does not implement end-user OAuth in this change.
- The deployment must control who can reach the endpoint.
- Do not expose a single-identity Analytics server unauthenticated to the public Internet.
- `/healthz` proves process liveness only; it does not validate Analytics credentials.

Mention Secure MCP Tunnel only as an optional client/platform-specific mechanism if the upstream maintainers accept vendor examples; keep it out of the core instructions if that would make the document vendor-specific.

- [ ] **Step 6: Add troubleshooting entries**

Include concrete cases:

- `DefaultCredentialsError`: configure ADC or service identity.
- HTTP 401/403 before MCP initialization: deployment-layer authentication denied the client.
- HTTP 404: verify the configured `--path`, default `/mcp`.
- Analytics permission denied: confirm the runtime identity is a Viewer on the account/property and APIs are enabled.
- `/healthz` works but tools fail: transport is healthy; investigate ADC/API permissions.

- [ ] **Step 7: Verify documentation commands match code**

Run:

```shell
analytics-mcp-http --help
python -m unittest discover --buffer -s tests -p "*_test.py"
nox -s lint
```

Manually compare the documented defaults to `parse_http_config`: host `127.0.0.1`, port `8000` or `PORT`, path `/mcp`.

- [ ] **Step 8: Commit**

```shell
git add README.md docs/remote-server.md
git commit -m "docs(remote): add self-hosting and Cloud Run guide"
```

---

### Task 7: Run the complete verification matrix and inspect the upstream diff

**Files:**
- Modify only if verification exposes a defect.

**Interfaces:**
- Validates the entire feature against the spec and upstream submission constraints.

- [ ] **Step 1: Run Black exactly as CI does**

```shell
nox -s lint
```

Expected: exit 0.

- [ ] **Step 2: Run every supported Python test environment available locally**

```shell
nox -s tests-3.10 tests-3.11 tests-3.12 tests-3.13
```

If one interpreter is not installed locally, run every available version and rely on GitHub Actions for the full matrix; report the exact missing interpreter rather than claiming full local coverage.

- [ ] **Step 3: Re-run the protocol-focused test after the full matrix**

```shell
python -m unittest tests.http_server_test.StreamableHttpProtocolTest -v
```

Expected: initialization, actual Google Analytics tool discovery, mocked tool invocation, and statelessness all PASS without credentials.

- [ ] **Step 4: Verify packaging from a clean wheel**

```shell
python -m build
python -m venv /tmp/analytics-mcp-wheel-test
/tmp/analytics-mcp-wheel-test/bin/pip install dist/analytics_mcp-*.whl
/tmp/analytics-mcp-wheel-test/bin/analytics-mcp-http --help
```

If `build` is not installed, install it in the verification environment only with `python -m pip install build`; do not add it as a runtime dependency.

Expected: wheel builds and the installed HTTP console script resolves.

- [ ] **Step 5: Rebuild and smoke-test Docker if Docker is available**

```shell
docker build -t analytics-mcp-http:test .
docker run --rm -d \
  --name analytics-mcp-http-test \
  -p 18080:8080 \
  analytics-mcp-http:test
curl --fail http://127.0.0.1:18080/healthz
docker stop analytics-mcp-http-test
```

Expected: image builds and `/healthz` returns `ok`.

- [ ] **Step 6: Inspect branch scope**

Run:

```shell
git diff --stat main...HEAD
git diff main...HEAD -- \
  analytics_mcp pyproject.toml tests Dockerfile .dockerignore README.md docs
```

Confirm line-by-line:

- no Analytics tool logic changed,
- no credential cache changed,
- stdio script mappings remain,
- no OAuth or token storage was introduced,
- no MCP 2.x dependency appeared,
- no generated files, credentials, or unrelated refactors entered the diff.

- [ ] **Step 7: Decide whether Superpowers planning artifacts belong in the upstream PR**

Before opening the upstream PR, compare the contribution value of:

```text
docs/superpowers/specs/2026-08-27-streamable-http-transport-design.md
docs/superpowers/plans/2026-08-27-streamable-http-transport.md
```

Default recommendation: keep them in the fork during development, then remove them from the upstream PR unless maintainers explicitly value internal design/process documents. The user-facing `docs/remote-server.md` remains part of the PR.

- [ ] **Step 8: Prepare upstream PR metadata, but do not submit until the branch is reviewed**

Draft title:

```text
feat: add Streamable HTTP transport
```

Draft summary points:

```markdown
- add an optional stateless Streamable HTTP transport while preserving stdio
- expose the existing Google Analytics tools through a self-hostable ASGI server
- add a portable container and Cloud Run deployment guidance using ADC
- cover MCP initialization, tool discovery, tool calls, and stdio compatibility

Related discussions: #17, #47, #106, #113
```

Do not claim that Google now hosts the server. Do not claim secure public access without a deployment-layer authentication mechanism.

- [ ] **Step 9: Commit any verification-driven fixes separately**

Only if verification required code/documentation changes:

```shell
git add <only-the-files-fixed>
git commit -m "fix(http): address remote transport verification"
```

If nothing changed, do not create an empty cleanup commit.
