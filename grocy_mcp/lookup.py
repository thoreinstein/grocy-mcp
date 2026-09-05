"""Resolving what a person said to what Grocy holds.

Every tool takes products and locations as human text — a name, a fragment of
a description, a barcode, a shelf. This is the layer that turns that into ids,
and, just as importantly, refuses to when the text is ambiguous.

Nothing here is an MCP tool; these raise ValueError with a message written to
be read by the agent that has to fix the call.
"""

from __future__ import annotations

import difflib

from .api import GrocyError, get
from .config import get_config
from .stock import UNCATEGORIZED, compact, fmt_amount, load_items


def matches(value, needle) -> bool:
    return needle.casefold() in (value or "").casefold()


def lookup_by_name(objects, name, kind):
    """Exact case-insensitive lookup by name, or raise with the known set."""
    q = (name or "").strip()
    if not q:
        raise ValueError(f"No {kind} given.")
    hits = [o for o in objects if o["name"].casefold() == q.casefold()]
    if len(hits) == 1:
        return hits[0]
    known = ", ".join(sorted(o["name"] for o in objects))
    raise ValueError(f"{q!r} is not a known {kind}. Known: {known}.")


def locations_by_id() -> dict:
    return {l["id"]: l["name"] for l in get("/objects/locations")}


def resolve_location(value, locations=None) -> int:
    """Accept either a location id or its exact name.

    Tools take locations both ways because a bare integer id is easy to get
    wrong without looking it up first, and requiring the lookup first is a
    round trip for something the caller usually knows by name.
    """
    if locations is None:
        locations = locations_by_id()
    if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
        by_name = {n.casefold(): i for i, n in locations.items()}
        lid = by_name.get(value.strip().casefold())
        if lid is None:
            raise ValueError(
                f"{value!r} is not a known location. Known: "
                + ", ".join(sorted(locations.values()))
            )
        return lid
    try:
        lid = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"location must be an id or location name, got {value!r}.")
    if lid not in locations:
        raise ValueError(
            f"location {lid!r} is not a known location. Known: "
            + ", ".join(f"{n} ({i})" for i, n in sorted(locations.items()))
        )
    return lid


def normalize_name(name) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def possible_duplicates(name, existing_names, threshold=0.85) -> list:
    """Existing names close enough to `name` to be worth a second look.

    Compares normalized (casefolded, punctuation- and space-stripped) forms, so
    "Apple Sauce" vs "Applesauce" scores 1.0 rather than being missed by an
    exact-string check.
    """
    target = normalize_name(name)
    return sorted({
        n for n in existing_names
        if difflib.SequenceMatcher(None, target, normalize_name(n)).ratio() >= threshold
    })


def full_catalog() -> list:
    """Every product Grocy knows, in stock or not, compact-shaped.

    /stock only lists products with stock on hand — a product at zero simply
    does not appear — so this merges it with /objects/products and tags each
    item in_stock: true/false.
    """
    items, _ = load_items()
    in_stock_by_id = {i["product_id"]: i for i in items}

    products = get("/objects/products")
    locations = locations_by_id()
    groups = {g["id"]: g["name"] for g in get("/objects/product_groups")}
    barcodes = {}
    for b in get("/objects/product_barcodes"):
        code = (b.get("barcode") or "").strip()
        if code:
            barcodes.setdefault(b["product_id"], code)

    out = []
    for p in products:
        stocked = in_stock_by_id.get(p["id"])
        if stocked is not None:
            entry = compact(stocked)
            entry["in_stock"] = True
        else:
            entry = {
                "name": p["name"],
                "category": groups.get(p["product_group_id"], UNCATEGORIZED),
                "location": locations.get(p["location_id"], "Other"),
                "in_stock": False,
            }
            desc = (p.get("description") or "").strip()
            if desc:
                entry["desc"] = desc
            bc = barcodes.get(p["id"], "")
            if bc:
                entry["barcode"] = bc
        out.append(entry)
    return out


