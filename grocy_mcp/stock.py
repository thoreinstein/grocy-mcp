"""Reading stock out of Grocy and shaping it for display.

One implementation of "how much is there, and when does it go off", so every
consumer — the MCP tools, and anything else built on this package — agrees.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from .api import get
from .config import get_config

UNCATEGORIZED = "Uncategorized"

# Grocy's stand-in date for "never expires". Anything at or past it is not a
# real best-before date and is reported as no date at all.
NEVER_EXPIRES = "2999-12-31"


def today() -> date:
    return datetime.now(timezone.utc).date()


def fmt_amount(amount, unit) -> str:
    """Format an amount in its unit the way a person would say it."""
    n = f"{amount:g}"
    if unit == "Gram":
        return f"{n} g"
    if unit == "Kilogram":
        return f"{n} kg"
    if not unit:
        return n
    return f"{n} {unit}" + ("s" if amount != 1 and not unit.endswith("s") else "")


def expiry_info(bbd, on=None):
    """Return (css_class, label) for a best-before date string."""
    on = on or today()
    if not bbd or bbd >= NEVER_EXPIRES:
        return "", ""
    d = date.fromisoformat(bbd)
    days = (d - on).days
    if days < 0:
        return "expired", f"expired {-days} d ago"
    if days <= get_config().soon_days:
        return "soon", f"{days} d left"
    if days <= 60:
        return "", f"{d:%d %b}"
    return "", f"{d:%b %Y}"


def is_food(group_name: str, config=None) -> bool:
    """Whether a product group holds consumables.

    With no `food_categories` configured every group is food, which is right
    for an instance that only holds a pantry. Once the list is set, anything
    outside it — uncategorized products included — is a durable possession.
    See PantryConfig.food_categories for why that default direction.
    """
    allowed = (config or get_config()).food_categories
    return not allowed or group_name in allowed


def _ordered(names, preferred):
    """`names` sorted by `preferred` first, then alphabetically."""
    rank = {n: i for i, n in enumerate(preferred)}
    return sorted(names, key=lambda n: (rank.get(n, len(rank)), n.casefold()))


def load_items():
    """Fetch current stock and shape it into item dicts.

    Returns (items, meta). `meta` carries the id->name lookups plus the
    category and location display order already resolved to names.

    Only products with stock on hand appear — Grocy's /stock endpoint does not
    list a product sitting at zero. Use lookup.full_catalog() when the empties
    matter too.
    """
    stock = get("/stock")
    locations = {l["id"]: l["name"] for l in get("/objects/locations")}
    groups = {g["id"]: g["name"] for g in get("/objects/product_groups")}
    units = {u["id"]: u["name"] for u in get("/objects/quantity_units")}

    # Primary barcode (GTIN/EAN) per product, so agents can identify the exact
    # package. A product may carry several; keep the first.
    barcodes = {}
    for b in get("/objects/product_barcodes"):
        code = (b.get("barcode") or "").strip()
        if code:
            barcodes.setdefault(b["product_id"], code)

    now = today()
    config = get_config()
    items = []
    for entry in stock:
        p = entry["product"]
        unit = units.get(p["qu_id_stock"], "")
        bbd = entry.get("best_before_date") or ""
        cls, label = expiry_info(bbd, now)
        real_bbd = "" if bbd >= NEVER_EXPIRES else bbd
        group = groups.get(p["product_group_id"], UNCATEGORIZED)
        items.append({
            "product_id": p["id"],
            "name": p["name"],
            "desc": (p.get("description") or "").strip(),
            "barcode": barcodes.get(p["id"], ""),
            "group": group,
            "food": is_food(group, config),
            "location": locations.get(p["location_id"], "Other"),
            "unit": unit,
            "amount": fmt_amount(entry["amount_aggregated"], unit),
            "amount_num": entry["amount_aggregated"],
            "opened": entry.get("amount_opened_aggregated") or 0,
            "bbd": real_bbd,
            "cls": cls,
            "expiry": label,
            "days_left": (date.fromisoformat(real_bbd) - now).days if real_bbd else None,
        })

    meta = {
        "locations": locations,
        "groups": groups,
        "units": units,
        "location_order": _ordered(locations.values(), config.location_order),
        "group_order": _ordered(groups.values(), config.category_order),
        "today": now,
    }
    return items, meta


def compact(i):
    """The per-item shape handed to agents."""
    out = {
        "name": i["name"],
        "qty": i["amount"],
        "category": i["group"],
        "location": i["location"],
        "best_before": i["bbd"] or None,
    }
    if i["desc"]:
        out["desc"] = i["desc"]
    if i["barcode"]:
        out["barcode"] = i["barcode"]
    if i["opened"]:
        out["opened"] = True
    return out
