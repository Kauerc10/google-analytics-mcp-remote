# Streamable HTTP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, self-hostable, stateless Streamable HTTP transport to the Google Analytics MCP server without changing the existing stdio workflow.

**Architecture:** Keep `analytics_mcp.coordinator.app` as the single MCP server and add `analytics_mcp/http_server.py` as a transport adapter around MCP Python SDK 1.x `StreamableHTTPSessionManager`. The adapter owns CLI configuration, Starlette lifecycle, `/mcp`, `/healthz`, and Uvicorn startup; Analytics tools and ADC stay unchanged.

**Tech Stack:** Python 3.10-3.13, MCP Python SDK `>=1.24.0,<2`, Starlette, Uvicorn, httpx, `unittest`, `nox`, Black, Docker/OCI, Google Cloud Run.

**Spec:** `docs/superpowers/specs/2026-08-27-streamable-http-transport-design.md`

## Global Constraints

- Preserve `pipx run analytics-mcp` and `google-analytics-mcp` unchanged.
- Keep `mcp>=1.24.0,<2`; no MCP SDK 2.x migration.
- Preserve Python 3.10, 3.11, 3.12, and 3.13 support.
- Use stateless Streamable HTTP with no event store, Redis, database, or sticky sessions.
- Continue using existing ADC with `https://www.googleapis.com/auth/analytics.readonly`.
- Do not implement end-user OAuth, refresh-token storage, multi-tenancy, or a hosted service.
- Do not move transport logic into `coordinator.py` or Analytics tool modules.
- Use the repository's `unittest`, `nox`, and Black 80-column conventions.
- Tests must not require live Google Analytics credentials or properties.
- `/healthz` must not resolve ADC or call Google APIs.
- Do not add wildcard CORS by default.
- Declare Starlette and Uvicorn as direct dependencies because the new module imports them directly.
- Do not recommend unauthenticated public deployment of a server that can read Analytics data.

---

## File Structure

- `analytics_mcp/http_server.py`: configuration, Streamable HTTP manager, health handler, ASGI app factory, Uvicorn entrypoint.
- `tests/http_server_test.py`: configuration, health, lifecycle, MCP protocol, statelessness, and packaging compatibility.
- `pyproject.toml`: direct HTTP dependencies and `analytics-mcp-http` console script.
- `Dockerfile`: portable non-root HTTP runtime image.
- `.dockerignore`: exclude VCS data, environments, caches, secrets, and local credential JSON files.
- `docs/remote-server.md`: local HTTP, Docker, Cloud Run, ADC, security, client connection, troubleshooting.
- `README.md`: short remote-server entry point that leaves the existing local setup first.

---

### Task 1: Define HTTP configuration

**Files:**
- Create: `analytics_mcp/http_server.py`
- Create: `tests/http_server_test.py`

**Interfaces:**
- Produces: `HttpServerConfig(host: str, port: int, path: str)`.
- Produces: `parse_http_config(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> HttpServerConfig`.

- [ ] **Step 1: Write the failing configuration tests**

Create `tests/http_server_test.py` with the repository license header and:

```python
import unittest

from analytics_mcp import http_server


class HttpServerConfigTest(unittest.TestCase):
    def test_defaults_bind_locally(self):
        config = http_server.parse_http_config([], {})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.path, "/mcp")

    def test_port_environment_variable_sets_default(self):
        config = http_server.parse_http_config([], {"PORT": "8080"})
        self.assertEqual(config.port, 8080)

    def test_cli_port_overrides_environment(self):
        config = http_server.parse_http_config(
            ["--port", "9000"], {"PORT": "8080"}
        )
        self.assertEqual(config.port, 9000)

    def test_normalizes_trailing_path_slash(self):
        config = http_server.parse_http_config(
            ["--path", "/analytics/"], {}
        )
        self.assertEqual(config.path, "/analytics")

    def test_rejects_path_without_leading_slash(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config(["--path", "mcp"], {})

    def test_rejects_out_of_range_cli_port(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config(["--port", "70000"], {})

    def test_rejects_invalid_port_environment_variable(self):
        with self.assertRaises(SystemExit):
            http_server.parse_http_config([], {"PORT": "not-a-port"})
```

- [ ] **Step 2: Run RED**

```shell
python -m unittest tests.http_server_test.HttpServerConfigTest -v
```

Expected: FAIL because `analytics_mcp.http_server` does not exist.

- [ ] **Step 3: Implement the minimal configuration parser**

Create `analytics_mcp/http_server.py`:

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
        raise argparse.ArgumentTypeError(
            "port must be between 1 and 65535"
        )
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
    try:
        default_port = _port(env.get("PORT", "8000"))
    except argparse.ArgumentTypeError as exc:
        parser.error(f"invalid PORT: {exc}")

    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=default_port)
    parser.add_argument("--path", type=_path, default="/mcp")
    args = parser.parse_args(argv)
    return HttpServerConfig(args.host, args.port, args.path)
