"""An MCP server for Grocy — read and manage a household pantry from an AI agent.

Typical use is the command line (`grocy-mcp`), but the pieces are importable:

    from grocy_mcp import api, build_server
    api.configure("https://grocy.example.com/api", key)
    server = build_server()

Two properties this package keeps deliberately:

- Importing it never requires a live Grocy. `api` and `config` read the
  environment on first use, not at import.
- Importing the data layer never requires the MCP SDK. `grocy_mcp.api`,
  `grocy_mcp.stock` and `grocy_mcp.lookup` are useful on their own — for a
  report, a static page, a script — and pulling in the server stack to read a
  stock list would make them useless in any environment without it. So
  `build_server` and `register_all` are resolved lazily, below.
"""

from .api import GrocyClient, GrocyError
from .api import configure as configure_api
from .config import PantryConfig
from .config import configure as configure_pantry

__all__ = [
    "GrocyClient",
    "GrocyError",
    "PantryConfig",
    "build_server",
    "configure_api",
    "configure_pantry",
    "register_all",
]

__version__ = "0.1.1"

_LAZY = {"build_server", "register_all"}


def __getattr__(name):
    """Resolve the server entry points on first access (PEP 562)."""
    if name in _LAZY:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
