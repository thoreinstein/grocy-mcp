"""Organizer tools: the locations, units and categories products hang off.

A fresh Grocy has one location, two units and no product groups, and
create_product needs a category that already exists. These tools add the
missing ones so a pantry can be set up without the web UI. Renaming or
deleting them stays in the web UI: both ripple through every product that
uses them.
"""

from __future__ import annotations

from ..api import get, post


def _create(entity: str, kind: str, body: dict) -> dict:
    name = body["name"]
    existing = get(f"/objects/{entity}") or []
    clash = next(
        (o for o in existing if o["name"].casefold() == name.casefold()), None
    )
    if clash is not None:
        raise ValueError(
            f"{clash['name']!r} is already a {kind} (id {clash['id']}) — use it "
            "instead of creating another."
        )
    result = post(f"/objects/{entity}", body)
    out = {kind: name}
    created = (result or {}).get("created_object_id")
    if created:
        out["id"] = created
    return out


def _name(value: str, kind: str) -> str:
    q = (value or "").strip()
    if not q:
        raise ValueError(f"No {kind} name given.")
    return q


def create_location(
    name: str, description: str | None = None, is_freezer: bool = False
) -> dict:
    """Add a storage location, e.g. "Pantry", "Freezer", "Garage shelf".

    Check get_conventions first: refuses a name that already exists (ignoring
    case). Locations are where add_stock puts things and what list_stock can
    filter by.

    Args:
        name: Location name as it should appear in Grocy.
        description: Optional note, e.g. which shelf or what belongs there.
        is_freezer: True for a freezer. Grocy uses it to apply freezer
            best-before rules when stock is moved in or out.
    """
    body: dict = {"name": _name(name, "location"), "is_freezer": 1 if is_freezer else 0}
    if description and description.strip():
        body["description"] = description.strip()
    return _create("locations", "location", body)


def create_quantity_unit(name: str, name_plural: str | None = None) -> dict:
    """Add a quantity unit, e.g. "Gram", "Can", "Bottle".

    Check get_conventions first: refuses a name that already exists (ignoring
    case). A product's unit has to exist before create_product can use it.

    Args:
        name: Singular unit name, e.g. "Can".
        name_plural: Plural form, e.g. "Cans". Defaults to the singular.
    """
    q = _name(name, "unit")
    plural = (name_plural or "").strip() or q
    return _create("quantity_units", "unit", {"name": q, "name_plural": plural})


def create_category(name: str, description: str | None = None) -> dict:
    """Add a product category (a Grocy product group), e.g. "Dairy", "Canned goods".

    create_product requires a category that already exists, and a fresh Grocy
    has none, so this comes first when setting up. Check get_conventions:
    refuses a name that already exists (ignoring case).

    Args:
        name: Category name.
        description: Optional note on what belongs in it.
    """
    body: dict = {"name": _name(name, "category")}
    if description and description.strip():
        body["description"] = description.strip()
    return _create("product_groups", "category", body)


TOOLS = [create_location, create_quantity_unit, create_category]


def register(mcp):
    for fn in TOOLS:
        mcp.tool()(fn)
