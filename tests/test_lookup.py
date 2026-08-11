"""Resolution and its refusals.

These are the tests that matter most: resolve() guessing wrong is the failure
mode that silently moves the wrong product's stock, and nobody notices for
weeks.
"""

import pytest

from grocy_mcp import lookup
from grocy_mcp.stock import fmt_amount


def test_resolves_exact_name(fake_grocy):
    assert lookup.resolve("Long-grain Rice")["id"] == 1


def test_resolves_case_insensitively(fake_grocy):
    assert lookup.resolve("long-grain rice")["id"] == 1


def test_resolves_by_barcode(fake_grocy):
    assert lookup.resolve("40349558")["name"] == "Butter"


def test_resolves_by_description_fragment(fake_grocy):
    assert lookup.resolve("Ground turmeric")["id"] == 2


def test_refuses_ambiguous_name_and_names_the_candidates(fake_grocy):
    # "Rice" matches both "Long-grain Rice" and "Rice Noodles".
    with pytest.raises(ValueError) as exc:
        lookup.resolve("Rice")
    message = str(exc.value)
    assert "Long-grain Rice" in message and "Rice Noodles" in message


def test_exact_name_wins_over_substring(fake_grocy):
    """An exact match must not be defeated by also being a substring of another.

    Without the exact-match pass, asking for the product actually called
    "Rice Noodles" would be ambiguous against "Long-grain Rice".
    """
    assert lookup.resolve("Rice Noodles")["id"] == 3


def test_unknown_product_raises_with_a_recognisable_prefix(fake_grocy):
    # Callers branch on this prefix to tell "not found" from "ambiguous".
    with pytest.raises(ValueError, match="^No product matches"):
        lookup.resolve("Marmite")


def test_empty_query_refused(fake_grocy):
    with pytest.raises(ValueError):
        lookup.resolve("   ")


def test_resolve_location_accepts_id_or_name(fake_grocy):
    assert lookup.resolve_location(2) == 2
    assert lookup.resolve_location("Fridge") == 2
    assert lookup.resolve_location("fridge") == 2


def test_resolve_location_lists_the_known_set_when_wrong(fake_grocy):
    with pytest.raises(ValueError) as exc:
        lookup.resolve_location("Cellar")
    assert "Fridge" in str(exc.value) and "Pantry" in str(exc.value)


def test_resolve_location_rejects_unknown_id(fake_grocy):
    with pytest.raises(ValueError):
        lookup.resolve_location(99)


def test_stock_amount_reports_zero_for_unstocked_product(fake_grocy):
    assert lookup.stock_amount(3) == 0.0


def test_amount_after_write_reports_failure_without_raising(fake_grocy, monkeypatch):
    """A failed post-write read must not look like a failed write.

    Raising here would invite a retry of a non-idempotent consume, which would
    take the stock down twice.
    """
    def boom(_pid):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(lookup, "stock_amount", boom)
    amount, warning = lookup.amount_after_write(1)
    assert amount is None
    assert "Do not retry" in warning


def test_possible_duplicates_sees_past_spacing_and_case():
    assert lookup.possible_duplicates("Applesauce", ["Apple Sauce"]) == ["Apple Sauce"]
    assert lookup.possible_duplicates("Rice", ["Butter"]) == []


def test_check_amount_rejects_zero_and_negative():
    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            lookup.check_amount(bad)


def test_transaction_id_reads_grocys_booking_rows():
    assert lookup.transaction_id([{"transaction_id": "abc"}]) == "abc"
    assert lookup.transaction_id({"transaction_id": "abc"}) == "abc"
    assert lookup.transaction_id([]) is None
    assert lookup.transaction_id(None) is None


def test_fmt_amount_uses_symbols_for_weights_and_plurals_otherwise():
    assert fmt_amount(100, "Gram") == "100 g"
    assert fmt_amount(1.5, "Kilogram") == "1.5 kg"
    assert fmt_amount(1, "Pack") == "1 Pack"
    assert fmt_amount(2, "Pack") == "2 Packs"
    assert fmt_amount(3, "") == "3"
