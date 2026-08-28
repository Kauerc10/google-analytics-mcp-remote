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


class HttpAuthConfigIntegrationTest(unittest.TestCase):
    def test_http_config_includes_disabled_auth_by_default(self):
        from analytics_mcp import http_server

        config = http_server.parse_http_config([], {})
        self.assertEqual(config.auth.mode, auth.AuthMode.NONE)

    def test_http_config_preserves_auth0_configuration(self):
        from analytics_mcp import http_server

        config = http_server.parse_http_config(
            [],
            {
                "MCP_AUTH_MODE": "auth0",
                "MCP_AUTH_ISSUER": "https://example.us.auth0.com/",
                "MCP_AUTH_RESOURCE": "https://analytics.example.com/mcp",
                "MCP_AUTH_REQUIRED_SCOPE": "analytics:read",
            },
        )
        self.assertEqual(config.auth.mode, auth.AuthMode.AUTH0)
        self.assertEqual(config.auth.required_scope, "analytics:read")
