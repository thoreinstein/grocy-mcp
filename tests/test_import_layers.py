"""The data layer must be usable without the MCP SDK.

Regression test for a real breakage: a static page generator importing
`grocy_mcp.stock` died with ModuleNotFoundError: No module named 'mcp', because
the package __init__ eagerly imported the server. Anything wanting just the API
client or stock shaping should not need the server stack.
"""

import subprocess
import sys
import textwrap

BLOCK_MCP = """
import sys

class Blocked:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)
    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("mcp is not installed (blocked by the test)")
        return None

sys.meta_path.insert(0, Blocked())
"""


def run_without_mcp(body):
    # Dedent the body before joining: BLOCK_MCP is already flush left, so
    # dedenting the concatenation would find no common prefix to strip.
    script = BLOCK_MCP + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )


def test_data_layer_imports_without_the_mcp_sdk():
    result = run_without_mcp("""
        import grocy_mcp
        from grocy_mcp.stock import fmt_amount, load_items
        from grocy_mcp.api import GrocyError
        from grocy_mcp.lookup import resolve
        print(fmt_amount(2, "Pack"))
    """)
    assert result.returncode == 0, result.stderr
    assert "2 Packs" in result.stdout


def test_the_blocker_actually_blocks():
    """Otherwise the test above would pass for the wrong reason."""
    result = run_without_mcp("import mcp")
    assert result.returncode != 0
    assert "blocked by the test" in result.stderr


def test_build_server_is_still_reachable_from_the_package_root():
    from grocy_mcp import build_server

    assert callable(build_server)


def test_unknown_attribute_still_raises():
    import grocy_mcp
    import pytest

    with pytest.raises(AttributeError):
        grocy_mcp.no_such_thing
