"""Server construction and the tool surface it exposes."""

import pytest

from grocy_mcp.__main__ import parse_args
from grocy_mcp.server import build_server

EXPECTED_TOOLS = {
    # stock
    "list_stock", "expiring_soon", "out_of_stock", "below_min_stock",
    "list_stock_entries", "product_details", "stock_history", "add_stock",
    "consume_product", "open_product", "correct_stock", "transfer_stock",
    "edit_stock_entry", "undo_transaction",
    # catalog
    "search_products", "get_conventions", "create_product", "update_product",
    "add_barcode", "remove_barcode", "delete_product",
    # shopping list
    "list_shopping_list", "add_to_shopping_list", "remove_from_shopping_list",
    "check_off_shopping_item", "add_missing_to_shopping_list",
    "clear_shopping_list",
    # organizers
    "create_location", "create_quantity_unit", "create_category",
    # sync ledger
    "sync_log_check", "sync_log_add",
}


@pytest.mark.anyio
async def test_every_tool_is_registered():
    server = build_server()
    names = {t.name for t in await server.list_tools()}
    assert names == EXPECTED_TOOLS


@pytest.mark.anyio
async def test_every_tool_has_a_description():
    """The docstring is the whole interface an agent sees."""
    for tool in await build_server().list_tools():
        assert tool.description and len(tool.description) > 40, tool.name


@pytest.mark.anyio
async def test_tools_are_callable_through_the_server(fake_grocy):
    server = build_server()
    result = await server.call_tool("list_stock", {})
    assert result is not None


def test_building_without_auth_configures_no_oauth():
    server = build_server()
    assert server.settings.auth is None


def test_stdio_is_the_default_transport():
    assert parse_args([]).transport == "stdio"


def test_auth_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse_args(["--auth", "--no-auth"])


def test_no_auth_parses_to_false():
    assert parse_args(["--transport", "http", "--no-auth"]).auth is False
    assert parse_args(["--transport", "http"]).auth is None


@pytest.fixture
def anyio_backend():
    return "asyncio"
