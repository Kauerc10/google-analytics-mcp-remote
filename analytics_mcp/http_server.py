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

"""HTTP server configuration for Google Analytics MCP."""

import argparse
import contextlib
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import TokenVerifier
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

import analytics_mcp.auth as auth
import analytics_mcp.coordinator as coordinator

_LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class HttpServerConfig:
    """Configuration for the Streamable HTTP server."""

    host: str
    port: int
    path: str
    auth: auth.AuthConfig


class _McpEndpoint:
    """ASGI endpoint that forwards requests to the MCP transport."""

    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self._session_manager = session_manager

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def _port(value: str) -> int:
    """Parse and validate a TCP port."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _path(value: str) -> str:
    """Normalize and validate the MCP endpoint path."""
    if not value.startswith("/"):
        raise argparse.ArgumentTypeError("path must start with '/'")
    normalized = value.rstrip("/")
    return normalized or "/"


def _transport_security(host: str) -> TransportSecuritySettings:
    """Return transport security settings for the selected bind host."""
    if host in _LOCAL_BIND_HOSTS:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1",
                "127.0.0.1:*",
                "localhost",
                "localhost:*",
                "[::1]",
                "[::1]:*",
            ],
            allowed_origins=[
                "http://127.0.0.1",
                "http://127.0.0.1:*",
                "http://localhost",
                "http://localhost:*",
                "http://[::1]",
                "http://[::1]:*",
            ],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def parse_http_config(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> HttpServerConfig:
    """Parse HTTP server configuration from arguments and environment."""
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
    return HttpServerConfig(
        args.host,
        args.port,
        args.path,
        auth.parse_auth_config(env),
    )


def create_http_app(
    mcp_server: Server = coordinator.app,
    path: str = "/mcp",
    host: str = "127.0.0.1",
    auth_config: auth.AuthConfig | None = None,
    token_verifier: TokenVerifier | None = None,
) -> Starlette:
    """Create the ASGI application for Streamable HTTP."""
    normalized_path = _path(path)
    auth_config = auth_config or auth.AuthConfig(auth.AuthMode.NONE)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=True,
        stateless=True,
        security_settings=_transport_security(host),
    )

    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    mcp_endpoint = _McpEndpoint(session_manager)
    routes = [Route("/healthz", healthz, methods=["GET"])]

    if auth_config.enabled:
        resource = auth_config.resource
        required_scope = auth_config.required_scope
        if resource is None or required_scope is None:
            raise ValueError("OAuth configuration is incomplete")
        resource_path = urlparse(resource).path.rstrip("/") or "/"
        if resource_path != normalized_path:
            raise ValueError("OAuth resource path must match MCP path")

        verifier = token_verifier or auth.Auth0TokenVerifier(auth_config)
        protected_endpoint = RequireAuthMiddleware(
            mcp_endpoint,
            required_scopes=[required_scope],
            resource_metadata_url=auth.resource_metadata_url(auth_config),
        )
        protected_endpoint = AuthContextMiddleware(protected_endpoint)
        mcp_endpoint = AuthenticationMiddleware(
            protected_endpoint,
            backend=BearerAuthBackend(verifier),
        )
        routes.extend(auth.protected_resource_routes(auth_config))

    routes.append(
        Route(
            normalized_path,
            endpoint=mcp_endpoint,
            methods=["GET", "POST", "DELETE"],
        )
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def run_http_server(argv: Sequence[str] | None = None) -> None:
    """Run the Streamable HTTP server with Uvicorn."""
    config = parse_http_config(argv)
    app = create_http_app(
        path=config.path,
        host=config.host,
        auth_config=config.auth,
    )

    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)