```

- [ ] **Step 4: Run GREEN and formatting**

```shell
python -m unittest tests.http_server_test.HttpServerConfigTest -v
nox -s lint
```

Expected: both commands exit 0.

- [ ] **Step 5: Commit**

```shell
git add analytics_mcp/http_server.py tests/http_server_test.py
git commit -m "feat(http): define remote server configuration"
```

---

### Task 2: Add the stateless Streamable HTTP application

**Files:**
- Modify: `analytics_mcp/http_server.py`
- Modify: `tests/http_server_test.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `HttpServerConfig`, `parse_http_config()`.
- Produces: `create_http_app(mcp_server: Server = coordinator.app, path: str = "/mcp") -> Starlette`.
- Produces: `run_http_server(argv: Sequence[str] | None = None) -> None`.
- Produces console script: `analytics-mcp-http`.

- [ ] **Step 1: Write failing health and lifecycle tests**

Add:

```python
import contextlib
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

    def test_custom_mcp_path_replaces_default_path(self):
        app = http_server.create_http_app(path="/analytics")
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/mcp")
        self.assertEqual(response.status_code, 404)

    def test_lifespan_starts_and_stops_session_manager(self):
        events = []

        @contextlib.asynccontextmanager
        async def fake_run(_manager):
            events.append("start")
            try:
                yield
            finally:
                events.append("stop")

        with mock.patch.object(
            http_server.StreamableHTTPSessionManager,
            "run",
            new=fake_run,
        ):
            app = http_server.create_http_app()
            with TestClient(app):
                self.assertEqual(events, ["start"])

        self.assertEqual(events, ["start", "stop"])
```

- [ ] **Step 2: Write a failing real-protocol test before implementation**

Add these imports and fixture:

```python
import httpx
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
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


class StreamableHttpProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_lists_and_calls_tool(self):
        app = http_server.create_http_app(
            mcp_server=create_test_mcp_server()
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=True,
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
                        result = await session.call_tool(
                            "echo_property",
                            {"property_id": "123456"},
                        )

        self.assertEqual(initialized.serverInfo.name, "test-analytics-mcp")
        self.assertEqual([tool.name for tool in tools.tools], ["echo_property"])
        self.assertFalse(result.isError)
        self.assertEqual(result.content[0].text, "123456")
        self.assertIsNone(get_session_id())
```

The `get_session_id()` assertion is the statelessness regression check.

- [ ] **Step 3: Run RED**

```shell
python -m unittest \
  tests.http_server_test.HttpApplicationTest \
  tests.http_server_test.StreamableHttpProtocolTest \
  -v
```

Expected: FAIL because `create_http_app()` is not implemented.

- [ ] **Step 4: Declare direct dependencies and console script**

Add to `[project].dependencies` without changing the existing MCP constraint:

```toml
"starlette>=0.27",
"uvicorn>=0.31.1",
```

Add to `[project.scripts]` without modifying the two existing entries:

```toml
analytics-mcp-http = "analytics_mcp.http_server:run_http_server"
```

- [ ] **Step 5: Implement the ASGI app**

Add to `analytics_mcp/http_server.py`:

```python
import contextlib
from collections.abc import AsyncIterator

import analytics_mcp.coordinator as coordinator
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send


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

Do not add CORS middleware. Do not modify `coordinator.py`.

- [ ] **Step 6: Implement Uvicorn startup**

Add:

```python
def run_http_server(argv: Sequence[str] | None = None) -> None:
    config = parse_http_config(argv)
    app = create_http_app(path=config.path)

    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)
```

- [ ] **Step 7: Run GREEN, the full current suite, and help command**

```shell
python -m unittest \
  tests.http_server_test.HttpApplicationTest \
  tests.http_server_test.StreamableHttpProtocolTest \
  -v
python -m unittest discover --buffer -s tests -p "*_test.py"
python -m pip install -e .
analytics-mcp-http --help
nox -s lint
```

Expected: all commands exit 0 and no test resolves live Google credentials.

- [ ] **Step 8: Commit**

```shell
git add analytics_mcp/http_server.py tests/http_server_test.py pyproject.toml
git commit -m "feat(http): add stateless Streamable HTTP transport"
```

---

### Task 3: Cover real Google Analytics tool discovery and stdio compatibility

**Files:**
- Modify: `tests/http_server_test.py`

**Interfaces:**
- Consumes: `create_http_app()` from Task 2.
- Verifies the production `coordinator.app` tool registry without executing Google API calls.
- Verifies all three console-script mappings.

- [ ] **Step 1: Add production tool-discovery regression coverage**

Add:

```python
class GoogleAnalyticsToolDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_lists_existing_google_analytics_tools(self):
        app = http_server.create_http_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=True,
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream, write_stream
                    ) as session:
                        await session.initialize()
                        tools = await session.list_tools()

        names = {tool.name for tool in tools.tools}
        self.assertIn("get_account_summaries", names)
        self.assertIn("run_report", names)
        self.assertIn("run_realtime_report", names)
        self.assertIn("run_funnel_report", names)
        self.assertIn("run_conversions_report", names)
