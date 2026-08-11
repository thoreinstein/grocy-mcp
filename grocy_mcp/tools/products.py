"""Product catalog tools: what the pantry knows how to stock.

The catalog is separate from stock. A product exists whether or not there is
any of it on the shelf, which is why search_products and out_of_stock can see
things list_stock cannot. These tools create, edit, describe and retire those
records; the amounts are the stock module's job.
"""

from __future__ import annotations

from ..api import delete, get, post, put
from ..lookup import (
    conventions,
    full_catalog,
    lookup_by_name,
    matches,
    possible_duplicates,
    resolve,
    resolve_location,
    stock_amount,
)
from ..stock import fmt_amount


def _barcode_rows():
    return get("/objects/product_barcodes") or []


def search_products(query: str) -> dict:
    """Find products by name, description, or barcode — across the whole
    catalog, not just what's currently on the shelf.

    A miss means the product genuinely doesn't exist in Grocy. A hit's in_stock
    flag says whether there's actually any on hand right now — a hit with
    in_stock: false is a known product sitting at zero, not a false positive.
    Check this before create_product to catch an existing or near-duplicate
    product, and before add_stock/consume_product when unsure how something is
    named.

    Args:
        query: Case-insensitive substring, or a full GTIN/EAN barcode.
    """
    hits = [
        i for i in full_catalog()
        if matches(i["name"], query)
        or matches(i.get("desc"), query)
        or query.strip() == i.get("barcode", "")
    ]
    hits.sort(key=lambda i: (not i["in_stock"], i["name"].casefold()))
    return {"matches": len(hits), "items": hits}


def get_conventions() -> str:
    """This instance's locations (with ids), categories, quantity units, and
    whatever house rules are configured — the reference every other tool's
    docstring points at.

    Call this once at the start of a session rather than guessing or
    trial-and-erroring an id. Locations, categories and units are read live, so
    this stays right even after something is renamed in the web UI.
    """
    return conventions()


def create_product(
    name: str,
    category: str,
    location_id: int | str,
    unit: str = "Piece",
    description: str | None = None,
    min_stock: float | None = None,
) -> dict:
    """Add a new product to the catalog, for something never stocked before.

    This only creates the product record — call add_stock next to record an
    amount on hand. Refuses rather than guessing if the name already matches an
    existing product; use add_stock for those instead. A near-duplicate name
    (e.g. "Applesauce" vs an existing "Apple Sauce") does not block creation,
    but comes back in possible_duplicates — check that list before assuming the
    new product is really new.

    Args:
        name: Product name. See get_conventions for any naming rule this pantry
            follows.
        category: Product group name, exact match. See get_conventions.
        location_id: Usual storage location — either its id or its exact name.
        unit: Stock/purchase/consume unit name, e.g. "Piece", "Pack", "Gram".
            Must already exist in Grocy; see get_conventions.
        description: Short description identifying the exact item — brand,
            size, variant.
        min_stock: Optional minimum to keep on hand, in the same unit. Setting
            it is what makes below_min_stock and add_missing_to_shopping_list
            work for this product.
    """
    q = (name or "").strip()
    if not q:
        raise ValueError("No product name given.")

    all_products = get("/objects/products")

    try:
        dup = resolve(q)
    except ValueError as exc:
        if not str(exc).startswith("No product matches"):
            raise
        dup = None
    if dup is not None:
        raise ValueError(
            f"{dup['name']!r} already exists — use add_stock to record stock "
            "for it instead of create_product."
        )

    group = lookup_by_name(get("/objects/product_groups"), category, "category")
    qu = lookup_by_name(get("/objects/quantity_units"), unit, "unit")

    locations = {l["id"]: l["name"] for l in get("/objects/locations")}
    lid = resolve_location(location_id, locations)

    body = {
        "name": q,
        "product_group_id": group["id"],
        "location_id": lid,
        # Grocy 4.x wants stock/purchase/consume/price units set individually
        # rather than a conversion factor. One unit for all four is right
        # whenever a pantry doesn't need purchase-vs-stock conversion; set up
        # conversions in the web UI if it does.
        "qu_id_stock": qu["id"],
        "qu_id_purchase": qu["id"],
        "qu_id_consume": qu["id"],
        "qu_id_price": qu["id"],
    }
    if description and description.strip():
        body["description"] = description.strip()
    if min_stock is not None:
        if min_stock < 0:
            raise ValueError(f"min_stock cannot be negative, got {min_stock!r}.")
        body["min_stock_amount"] = min_stock

    result = post("/objects/products", body)
    product_id = (result or {}).get("created_object_id")

    out = {
        "product": q,
        "category": group["name"],
        "location": locations[lid],
        "unit": qu["name"],
        "note": "Created with no stock yet — call add_stock to record an amount.",
    }
    if product_id:
        out["product_id"] = product_id
    if min_stock is not None:
        out["min_stock"] = fmt_amount(min_stock, qu["name"])
    similar = possible_duplicates(q, [p["name"] for p in all_products])
    if similar:
        out["possible_duplicates"] = similar
    return out


