"""Command line entry point.

    grocy-mcp                       # stdio, for a local MCP client
    grocy-mcp --transport http      # HTTP, behind a reverse proxy
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import PantryConfig, configure
from .server import build_server


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="grocy-mcp",
        description="MCP server exposing a Grocy instance to AI agents.",
    )
    p.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("GROCY_MCP_TRANSPORT", "stdio"),
        help="stdio (default) for a local client; http to serve over the "
             "network behind a reverse proxy.",
    )
    p.add_argument("--host", default=os.environ.get("GROCY_MCP_HOST", "127.0.0.1"),
                   help="http only. Default 127.0.0.1; a proxy should terminate TLS.")
    p.add_argument("--port", type=int, default=int(os.environ.get("GROCY_MCP_PORT", "8765")),
                   help="http only. Default 8765.")
    p.add_argument("--path", default=os.environ.get("GROCY_MCP_PATH", "/mcp"),
                   help="http only. URL path to serve on. Default /mcp.")
    p.add_argument("--public-host", default=os.environ.get("GROCY_MCP_PUBLIC_HOST"),
                   help="http only. The hostname clients reach this server by, "
                        "i.e. the proxy's name. Required for DNS-rebinding "
                        "protection to accept proxied requests.")
    p.add_argument("--public-url", default=os.environ.get("GROCY_MCP_PUBLIC_URL"),
                   help="http only. The full URL as configured in the client, "
                        "path included. Defaults to https://<public-host><path>.")
    p.add_argument("--config", default=os.environ.get("GROCY_MCP_CONFIG"),
                   help="Path to a pantry config file (TOML). Optional.")
    p.add_argument("--name", default=os.environ.get("GROCY_MCP_NAME", "grocy-pantry"),
                   help="Server name shown to clients. Default grocy-pantry.")
    auth = p.add_mutually_exclusive_group()
    auth.add_argument("--auth", action="store_true", default=None,
                      help="http only. Require an OAuth 2.1 bearer token, "
                           "validated against GROCY_MCP_OIDC_ISSUER.")
    auth.add_argument("--no-auth", dest="auth", action="store_false",
                      help="http only. Serve without authentication. Only safe "
                           "on an interface nothing untrusted can reach.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Log at DEBUG.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    # stdio speaks JSON-RPC on stdout, so logs must go to stderr or they
    # corrupt the protocol stream.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
    )

    configure(PantryConfig.load(args.config))

    if args.transport == "stdio":
        if args.auth:
            raise SystemExit(
                "--auth applies to the http transport only; a stdio server is "
                "reached by the local process that launched it."
            )
        build_server(name=args.name).run(transport="stdio")
        return 0

    # Default to requiring auth over HTTP unless told otherwise: an
    # unauthenticated write-capable server is the kind of default nobody
    # notices until it is reachable from somewhere it shouldn't be.
    use_auth = True if args.auth is None else args.auth
    if use_auth and not os.environ.get("GROCY_MCP_OIDC_ISSUER"):
        raise SystemExit(
            "Serving over http requires either GROCY_MCP_OIDC_ISSUER (to "
            "validate bearer tokens) or an explicit --no-auth."
        )

    server = build_server(
        name=args.name,
        host=args.host,
        port=args.port,
        path=args.path,
        public_host=args.public_host,
        public_url=args.public_url,
        auth=use_auth,
    )
    server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