```

This must list tools only; do not call a production Analytics tool.

- [ ] **Step 2: Add console-script compatibility coverage**

Add:

```python
from importlib import metadata


class ConsoleScriptCompatibilityTest(unittest.TestCase):
    def test_stdio_and_http_scripts_are_registered(self):
        expected = {
            "analytics-mcp": "analytics_mcp.server:run_server",
            "google-analytics-mcp": "analytics_mcp.server:run_server",
            "analytics-mcp-http": (
                "analytics_mcp.http_server:run_http_server"
            ),
        }
        scripts = {
            entry.name: entry.value
            for entry in metadata.entry_points(group="console_scripts")
            if entry.name in expected
        }
        self.assertEqual(scripts, expected)
```

- [ ] **Step 3: Install editable package and run the regression tests**

```shell
python -m pip install -e .
python -m unittest \
  tests.http_server_test.GoogleAnalyticsToolDiscoveryTest \
  tests.http_server_test.ConsoleScriptCompatibilityTest \
  -v
python -m unittest discover --buffer -s tests -p "*_test.py"
nox -s lint
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit**

```shell
git add tests/http_server_test.py
git commit -m "test(http): cover remote tool discovery and compatibility"
```

---

### Task 4: Add a portable non-root container

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `analytics-mcp-http`.
- Produces: OCI image listening on `0.0.0.0:$PORT`, default `PORT=8080`.

- [ ] **Step 1: Add a defensive Docker build context**

Create `.dockerignore`:

```text
.git
.github
.venv
venv
__pycache__
*.pyc
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

The JSON exclusion prevents common local credential files from entering the build context; `skills-lock.json` is restored because it is repository content.

- [ ] **Step 2: Add `Dockerfile`**

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

Do not set `GOOGLE_APPLICATION_CREDENTIALS` or copy a service-account key.

- [ ] **Step 3: Build and smoke-test when Docker is available**

```shell
docker build -t analytics-mcp-http:test .
docker run --rm -d \
  --name analytics-mcp-http-test \
  -p 18080:8080 \
  analytics-mcp-http:test
curl --fail http://127.0.0.1:18080/healthz
docker stop analytics-mcp-http-test
```

Expected: image builds and `curl` returns `ok`. If the execution environment has no Docker daemon, record that limitation and do not claim the image was runtime-verified.

- [ ] **Step 4: Commit**

```shell
git add Dockerfile .dockerignore
git commit -m "build(container): add remote MCP runtime image"
```

---

### Task 5: Document remote operation and secure deployment

**Files:**
- Create: `docs/remote-server.md`
- Modify: `README.md`

**Interfaces:**
- Documents `analytics-mcp-http`, `/mcp`, `/healthz`, Docker, Cloud Run, ADC, and deployment-layer authentication.

- [ ] **Step 1: Add a concise README remote-server section**

Add after the current local client setup, leaving the local instructions first:

```markdown
### Run as a remote MCP server

The same tools can also be exposed over stateless MCP Streamable HTTP:

```shell
analytics-mcp-http
```

The default endpoint is `http://127.0.0.1:8000/mcp`. Remote deployments
continue to use Application Default Credentials and must be protected by the
deployment environment. See [Remote server deployment](docs/remote-server.md)
for Docker, Cloud Run, and security guidance.
```

- [ ] **Step 2: Document local HTTP and Docker**

In `docs/remote-server.md`, explain the single-identity ADC model and include:

```shell
pipx install analytics-mcp
analytics-mcp-http
analytics-mcp-http --host 0.0.0.0 --port 8080 --path /mcp
```

Document precedence: `--port` overrides `PORT`; otherwise `PORT` overrides `8000`.

Add Docker:

```shell
docker build -t analytics-mcp .
docker run --rm -p 8080:8080 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/credentials/adc.json \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/credentials/adc.json:ro" \
  analytics-mcp