def update_product(
    product: str,
    name: str | None = None,
    description: str | None = None,
    category: str | None = None,
    location_id: int | str | None = None,
    unit: str | None = None,
    min_stock: float | None = None,
) -> dict:
    """Edit an existing product's catalog record.

    For fixing a typo, recategorising something, moving its default shelf, or
    setting a minimum stock level. Only the arguments given are changed. This
    does not touch stock amounts — use add_stock, consume_product or
    correct_stock for those.

    Args:
        product: Product name, description fragment, or barcode identifying the
            product to edit.
        name: New name.
        description: New description — brand, size, variant.
        category: New product group, exact name. See get_conventions.
        location_id: New default location, id or exact name.
        unit: New stock unit. Refused while any stock is on hand, since Grocy
            would reinterpret the existing amount in the new unit — 2 Packs
            silently becoming 2 Grams. Consume to zero first, or create a
            separate product.
        min_stock: Minimum to keep on hand, in the product's stock unit. Zero
            clears it. This is what below_min_stock and
            add_missing_to_shopping_list read.
    """
    given = (name, description, category, location_id, unit, min_stock)
    if all(v is None for v in given):
        raise ValueError("Nothing to change — pass at least one field.")

    p = resolve(product)
    current = get(f"/objects/products/{p['id']}")
    if not current:
        raise ValueError(f"Product {p['name']!r} disappeared between lookups.")

    # Only the changed columns are sent: PUT /objects/<entity>/<id> updates
    # what it is given, so echoing the whole record back would rewrite fields
    # this tool has no business touching.
    body: dict = {}
    changed: dict = {}

    if name is not None:
        new = name.strip()
        if not new:
            raise ValueError("name cannot be blank.")
        clash = [
            o for o in get("/objects/products")
            if o["name"].casefold() == new.casefold() and o["id"] != p["id"]
        ]
        if clash:
            raise ValueError(
                f"Another product is already called {clash[0]['name']!r}. "
                "Grocy requires unique names — pick a different one, or "
                "delete_product the duplicate."
            )
        body["name"] = new
        changed["name"] = new

    if description is not None:
        body["description"] = description.strip()
        changed["description"] = body["description"]

    if category is not None:
        group = lookup_by_name(get("/objects/product_groups"), category, "category")
        body["product_group_id"] = group["id"]
        changed["category"] = group["name"]

    if location_id is not None:
        locations = {l["id"]: l["name"] for l in get("/objects/locations")}
        body["location_id"] = resolve_location(location_id, locations)
        changed["location"] = locations[body["location_id"]]

    if unit is not None:
        on_hand = stock_amount(p["id"])
        if on_hand > 0:
            raise ValueError(
                f"{p['name']} still has {fmt_amount(on_hand, p['unit'])} in "
                "stock, so its unit cannot be changed — the existing amount "
                "would be reinterpreted in the new unit. Consume it to zero "
                "first."
            )
        qu = lookup_by_name(get("/objects/quantity_units"), unit, "unit")
        for field in ("qu_id_stock", "qu_id_purchase", "qu_id_consume", "qu_id_price"):
            body[field] = qu["id"]
        changed["unit"] = qu["name"]

    if min_stock is not None:
        if min_stock < 0:
            raise ValueError(f"min_stock cannot be negative, got {min_stock!r}.")
        body["min_stock_amount"] = min_stock
        changed["min_stock"] = fmt_amount(min_stock, changed.get("unit", p["unit"]))

    put(f"/objects/products/{p['id']}", body)
    return {"product": changed.get("name", p["name"]), "changed": changed}


