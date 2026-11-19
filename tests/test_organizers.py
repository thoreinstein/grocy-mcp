"""Organizer tools: what they send, and what they refuse."""

import pytest

from grocy_mcp.tools import organizers, products


def test_create_location_sends_name_and_freezer_flag(fake_grocy):
    out = organizers.create_location("Garage Shelf", "Bulk paper goods")
    assert fake_grocy.body_of("POST", "/objects/locations") == {
        "name": "Garage Shelf",
        "is_freezer": 0,
        "description": "Bulk paper goods",
    }
    assert out["location"] == "Garage Shelf" and out["id"] == 5


def test_create_location_marks_a_freezer(fake_grocy):
    organizers.create_location("Chest Freezer", is_freezer=True)
    assert fake_grocy.body_of("POST", "/objects/locations")["is_freezer"] == 1


def test_create_location_refuses_an_existing_name_ignoring_case(fake_grocy):
    with pytest.raises(ValueError, match="already a location"):
        organizers.create_location("pantry")
    assert not [w for w in fake_grocy.writes if w[0] == "POST"]


def test_create_quantity_unit_defaults_plural_to_singular(fake_grocy):
    organizers.create_quantity_unit("Can")
    assert fake_grocy.body_of("POST", "/objects/quantity_units") == {
        "name": "Can",
        "name_plural": "Can",
    }


def test_create_quantity_unit_refuses_an_existing_unit(fake_grocy):
    with pytest.raises(ValueError, match="already a unit"):
        organizers.create_quantity_unit("GRAM")


def test_create_category_then_create_product_uses_it(fake_grocy):
    organizers.create_category("Canned Goods")
    out = products.create_product("Black Beans", "Canned Goods", "Pantry")
    assert out["category"] == "Canned Goods"


def test_blank_names_are_refused(fake_grocy):
    with pytest.raises(ValueError, match="No category name"):
        organizers.create_category("  ")