def resolve(query) -> dict:
    """Resolve a human-typed product reference to {id, name, unit}.

    Searches every product, not just those in stock, so restocking something
    that ran out still works. Raises rather than guessing: a wrong guess
    silently moves the wrong product's stock, which nobody notices for weeks.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("No product given.")

    products = get("/objects/products")
    units = {u["id"]: u["name"] for u in get("/objects/quantity_units")}
    by_barcode = {}
    for b in get("/objects/product_barcodes"):
        code = (b.get("barcode") or "").strip()
        if code:
            by_barcode.setdefault(code, b["product_id"])

    def out(p):
        return {"id": p["id"], "name": p["name"], "unit": units.get(p["qu_id_stock"], "")}

    # 1. Exact barcode.
    pid = by_barcode.get(q)
    if pid is not None:
        for p in products:
            if p["id"] == pid:
                return out(p)

    # 2. Exact name, case-insensitive.
    exact = [p for p in products if p["name"].casefold() == q.casefold()]
    if len(exact) == 1:
        return out(exact[0])

    # 3. Substring over name, then description.
    for field in ("name", "description"):
        hits = [p for p in products if matches(p.get(field), q)]
        if len(hits) == 1:
            return out(hits[0])
        if len(hits) > 1:
            names = ", ".join(sorted(p["name"] for p in hits))
            raise ValueError(
                f"{len(hits)} products match {q!r}: {names}. "
                "Ask which one is meant, then call again with the exact name."
            )

    raise ValueError(
        f"No product matches {q!r}. Call search_products or list_stock to see "
        "what exists."
    )


def stock_amount(product_id) -> float:
    """Current aggregated stock for a product.

    A 404 means the product holds no stock, which is a real answer (0). Any
    other failure is a broken lookup and must propagate rather than be reported
    as zero — reporting zero would let a consume guard pass that should not.
    """
    try:
        detail = get(f"/stock/products/{product_id}", tries=1)
    except GrocyError as exc:
        if exc.code == 404:
            return 0.0
        raise
    try:
        return float((detail or {}).get("stock_amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def amount_after_write(product_id):
    """Re-read stock after a successful write, for reporting only.

    Returns (amount, warning) with exactly one set. This failure must never
    surface as a tool error: the transaction already went through, and an error
    here invites the model to retry it — which, for a non-idempotent consume,
    takes the stock down twice.
    """
    try:
        return stock_amount(product_id), None
    except Exception as exc:
        return None, f"Recorded, but re-reading the new amount failed ({exc}). Do not retry."


def check_amount(amount) -> None:
    if amount is None or amount <= 0:
        raise ValueError(f"amount must be greater than 0, got {amount!r}.")


def transaction_id(result):
    """Pull the transaction id out of a stock write's response.

    Grocy answers add/consume/transfer/inventory with the booking rows it
    created, each carrying the transaction id that groups them. Returning it is
    what makes undo_transaction usable: without it a mistaken consume can only
    be reversed by hand in the web UI.
    """
    rows = result if isinstance(result, list) else [result]
    for row in rows:
        if isinstance(row, dict) and row.get("transaction_id"):
            return row["transaction_id"]
    return None


def conventions() -> str:
    """The reference document every tool docstring points at.

    Locations, categories and units are read live, so a rename in the Grocy web
    UI cannot leave this lying. What each location is for is read live too, from
    the location's own description, falling back to the config file. The rest of
    the advice — how long things keep, how products are named — comes from the
    config file, and is simply absent when there isn't one.
    """
    config = get_config()
    raw_locations = get("/objects/locations")
    locations = {l["id"]: l["name"] for l in raw_locations}

    # What a shelf is for is a property of the shelf, so it belongs on the
    # location record in Grocy: editable from anywhere, no redeploy, and it
    # cannot be orphaned by a rename the way a name-keyed config entry can.
    # config.location_notes remains the fallback for instances that keep their
    # notes there, and for locations whose description is blank.
    descriptions = {
        l["name"]: " ".join((l.get("description") or "").split())
        for l in raw_locations
    }
    groups = [g["name"] for g in get("/objects/product_groups")]
    units = sorted(u["name"] for u in get("/objects/quantity_units"))

    order = {n: i for i, n in enumerate(config.location_order)}
    ordered = sorted(
        locations, key=lambda i: (order.get(locations[i], len(order)), locations[i])
    )
    width = max((len(locations[i]) for i in ordered), default=0) + 8

    lines = []
    for i in ordered:
        label = f"{locations[i]} ({i})"
        note = descriptions.get(locations[i]) or config.location_notes.get(locations[i], "")
        lines.append(f"  {label:<{width}}{note}".rstrip())

    grank = {n: i for i, n in enumerate(config.category_order)}
    ordered_groups = sorted(groups, key=lambda g: (grank.get(g, len(grank)), g.casefold()))

    parts = ["Pantry conventions, read live from this Grocy instance."]
    if config.summary:
        parts.append(config.summary.strip())
    parts.append(
        "Locations (id) — pass either the id or the exact name wherever a\n"
        "location is taken:\n" + "\n".join(lines)
    )
    parts.append(
        "Categories (product groups) — pass the exact name to create_product\n"
        "or update_product:\n  " + ", ".join(ordered_groups)
    )
    parts.append("Quantity units in this instance: " + ", ".join(units))
    parts.append(
        "Amounts are always in the product's own stock unit, so the qty string "
        "from list_stock or search_products is the authority — read it before "
        "consuming rather than assuming the unit."
    )
    for extra in (config.unit_guidance, config.expiry_guidance, config.naming_guidance):
        if extra and extra.strip():
            parts.append(extra.strip())
    return "\n\n".join(parts) + "\n"


def fmt(amount, unit):
    """fmt_amount, tolerant of a missing amount (a failed post-write read)."""
    return None if amount is None else fmt_amount(amount, unit)
