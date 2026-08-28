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

import contextlib
import unittest
from importlib import metadata
from typing import Any
from unittest import mock

import httpx
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import Server
from starlette.testclient import TestClient

from analytics_mcp import auth, http_server


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


def create_test_mcp_server() -> Server:
    """Creates an isolated server for transport-level tests."""
    server = Server("test-analytics-mcp")

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
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
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[mcp_types.ContentBlock]:
        if name != "echo_property":
            raise ValueError(f"Unknown test tool: {name}")
        return [
            mcp_types.TextContent(
                type="text",
                text=arguments["property_id"],
            )
        ]

    return server


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
        config = http_server.parse_http_config(["--path", "/analytics/"], {})
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


class HttpTransportSecurityTest(unittest.TestCase):
    def test_loopback_bind_enables_dns_rebinding_protection(self):
        settings = http_server._transport_security("127.0.0.1")
        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertIn("127.0.0.1:*", settings.allowed_hosts)
        self.assertIn("localhost:*", settings.allowed_hosts)

    def test_remote_bind_defers_host_validation_to_deployment(self):
        settings = http_server._transport_security("0.0.0.0")
        self.assertFalse(settings.enable_dns_rebinding_protection)


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

    def test_default_mcp_path_does_not_redirect(self):
        async def fake_handle_request(_manager, scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        with mock.patch.object(
            http_server.StreamableHTTPSessionManager,
            "handle_request",
            new=fake_handle_request,
        ):
            app = http_server.create_http_app()
            with TestClient(app, follow_redirects=False) as client:
                response = client.post("/mcp")

        self.assertEqual(response.status_code, 204)

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
        self.assertEqual(
            body["resource"],
            "https://analytics.example.com/mcp",
        )
        self.assertEqual(
            body["authorization_servers"],
            ["https://example.us.auth0.com/"],
        )
        self.assertEqual(body["scopes_supported"], ["analytics:read"])

    def test_mcp_without_token_returns_401(self):
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(None),
        )
        with TestClient(app, follow_redirects=False) as client:
            response = client.post("/mcp")
        self.assertEqual(response.status_code, 401)

    def test_401_challenge_includes_resource_metadata(self):
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(None),
        )
        with TestClient(app, follow_redirects=False) as client:
            response = client.post("/mcp")
        challenge = response.headers["www-authenticate"]
        self.assertIn("Bearer", challenge)
        self.assertIn(
            "resource_metadata=",
            challenge,
        )
        self.assertIn(
            "/.well-known/oauth-protected-resource/mcp",
            challenge,
        )

    def test_invalid_token_returns_401(self):
        verifier = _StaticTokenVerifier(None)
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=verifier,
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                headers={"Authorization": "Bearer invalid-token"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(verifier.calls, ["invalid-token"])

    def test_valid_token_without_required_scope_returns_403(self):
        access_token = AccessToken(
            token="test-token",
            client_id="auth0|test-user",
            scopes=["openid"],
            expires_at=None,
            resource="https://analytics.example.com/mcp",
        )
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(access_token),
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "insufficient_scope")

    def test_auth_disabled_does_not_publish_metadata(self):
        app = http_server.create_http_app()
        with TestClient(app) as client:
            response = client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
        self.assertEqual(response.status_code, 404)

    def test_oauth_mcp_path_does_not_redirect(self):
        app = http_server.create_http_app(
            auth_config=AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(None),
        )
        with TestClient(app, follow_redirects=False) as client:
            response = client.post("/mcp")
        self.assertEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 307)

    def test_resource_path_must_match_mcp_path(self):
        with self.assertRaisesRegex(ValueError, "resource path"):
            http_server.create_http_app(
                path="/analytics",
                auth_config=AUTH_CONFIG,
                token_verifier=_StaticTokenVerifier(None),
            )


class StreamableHttpProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_lists_and_calls_tool(self):
        app = http_server.create_http_app(mcp_server=create_test_mcp_server())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost:8000",
            ) as http_client:
                async with streamable_http_client(
                    "http://localhost:8000/mcp",
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

        self.assertEqual(
            initialized.serverInfo.name,
            "test-analytics-mcp",
        )
        self.assertEqual(
            [tool.name for tool in tools.tools],
            ["echo_property"],
        )
        self.assertFalse(result.isError)
        self.assertEqual(result.content[0].text, "123456")
        self.assertIsNone(get_session_id())


class GoogleAnalyticsToolDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_lists_existing_google_analytics_tools(self):
        app = http_server.create_http_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost:8000",
            ) as http_client:
                async with streamable_http_client(
                    "http://localhost:8000/mcp",
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


class ConsoleScriptCompatibilityTest(unittest.TestCase):
    def test_stdio_and_http_scripts_are_registered(self):
        expected = {
            "analytics-mcp": "analytics_mcp.server:run_server",
            "google-analytics-mcp": "analytics_mcp.server:run_server",
            "analytics-mcp-http": ("analytics_mcp.http_server:run_http_server"),
        }
        scripts = {
            entry.name: entry.value
            for entry in metadata.entry_points(group="console_scripts")
            if entry.name in expected
        }
        self.assertEqual(scripts, expected)
