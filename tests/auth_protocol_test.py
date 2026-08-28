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

import unittest
from typing import Any

import httpx
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import Server

from analytics_mcp import auth, http_server

_AUTH_CONFIG = auth.AuthConfig(
    mode=auth.AuthMode.AUTH0,
    issuer="https://example.us.auth0.com/",
    resource="https://analytics.example.com/mcp",
    required_scope="analytics:read",
)


class _StaticTokenVerifier:
    def __init__(self, access_token: AccessToken):
        self.access_token = access_token

    async def verify_token(self, _token: str) -> AccessToken:
        return self.access_token


def _access_token() -> AccessToken:
    return AccessToken(
        token="test-token",
        client_id="auth0|test-user",
        scopes=["analytics:read"],
        expires_at=None,
        resource="https://analytics.example.com/mcp",
    )


def _create_test_server() -> Server:
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
        name: str,
        arguments: dict[str, Any],
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


class AuthenticatedStreamableHttpProtocolTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_initialize_list_and_call_stay_stateless(self):
        app = http_server.create_http_app(
            mcp_server=_create_test_server(),
            auth_config=_AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(_access_token()),
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
                    async with ClientSession(
                        read_stream,
                        write_stream,
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

    async def test_production_tool_registry_is_preserved(self):
        app = http_server.create_http_app(
            auth_config=_AUTH_CONFIG,
            token_verifier=_StaticTokenVerifier(_access_token()),
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
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                    ) as session:
                        await session.initialize()
                        tools = await session.list_tools()

        names = {tool.name for tool in tools.tools}
        self.assertIn("get_account_summaries", names)
        self.assertIn("run_report", names)
        self.assertIn("run_realtime_report", names)
        self.assertIn("run_funnel_report", names)
        self.assertIn("run_conversions_report", names)
