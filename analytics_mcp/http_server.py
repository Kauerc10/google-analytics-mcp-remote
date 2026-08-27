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

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

import analytics_mcp.coordinator as coordinator


@dataclass(frozen=True)
class HttpServerConfig:
    """Configuration for the Streamable HTTP server."""

    host: str
    port: int
    path: str


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
    return HttpServerConfig(args.host, args.port, args.path)


def create_http_app(
    mcp_server: Server = coordinator.app,
    path: str = "/mcp",
) -> Starlette:
    """Create the ASGI application for Streamable HTTP."""
    normalized_path = _path(path)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=True,
        stateless=True,
    )

    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route(
                normalized_path,
                endpoint=_McpEndpoint(session_manager),
                methods=["GET", "POST", "DELETE"],
            ),
        ],
        lifespan=lifespan,
    )


def run_http_server(argv: Sequence[str] | None = None) -> None:
    """Run the Streamable HTTP server with Uvicorn."""
    config = parse_http_config(argv)
    app = create_http_app(path=config.path)

    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port)
