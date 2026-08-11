"""Tool behaviour, especially the guards.

The guards are most of the value of these tools over raw API calls: Grocy
itself will happily take stock negative, move a batch out of a shelf it isn't
on, or accept a barcode already belonging to something else.
"""

import pytest

from grocy_mcp.tools import products, shopping, stock


# --- stock -------------------------------------------------------------------

def test_consume_reduces_stock_and_returns_a_transaction_id(fake_grocy):
    out = stock.consume_product("Long-grain Rice", 1)
    assert out["remaining"] == "1 Pack"
    assert out["transaction_id"] == "txn-test"
    assert fake_grocy.amount_of(1) == 1


def test_consume_refuses_to_go_negative(fake_grocy):
    with pytest.raises(ValueError, match="Only 2 Packs"):
        stock.consume_product("Long-grain Rice", 5)
    assert fake_grocy.amount_of(1) == 2, "nothing should have been booked"


def test_consume_reports_when_it_empties_the_shelf(fake_grocy):
    assert stock.consume_product("Long-grain Rice", 2)["out_of_stock"] is True


def test_add_stock_requires_an_iso_date(fake_grocy):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        stock.add_stock("Long-grain Rice", 1, "31/01/2027")
    assert fake_grocy.writes == []


def test_add_stock_sends_the_date_and_reports_the_new_total(fake_grocy):
    out = stock.add_stock("Long-grain Rice", 3, "2027-06-30")
    body = fake_grocy.body_of("POST", "/stock/products/1/add")
    assert body["best_before_date"] == "2027-06-30"
    assert body["transaction_type"] == "purchase"
    assert out["now_in_stock"] == "5 Packs"


def test_correct_stock_up_requires_a_date(fake_grocy):
    with pytest.raises(ValueError, match="best_before_date is required"):
        stock.correct_stock("Long-grain Rice", 5)


def test_correct_stock_down_needs_no_date_and_reports_direction(fake_grocy):
    out = stock.correct_stock("Long-grain Rice", 1)
    assert out["direction"] == "down"
    assert out["difference"] == "1 Pack"
    assert fake_grocy.body_of("POST", "/stock/products/1/inventory")["new_amount"] == 1


def test_correct_stock_to_the_same_amount_books_nothing(fake_grocy):
    out = stock.correct_stock("Long-grain Rice", 2)
    assert "unchanged" in out
    assert fake_grocy.writes == []


def test_correct_stock_rejects_a_negative_count(fake_grocy):
    with pytest.raises(ValueError):
        stock.correct_stock("Long-grain Rice", -1)


def test_transfer_refuses_when_source_and_destination_match(fake_grocy):
    # Long-grain Rice lives in the Pantry (3).
    with pytest.raises(ValueError, match="both"):
        stock.transfer_stock("Long-grain Rice", "Pantry", 1)


def test_transfer_refuses_more_than_is_on_that_shelf(fake_grocy):
    with pytest.raises(ValueError, match="cannot move"):
        stock.transfer_stock("Long-grain Rice", "Fridge", 99)


def test_transfer_infers_the_source_when_there_is_only_one(fake_grocy):
    out = stock.transfer_stock("Long-grain Rice", "Fridge", 1)
    body = fake_grocy.body_of("POST", "/stock/products/1/transfer")
    assert (body["location_id_from"], body["location_id_to"]) == (3, 2)
    assert out["from"] == "Pantry" and out["to"] == "Fridge"


def test_edit_stock_entry_needs_something_to_change(fake_grocy):
    with pytest.raises(ValueError, match="Nothing to change"):
        stock.edit_stock_entry(101)


def test_edit_stock_entry_sends_the_whole_entry_back(fake_grocy):
    """Grocy replaces the entry rather than patching it.

    Sending only the changed field silently zeroes the rest, so the untouched
    fields must be echoed back.
    """
    stock.edit_stock_entry(101, best_before_date="2028-01-01")
    body = fake_grocy.body_of("PUT", "/stock/entry/101")
    assert body["best_before_date"] == "2028-01-01"
    assert set(body) == {"amount", "best_before_date", "location_id", "price", "open"}


def test_undo_requires_a_transaction_id(fake_grocy):
    with pytest.raises(ValueError):
        stock.undo_transaction("  ")


def test_expiring_soon_windows_by_days(fake_grocy):
    # Butter is due 2026-08-21; a huge window catches everything dated.
    names = {i["name"] for i in stock.expiring_soon(days=100000)["items"]}
    assert "Butter" in names


def test_list_stock_filter_miss_explains_itself(fake_grocy):
    out = stock.list_stock(location="cellar")
    assert out["products"] == 0
    assert "known_locations" in out


def test_out_of_stock_lists_catalog_products_at_zero(fake_grocy):
    names = {i["name"] for i in stock.out_of_stock()["items"]}
    assert names == {"Rice Noodles"}


# --- products ----------------------------------------------------------------

