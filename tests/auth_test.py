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

import time
import unittest
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken

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


class _SigningKey:
    def __init__(self, key):
        self.key = key


class _StaticJwksClient:
    def __init__(self, key):
        self.key = key
        self.calls = []

    def get_signing_key_from_jwt(self, token):
        self.calls.append(token)
        return _SigningKey(self.key)


class Auth0TokenVerifierTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.config = auth.AuthConfig(
            mode=auth.AuthMode.AUTH0,
            issuer="https://example.us.auth0.com/",
            resource="https://analytics.example.com/mcp",
            required_scope="analytics:read",
        )
        self.jwks_client = _StaticJwksClient(self.public_key)

    def _sign_token(self, private_key=None, algorithm="RS256", **overrides):
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
        signing_key = private_key or self.private_key
        return jwt.encode(
            claims,
            signing_key,
            algorithm=algorithm,
            headers={"kid": "test-key"},
        )

    def _verifier(self):
        return auth.Auth0TokenVerifier(
            self.config,
            jwks_client=self.jwks_client,
        )

    async def test_accepts_valid_rs256_token(self):
        token = self._sign_token()
        result = await self._verifier().verify_token(token)
        self.assertIsInstance(result, AccessToken)
        self.assertEqual(result.client_id, "auth0|test-user")
        self.assertEqual(result.scopes, ["analytics:read"])
        self.assertEqual(result.resource, "https://analytics.example.com/mcp")

    async def test_rejects_forged_signature(self):
        forged_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        token = self._sign_token(private_key=forged_key)
        result = await self._verifier().verify_token(token)
        self.assertIsNone(result)

    async def test_rejects_expired_token(self):
        token = self._sign_token(exp=int(time.time()) - 1)
        result = await self._verifier().verify_token(token)
        self.assertIsNone(result)

    async def test_rejects_wrong_issuer(self):
        token = self._sign_token(iss="https://other.auth0.com/")
        result = await self._verifier().verify_token(token)
        self.assertIsNone(result)

    async def test_rejects_wrong_audience(self):
        token = self._sign_token(aud="https://other.example.com/mcp")
        result = await self._verifier().verify_token(token)
        self.assertIsNone(result)

    async def test_rejects_unsupported_algorithm(self):
        token = self._sign_token(
            private_key="test-secret",
            algorithm="HS256",
        )
        result = await self._verifier().verify_token(token)
        self.assertIsNone(result)

    async def test_extracts_space_delimited_scopes(self):
        token = self._sign_token(
            scope="openid offline_access analytics:read"
        )
        result = await self._verifier().verify_token(token)
        self.assertEqual(
            result.scopes,
            ["openid", "offline_access", "analytics:read"],
        )

    async def test_rejects_token_without_subject(self):
        token = self._sign_token(sub="")
        result = await self._verifier().verify_token(token)
        self.assertIsNone(result)

    async def test_does_not_log_raw_token(self):
        token = self._sign_token(aud="https://wrong.example.com/mcp")
        with mock.patch.object(auth._LOGGER, "debug") as debug_log:
            result = await self._verifier().verify_token(token)
        self.assertIsNone(result)
        for call in debug_log.call_args_list:
            self.assertNotIn(token, str(call))

    def test_uses_configured_auth0_jwks_endpoint(self):
        with mock.patch("analytics_mcp.auth.jwt.PyJWKClient") as client:
            auth.Auth0TokenVerifier(self.config)
        client.assert_called_once_with(
            "https://example.us.auth0.com/.well-known/jwks.json"
        )