def add_barcode(product: str, barcode: str) -> dict:
    """Attach a GTIN/EAN barcode to a product.

    Once attached, that code resolves to this product everywhere a tool takes a
    product argument, so a scanned package can be consumed or restocked without
    typing a name.

    Args:
        product: Product name or description fragment.
        barcode: The full GTIN/EAN as printed under the bars.
    """
    code = (barcode or "").strip()
    if not code:
        raise ValueError("No barcode given.")
    if not code.isdigit():
        raise ValueError(f"{code!r} is not a barcode — expected digits only.")

    p = resolve(product)
    names = {o["id"]: o["name"] for o in get("/objects/products")}
    for row in _barcode_rows():
        if (row.get("barcode") or "").strip() != code:
            continue
        if row["product_id"] == p["id"]:
            return {"product": p["name"], "barcode": code, "note": "Already attached."}
        # Sharing a barcode across products makes every later scan resolve to
        # whichever row is found first, which is silent and near-impossible to
        # notice from the pantry side.
        raise ValueError(
            f"Barcode {code} already belongs to "
            f"{names.get(row['product_id'], row['product_id'])!r}. Remove it "
            "there first if it really is this product."
        )

    post("/objects/product_barcodes", {"product_id": p["id"], "barcode": code})
    return {"product": p["name"], "barcode": code}


def remove_barcode(barcode: str) -> dict:
    """Detach a barcode that was attached to the wrong product.

    Args:
        barcode: The code to remove. It is removed from whichever product
            currently holds it.
    """
    code = (barcode or "").strip()
    if not code:
        raise ValueError("No barcode given.")

    names = {o["id"]: o["name"] for o in get("/objects/products")}
    for row in _barcode_rows():
        if (row.get("barcode") or "").strip() == code:
            delete(f"/objects/product_barcodes/{row['id']}")
            return {
                "removed": code,
                "from": names.get(row["product_id"], row["product_id"]),
            }
    raise ValueError(f"No product carries barcode {code!r}.")


def delete_product(product: str) -> dict:
    """Permanently remove a product with zero stock from the catalog.

    Refuses if any stock is on hand — consume_product it down to zero first.
    This is for cleaning up mistakes (a test product, a typo'd duplicate caught
    by search_products or create_product's possible_duplicates), not for
    retiring a product still in use.

    Args:
        product: Product name, description fragment, or barcode. Must identify
            exactly one product.
    """
    p = resolve(product)
    on_hand = stock_amount(p["id"])
    if on_hand > 0:
        raise ValueError(
            f"{p['name']} still has {fmt_amount(on_hand, p['unit'])} in "
            "stock — consume_product it down to zero before deleting."
        )
    delete(f"/objects/products/{p['id']}")
    return {"deleted": p["name"]}


TOOLS = [
    search_products,
    get_conventions,
    create_product,
    update_product,
    add_barcode,
    remove_barcode,
    delete_product,
]


def register(mcp):
    for fn in TOOLS:
        mcp.tool()(fn)
