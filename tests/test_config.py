"""Config loading, and the conventions document it feeds."""

import pytest

from grocy_mcp import config as config_module
from grocy_mcp.config import PantryConfig
from grocy_mcp.lookup import conventions

EXAMPLE = """
[pantry]
summary = "A test kitchen."
soon_days = 3
category_order = ["Spices & Herbs", "Snacks"]
location_order = ["Pantry", "Fridge"]
naming_guidance = "Names in English."

[pantry.location_notes]
"Fridge" = "Cold things"
"""


def test_absent_config_is_valid_and_empty():
    empty = PantryConfig()
    assert empty.location_notes == {} and empty.summary == ""
    assert empty.soon_days == 7


def test_loads_a_file(tmp_path):
    p = tmp_path / "pantry.toml"
    p.write_text(EXAMPLE)
    cfg = PantryConfig.load(p)
    assert cfg.summary == "A test kitchen."
    assert cfg.soon_days == 3
    assert cfg.location_notes == {"Fridge": "Cold things"}
    assert cfg.category_order[0] == "Spices & Herbs"


def test_shipped_example_parses(tmp_path):
    """The example in the repo must actually load — it is what people copy."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "pantry.toml"
    cfg = PantryConfig.load(example)
    assert cfg.location_notes and cfg.expiry_guidance


def test_missing_file_is_an_error_not_a_silent_default(tmp_path):
    # Silently ignoring it would leave the agent working without the
    # household's conventions, which looks fine right up until it files
    # something on the wrong shelf.
    with pytest.raises(FileNotFoundError):
        PantryConfig.load(tmp_path / "nope.toml")


def test_unknown_keys_are_rejected_with_the_known_set():
    with pytest.raises(ValueError, match="Unknown config keys"):
        PantryConfig.from_dict({"pantry": {"locations": ["Fridge"]}})


def test_wrong_shapes_are_rejected():
    with pytest.raises(ValueError, match="location_notes"):
        PantryConfig.from_dict({"pantry": {"location_notes": ["Fridge"]}})
    with pytest.raises(ValueError, match="category_order"):
        PantryConfig.from_dict({"pantry": {"category_order": "Snacks"}})


def test_conventions_reads_locations_live(fake_grocy):
    text = conventions()
    # Ids matter: they are what an agent passes back.
    assert "Fridge (2)" in text and "Pantry (3)" in text
    assert "Snacks" in text and "Spices & Herbs" in text
    assert "Piece" in text and "Gram" in text


def test_conventions_includes_configured_advice(fake_grocy, monkeypatch):
    monkeypatch.setattr(config_module, "_config", PantryConfig(
        summary="A test kitchen.",
        location_notes={"Fridge": "Cold things"},
        location_order=["Pantry", "Fridge"],
        expiry_guidance="Fresh veg keeps a week.",
    ))
    text = conventions()
    assert "A test kitchen." in text
    assert "Cold things" in text
    assert "Fresh veg keeps a week." in text
    # Configured order wins over id order.
    assert text.index("Pantry (3)") < text.index("Fridge (2)")


def test_conventions_without_config_says_nothing_it_cannot_know(fake_grocy):
    text = conventions()
    assert "keeps" not in text
    assert "Locations" in text
