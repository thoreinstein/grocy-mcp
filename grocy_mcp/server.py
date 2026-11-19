"""Building the MCP server.

The tool modules expose `register(mcp)` rather than decorating a module-level
instance, so the server can be constructed with whatever transport and auth
the caller wants instead of whatever the environment happened to say at import
time. That also makes the tools importable in tests without a server at all.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .tools import organizers, products, shopping, stock, sync_log

INSTRUCTIONS = """\
Live kitchen inventory backed by Grocy.

Read tools return current stock — every call queries Grocy, so there is no
cache to bust and no freshness check to do. Write tools record what was used,
bought, opened, moved or miscounted, and the shopping list tools cover the trip
in between.

Call get_conventions first. It reports this instance's locations (with ids),
categories, units, and whatever house rules are configured — how long things
keep when the package date is missing, how products are named. Guessing an id
or a category name instead is the main way these tools go wrong.

Amounts are always in each product's own stock unit, which differs per product:
spices are often tracked in grams, packaged goods per pack. Read the qty string
before consuming rather than assuming.

Every stock write returns a transaction_id. To fix a mistake, pass it to
undo_transaction — booking an opposite consume or purchase instead leaves both
rows in the journal and invents a best-before date.
"""


def register_all(mcp: FastMCP) -> FastMCP:
    """Attach every tool to a server instance."""
    stock.register(mcp)
    products.register(mcp)
    shopping.register(mcp)
    organizers.register(mcp)
    sync_log.register(mcp)
    return mcp


def build_server(
    *,
    name: str = "grocy-pantry",
    stateless: bool = True,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    public_host: str | None = None,
    public_url: str | None = None,
    auth: bool = False,
    required_scopes: list[str] | None = None,
) -> FastMCP:
    """Build a server.

    With `auth=False` (the default, and the only sensible setting for stdio)
    nothing about OAuth is configured. With `auth=True` the OIDC settings are
    read from GROCY_MCP_OIDC_* and the server advertises its protected-resource
    metadata so a client knows where to send the user to log in.
    """
    kwargs: dict = {"instructions": INSTRUCTIONS}

    if auth or public_host:
        from mcp.server.transport_security import TransportSecuritySettings

        public_host = public_host or f"{host}:{port}"
        # DNS-rebinding protection. The SDK leaves this off when
        # transport_security is unset, and — the trap — rejects *everything*
        # with 421 if it is switched on with an empty allow-list. So name the
        # hosts explicitly: the public name a proxy serves under, plus loopback
        # for debugging with curl on the box itself.
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, f"{host}:{port}", f"localhost:{port}"],
        )

    if auth:
        from mcp.server.auth.settings import AuthSettings

        from .auth import verifier_from_env

        issuer = os.environ["GROCY_MCP_OIDC_ISSUER"].rstrip("/")
        scopes = required_scopes or os.environ.get("GROCY_MCP_OIDC_SCOPES", "").split()
        kwargs["token_verifier"] = verifier_from_env()
        # Makes the SDK serve /.well-known/oauth-protected-resource and answer
        # an unauthenticated call with 401 + WWW-Authenticate pointing at it.
        # That handshake is what tells a client where to log in; without it the
        # connector has no way to discover the authorization server.
        kwargs["auth"] = AuthSettings(
            issuer_url=issuer,
            # Must equal the URL as the client has it configured, path
            # included, or the metadata is rejected as not describing this
            # resource.
            resource_server_url=public_url or f"https://{public_host}{path}",
            required_scopes=scopes or None,
        )

    mcp = FastMCP(
        name,
        host=host,
        port=port,
        streamable_http_path=path,
        # Stateless: each HTTP request is self-contained, so a restart or a
        # proxy hiccup never strands a client holding a dead session id.
        stateless_http=stateless,
        **kwargs,
    )
    return register_all(mcp)