```

Label the file mount as local development only and recommend workload/service identity on managed platforms.

- [ ] **Step 3: Document Cloud Run with service identity**

Include:

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

Then instruct operators to add `${SERVICE_ACCOUNT}` as a Viewer to the intended Google Analytics account/property.

State explicitly that IAM-protected Cloud Run requires a client capable of presenting the required Google identity token. A client without that capability needs an authentication layer it supports; do not recommend `--allow-unauthenticated` merely to make a client connect.

- [ ] **Step 4: Document security and troubleshooting**

State all of the following explicitly:

- The HTTP server is client-neutral and single-identity.
- End-user OAuth is not implemented in this change.
- The deployment controls who may reach `/mcp`.
- Never expose Analytics read access unauthenticated to the public Internet.
- `/healthz` proves process liveness only and does not validate ADC.
- `DefaultCredentialsError` means ADC/service identity is missing.
- HTTP 401/403 before MCP initialization means deployment auth denied the client.
- HTTP 404 means the configured MCP path is wrong; default is `/mcp`.
- Analytics permission errors require checking API enablement and Viewer access for the runtime identity.
- If `/healthz` succeeds while tools fail, transport is alive and the next investigation is ADC/API authorization.

- [ ] **Step 5: Verify docs against the executable**

```shell
analytics-mcp-http --help
python -m unittest discover --buffer -s tests -p "*_test.py"
nox -s lint
```

Manually confirm documented defaults match code: `127.0.0.1`, `8000` or `PORT`, `/mcp`.

- [ ] **Step 6: Commit**

```shell
git add README.md docs/remote-server.md
git commit -m "docs(remote): add self-hosting and Cloud Run guide"
```

---

### Task 6: Run full verification and prepare the upstream diff

**Files:**
- Modify only if verification exposes a defect.

**Interfaces:**
- Validates all acceptance criteria and keeps the upstream PR focused.

- [ ] **Step 1: Run formatting and every supported Python environment available locally**

```shell
nox -s lint
nox -s tests-3.10 tests-3.11 tests-3.12 tests-3.13
```

If a Python interpreter is unavailable, run every installed version and report exactly which matrix entries remain for GitHub Actions; do not claim full local matrix coverage.

- [ ] **Step 2: Re-run the HTTP protocol tests**

```shell
python -m unittest \
  tests.http_server_test.StreamableHttpProtocolTest \
  tests.http_server_test.GoogleAnalyticsToolDiscoveryTest \
  tests.http_server_test.ConsoleScriptCompatibilityTest \
  -v
```

Expected: initialization, isolated tool call, production tool discovery, statelessness, and script compatibility all pass without live credentials.

- [ ] **Step 3: Verify a built wheel**

```shell
python -m pip install build
python -m build
python -m venv /tmp/analytics-mcp-wheel-test
/tmp/analytics-mcp-wheel-test/bin/pip install dist/analytics_mcp-*.whl
/tmp/analytics-mcp-wheel-test/bin/analytics-mcp-http --help
```

Expected: build succeeds and the HTTP console script resolves from the wheel. `build` is verification tooling only and must not be added to runtime dependencies.

- [ ] **Step 4: Rebuild and smoke-test the container if Docker is available**

```shell
docker build -t analytics-mcp-http:test .
docker run --rm -d \
  --name analytics-mcp-http-test \
  -p 18080:8080 \
  analytics-mcp-http:test
curl --fail http://127.0.0.1:18080/healthz
docker stop analytics-mcp-http-test
```

Expected: `/healthz` returns `ok`.

- [ ] **Step 5: Inspect branch scope against `main`**

```shell
git diff --stat main...HEAD
git diff main...HEAD -- \
  analytics_mcp pyproject.toml tests Dockerfile .dockerignore README.md docs
```

Verify line-by-line that no Analytics tool logic, credential cache, stdio behavior, MCP major version, OAuth, token storage, or unrelated refactor entered the change.

- [ ] **Step 6: Remove development-only planning artifacts from the upstream diff**

Keep these in the fork while implementing:

```text
docs/superpowers/specs/2026-08-27-streamable-http-transport-design.md
docs/superpowers/plans/2026-08-27-streamable-http-transport.md
```

Before opening the upstream PR, remove them from the PR branch unless the maintainers explicitly request internal design/process documents. Keep `docs/remote-server.md` in the PR.

- [ ] **Step 7: Prepare PR metadata but do not submit before review**

Use title:

```text
feat: add Streamable HTTP transport
```

Use summary:

```markdown
- add an optional stateless Streamable HTTP transport while preserving stdio
- expose the existing Google Analytics tools through a self-hostable ASGI server
- add a portable container and Cloud Run deployment guidance using ADC
- cover initialization, tool discovery, tool calls, statelessness, and stdio compatibility

Related discussions: #17, #47, #106, #113
```

Do not claim Google hosts the server or that unauthenticated public deployment is safe.

- [ ] **Step 8: Commit verification-driven fixes only when files actually changed**

```shell
git add <the-files-that-verification-changed>
git commit -m "fix(http): address remote transport verification"
```

Do not create an empty cleanup commit.
