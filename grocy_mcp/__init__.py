"""An MCP server for Grocy — read and manage a household pantry from an AI agent.

Typical use is the command line (`grocy-mcp`), but the pieces are importable:

    from grocy_mcp import api, build_server
    api.configure("https://grocy.example.com/api", key)
    server = build_server()

`api` and `config` read the environment lazily, so importing this package never
requires a live Grocy instance.
"""

from .api import GrocyClient, GrocyError, configure as configure_api
from .config import PantryConfig, configure as configure_pantry
from .server import build_server, register_all

__all__ = [
    "GrocyClient",
    "GrocyError",
    "PantryConfig",
    "build_server",
    "configure_api",
    "configure_pantry",
    "register_all",
]

__version__ = "0.1.0"
