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
from mcp.server.lowlevel import Server
from starlette.testclient import TestClient

from analytics_mcp import http_server


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