def test_create_product_refuses_an_existing_name(fake_grocy):
    with pytest.raises(ValueError, match="already exists"):
        products.create_product("Butter", "Snacks", "Fridge")


def test_create_product_warns_about_near_duplicates(fake_grocy):
    out = products.create_product("Buttr", "Snacks", "Fridge")
    assert out["possible_duplicates"] == ["Butter"]


def test_create_product_sets_all_four_unit_fields(fake_grocy):
    products.create_product("Cardamom", "Spices & Herbs", "Pantry", unit="Gram")
    body = fake_grocy.body_of("POST", "/objects/products")
    assert body["qu_id_stock"] == body["qu_id_purchase"] == 4
    assert body["qu_id_consume"] == body["qu_id_price"] == 4


def test_create_product_rejects_an_unknown_category(fake_grocy):
    with pytest.raises(ValueError, match="not a known category"):
        products.create_product("Cardamom", "Condiments", "Pantry")


def test_update_product_sends_only_what_changed(fake_grocy):
    products.update_product("Butter", description="Salted, 250 g")
    body = fake_grocy.body_of("PUT", "/objects/products/4")
    assert body == {"description": "Salted, 250 g"}


def test_update_product_refuses_a_unit_change_while_stock_is_held(fake_grocy):
    with pytest.raises(ValueError, match="unit cannot be changed"):
        products.update_product("Butter", unit="Gram")


def test_update_product_allows_a_unit_change_at_zero_stock(fake_grocy):
    products.update_product("Rice Noodles", unit="Gram")
    assert fake_grocy.body_of("PUT", "/objects/products/3")["qu_id_stock"] == 4


def test_update_product_refuses_a_name_another_product_holds(fake_grocy):
    with pytest.raises(ValueError, match="already called"):
        products.update_product("Butter", name="Long-grain Rice")


def test_update_product_needs_at_least_one_field(fake_grocy):
    with pytest.raises(ValueError, match="Nothing to change"):
        products.update_product("Butter")


def test_delete_product_refuses_while_stock_is_held(fake_grocy):
    with pytest.raises(ValueError, match="consume_product it down to zero"):
        products.delete_product("Butter")


def test_delete_product_removes_an_empty_one(fake_grocy):
    assert products.delete_product("Rice Noodles")["deleted"] == "Rice Noodles"
    assert all(p["name"] != "Rice Noodles" for p in fake_grocy.products)


def test_add_barcode_refuses_one_belonging_to_another_product(fake_grocy):
    with pytest.raises(ValueError, match="already belongs to"):
        products.add_barcode("Long-grain Rice", "40349558")


def test_add_barcode_is_idempotent_for_the_same_product(fake_grocy):
    out = products.add_barcode("Butter", "40349558")
    assert out["note"] == "Already attached."
    assert fake_grocy.writes == []


def test_add_barcode_rejects_non_digits(fake_grocy):
    with pytest.raises(ValueError, match="not a barcode"):
        products.add_barcode("Butter", "not-a-code")


def test_remove_barcode_reports_which_product_lost_it(fake_grocy):
    assert products.remove_barcode("40349558")["from"] == "Butter"


# --- shopping list -----------------------------------------------------------

def test_add_links_a_known_product(fake_grocy):
    out = shopping.add_to_shopping_list("Butter", 2)
    assert out["kind"] == "product"
    assert fake_grocy.body_of("POST", "/stock/shoppinglist/add-product")["product_id"] == 4


def test_add_writes_a_note_row_for_something_untracked(fake_grocy):
    out = shopping.add_to_shopping_list("kitchen roll")
    assert out["kind"] == "note"
    assert fake_grocy.body_of("POST", "/objects/shopping_list")["note"] == "kitchen roll"


def test_add_refuses_a_near_miss_rather_than_writing_a_note(fake_grocy):
    """A misspelled product must not silently become a note row.

    A note row cannot be matched back to stock, so the mistake surfaces weeks
    later as a duplicate purchase.
    """
    with pytest.raises(ValueError, match="close to"):
        shopping.add_to_shopping_list("Buttr")


def test_check_off_then_clear_removes_only_the_done_row(fake_grocy):
    shopping.add_to_shopping_list("Butter")
    shopping.add_to_shopping_list("Long-grain Rice")
    shopping.check_off_shopping_item("Butter")

    out = shopping.clear_shopping_list(done_only=True)
    assert out["removed"] == 1 and out["still_on_list"] == 1
    assert [r["item"] for r in shopping._rows()] == ["Long-grain Rice"]


def test_remove_takes_a_note_row_off_too(fake_grocy):
    shopping.add_to_shopping_list("kitchen roll")
    assert shopping.remove_from_shopping_list("kitchen roll")["removed"] == "kitchen roll"
    assert shopping._rows() == []


def test_remove_something_not_on_the_list_says_what_is(fake_grocy):
    shopping.add_to_shopping_list("Butter")
    with pytest.raises(ValueError, match="Butter"):
        shopping.remove_from_shopping_list("Marmite")
