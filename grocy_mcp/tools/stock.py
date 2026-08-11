"""Stock tools: what is on the shelf, and every way it changes.

Grocy holds stock as separate entries — one per purchase, each with its own
best-before date and location — and aggregates them for display. Most tools
work on the aggregate, which is what people mean by "how much is there". The
entry-level tools exist for the cases where the aggregate is the wrong unit of
work: a single pack with the wrong date on it, or one of three packs moving to
the freezer.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..api import get, post, put, query
from ..config import get_config
from ..lookup import (
    amount_after_write,
    check_amount,
    full_catalog,
    locations_by_id,
    matches,
    resolve,
    resolve_location,
    stock_amount,
    transaction_id,
)
from ..stock import compact, fmt_amount, load_items, today

# Grocy's transaction_type values, in the words a person would use.
TRANSACTION_TYPES = {
    "purchase": "bought",
    "consume": "used",
    "inventory-correction": "corrected",
    "product-opened": "opened",
    "transfer_from": "moved out of",
    "transfer_to": "moved into",
    "stock-edit-old": "edited (before)",
    "stock-edit-new": "edited (after)",
    "self-production": "made",
}


def _parse_date(value, field="best_before_date"):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}.")


def _entry(row, locations):
    """Shape one Grocy stock entry (one purchase batch) for an agent."""
    out = {
        "entry_id": row["id"],
        "amount": row.get("amount"),
        "best_before": row.get("best_before_date"),
        "location": locations.get(row.get("location_id"), "Other"),
    }
    if row.get("purchased_date"):
        out["purchased"] = row["purchased_date"]
    if row.get("open") in (1, "1", True):
        out["opened"] = True
    return out


# --- reads -------------------------------------------------------------------

def list_stock(location: str | None = None, category: str | None = None) -> dict:
    """List everything currently in the pantry, read live from Grocy.

    This is always current — it queries Grocy on every call, so there is no
    need to check a timestamp or work around a cache.

    Args:
        location: Optional case-insensitive substring, e.g. "fridge", "freezer".
            Omit for everything.
        category: Optional product-group substring, e.g. "spices", "dairy".
    """
    items, _meta = load_items()
    selected = items
    if location:
        selected = [i for i in selected if matches(i["location"], location)]
    if category:
        selected = [i for i in selected if matches(i["group"], category)]

    if not selected and (location or category):
        return {
            "products": 0,
            "items": [],
            "note": "No items matched that filter — it may not be a real "
                    "location or category rather than the shelf being empty.",
            "known_locations": sorted({i["location"] for i in items}),
            "known_categories": sorted({i["group"] for i in items}),
        }

    selected.sort(key=lambda i: (i["group"], i["name"].casefold()))
    return {"products": len(selected), "items": [compact(i) for i in selected]}


def expiring_soon(days: int | None = None) -> dict:
    """List items already expired or due within N days, worst first.

    Args:
        days: Look-ahead window in days. Defaults to the configured window.
    """
    days = get_config().soon_days if days is None else days
    items, _ = load_items()
    hits = [i for i in items if i["days_left"] is not None and i["days_left"] <= days]
    hits.sort(key=lambda i: (i["days_left"], i["name"].casefold()))
    return {
        "window_days": days,
        "expired": sum(1 for i in hits if i["days_left"] < 0),
        "items": [{**compact(i), "days_left": i["days_left"]} for i in hits],
    }


def out_of_stock() -> dict:
    """List products with zero stock — candidates for the next shopping trip.

    This is "there is none left at all", and it needs no minimum stock levels
    to be set. For products that still have some but have fallen under a
    configured minimum, use below_min_stock.
    """
    empty = [i for i in full_catalog() if not i["in_stock"]]
    for i in empty:
        del i["in_stock"]
    empty.sort(key=lambda i: (i["category"], i["name"].casefold()))
    return {"products": len(empty), "items": empty}


def below_min_stock() -> dict:
    """List products that have fallen below their minimum stock amount.

    This is Grocy's own "missing products" calculation, so it only sees
    products that have a minimum set (update_product's min_stock). In an
    instance where few products have one, an empty result means "no minimums
    are set" at least as often as it means "nothing is low" — out_of_stock is
    the one that works regardless.
    """
    volatile = get("/stock/volatile") or {}
    missing = volatile.get("missing_products") or []
    units = {u["id"]: u["name"] for u in get("/objects/quantity_units")}
    unit_by_product = {
        p["id"]: units.get(p["qu_id_stock"], "") for p in get("/objects/products")
    }

    items = []
    for m in missing:
        pid = m.get("id")
        amount = float(m.get("amount_missing") or 0)
        items.append({
            "name": m.get("name"),
            "short_by": fmt_amount(amount, unit_by_product.get(pid, "")),
            "some_left": bool(int(m.get("is_partly_in_stock") or 0)),
        })
    items.sort(key=lambda i: (i["name"] or "").casefold())
    return {
        "products": len(items),
        "items": items,
        "note": None if items else
                "Nothing is below its minimum. Note that products without a "
                "minimum set are invisible to this — check out_of_stock too.",
    }


def list_stock_entries(product: str) -> dict:
    """List a product's individual stock entries — one per purchase batch.

    list_stock shows the total; this shows what that total is made of, since
    each batch carries its own best-before date and location. Call this to get
    an entry_id before edit_stock_entry, or to see which pack expires first.

    Args:
        product: Product name, description fragment, or barcode.
    """
    p = resolve(product)
    rows = get(f"/stock/products/{p['id']}/entries") or []
    locations = locations_by_id()
    entries = [_entry(r, locations) for r in rows]
    entries.sort(key=lambda e: (e["best_before"] or "9999-12-31", e["entry_id"]))
    return {
        "product": p["name"],
        "unit": p["unit"],
        "entries": len(entries),
        "items": entries,
    }


def product_details(product: str) -> dict:
    """Everything Grocy knows about one product: stock, dates, and history.

    Includes when it was last bought and last used, its average shelf life and
    spoil rate, the amount currently open, and its minimum stock level — the
    context for deciding whether to restock something or stop buying it.

    Args:
        product: Product name, description fragment, or barcode.
    """
    p = resolve(product)
    d = get(f"/stock/products/{p['id']}") or {}
    prod = d.get("product") or {}
    locations = locations_by_id()

    out = {
        "product": p["name"],
        "unit": p["unit"],
        "in_stock": fmt_amount(float(d.get("stock_amount") or 0), p["unit"]),
    }
    if prod.get("description"):
        out["desc"] = prod["description"].strip()
    if prod.get("location_id"):
        out["default_location"] = locations.get(int(prod["location_id"]), "Other")

    opened = float(d.get("stock_amount_opened") or 0)
    if opened:
        out["opened"] = fmt_amount(opened, p["unit"])

    min_stock = float(prod.get("min_stock_amount") or 0)
    out["min_stock"] = fmt_amount(min_stock, p["unit"]) if min_stock else None

    # Grocy has moved these keys around between releases, so take what is
    # there and leave out what isn't rather than reporting a confident null.
    # The per-key extras are Grocy's "not known" sentinels: -1 shelf life for a
    # product that has never been used up, 0 price for one whose price was
    # never recorded. Passed through as-is they read as real measurements.
    for key, label, unknown in (
        ("last_purchased", "last_bought", ()),
        ("last_used", "last_used", ()),
        ("next_due_date", "next_due", ()),
        ("average_shelf_life_days", "average_shelf_life_days", (-1, "-1")),
        ("spoil_rate_percent", "spoil_rate_percent", ()),
        ("last_price", "last_price", (0, "0", 0.0)),
        ("avg_price", "average_price", (0, "0", 0.0)),
    ):
        value = d.get(key)
        if value in (None, "", "0000-00-00") or value in unknown:
            continue
        out[label] = value

    barcodes = [
        b["barcode"] for b in (d.get("product_barcodes") or [])
        if (b.get("barcode") or "").strip()
    ]
    if barcodes:
        out["barcodes"] = barcodes
    return out


def stock_history(days: int = 30, product: str | None = None, limit: int = 100) -> dict:
    """What has been bought, used, opened or corrected recently.

    Grocy's stock journal, newest first. Use it to answer "what did we get
    through this week", to check a suspicious amount, or to find the
    transaction_id of something that needs undoing.

    Args:
        days: How far back to look. Defaults to a month.
        product: Optional — restrict to one product.
        limit: Maximum rows returned, newest first. Defaults to 100.
    """
    since = today() - timedelta(days=max(days, 0))
    filters = [f"row_created_timestamp>{since.isoformat()} 00:00:00"]
    resolved = None
    if product:
        resolved = resolve(product)
        filters.append(f"product_id={resolved['id']}")

    rows = query(
        "/objects/stock_log",
        filters=filters,
        order="row_created_timestamp:desc",
        limit=max(1, min(limit, 500)),
    ) or []

    products = get("/objects/products")
    names = {p["id"]: p["name"] for p in products}
    units = {u["id"]: u["name"] for u in get("/objects/quantity_units")}
    unit_by_product = {p["id"]: units.get(p["qu_id_stock"], "") for p in products}

    items = []
    for r in rows:
        pid = r.get("product_id")
        amount = float(r.get("amount") or 0)
        entry = {
            "when": (r.get("row_created_timestamp") or "")[:16],
            "product": names.get(pid, f"product {pid}"),
            "what": TRANSACTION_TYPES.get(
                r.get("transaction_type"), r.get("transaction_type")
            ),
            "amount": fmt_amount(abs(amount), unit_by_product.get(pid, "")),
            "transaction_id": r.get("transaction_id"),
        }
        if str(r.get("spoiled") or "0") not in ("0", ""):
            entry["spoiled"] = True
        if str(r.get("undone") or "0") not in ("0", ""):
            entry["undone"] = True
        items.append(entry)

    return {
        "since": since.isoformat(),
        "product": resolved["name"] if resolved else None,
        "rows": len(items),
        "items": items,
    }


# --- writes ------------------------------------------------------------------

def add_stock(
    product: str,
    amount: float,
    best_before_date: str,
    location_id: int | str | None = None,
) -> dict:
    """Record newly bought stock, and report the new total.

    Args:
        product: Product name, description fragment, or barcode. Must identify
            exactly one existing product — this does not create new products.
        amount: How much, in the product's own stock unit.
        best_before_date: YYYY-MM-DD. Use the package date when it is known;
            otherwise estimate it, and see get_conventions for whatever
            estimates this pantry uses.
        location_id: Where it is being put, if not the product's usual spot —
            either its id or its exact name.
    """
    check_amount(amount)
    parsed = _parse_date(best_before_date)

    p = resolve(product)
    body = {
        "amount": amount,
        "transaction_type": "purchase",
        "best_before_date": parsed.isoformat(),
    }
    if location_id is not None:
        body["location_id"] = resolve_location(location_id)

    result = post(f"/stock/products/{p['id']}/add", body)
    total, warning = amount_after_write(p["id"])
    out = {
        "product": p["name"],
        "added": fmt_amount(amount, p["unit"]),
        "best_before": parsed.isoformat(),
        "days_until_expiry": (parsed - today()).days,
        "transaction_id": transaction_id(result),
    }
    out["note" if warning else "now_in_stock"] = warning or fmt_amount(total, p["unit"])
    return out


def consume_product(product: str, amount: float = 1, spoiled: bool = False) -> dict:
    """Record that stock was used up, and report what is left.

    Args:
        product: Product name, description fragment, or barcode. Must identify
            exactly one product — this raises rather than guessing between two.
        amount: How much, in the product's own stock unit. Items tracked by
            weight need the weight, so "a teaspoon of turmeric" is roughly
            amount=5, not 1. Check list_stock's qty first if unsure.
        spoiled: True when it was thrown away rather than eaten.
    """
    check_amount(amount)
    p = resolve(product)
    on_hand = stock_amount(p["id"])
    if amount > on_hand:
        raise ValueError(
            f"Only {fmt_amount(on_hand, p['unit'])} of {p['name']} in stock, "
            f"cannot consume {fmt_amount(amount, p['unit'])}. Consume the "
            "remaining amount instead, or check the product is the right one."
        )

    result = post(
        f"/stock/products/{p['id']}/consume",
        {"amount": amount, "transaction_type": "consume", "spoiled": bool(spoiled)},
    )
    remaining, warning = amount_after_write(p["id"])
    out = {
        "product": p["name"],
        "consumed": fmt_amount(amount, p["unit"]),
        "spoiled": bool(spoiled),
        "transaction_id": transaction_id(result),
    }
    if warning:
        out["note"] = warning
    else:
        out["remaining"] = fmt_amount(remaining, p["unit"])
        out["out_of_stock"] = remaining <= 0
    return out


def open_product(product: str, amount: float = 1) -> dict:
    """Mark stock as opened, without consuming it.

    Opened items usually keep for less time than the printed best-before date,
    so flagging them helps expiring_soon stay useful.

    Args:
        product: Product name, description fragment, or barcode.
        amount: How many units were opened, in the product's stock unit.
    """
    check_amount(amount)
    p = resolve(product)
    on_hand = stock_amount(p["id"])
    if amount > on_hand:
        raise ValueError(
            f"Only {fmt_amount(on_hand, p['unit'])} of {p['name']} in stock, "
            f"cannot open {fmt_amount(amount, p['unit'])}."
        )

    result = post(f"/stock/products/{p['id']}/open", {"amount": amount})
    return {
        "product": p["name"],
        "opened": fmt_amount(amount, p["unit"]),
        "transaction_id": transaction_id(result),
    }


def correct_stock(
    product: str,
    actual_amount: float,
    best_before_date: str | None = None,
    location_id: int | str | None = None,
) -> dict:
    """Set stock to the amount actually counted on the shelf.

    This is the "I looked, and there are three" fix, for when the tracked
    amount has drifted from reality — someone used something without recording
    it, or a purchase was entered twice. It books the difference either way, so
    it can correct upward as well as down. To record ordinary use, prefer
    consume_product; to record a purchase, prefer add_stock.

    Args:
        product: Product name, description fragment, or barcode.
        actual_amount: The amount really there, in the product's stock unit.
            Zero is allowed and clears the product's stock.
        best_before_date: YYYY-MM-DD. Required when correcting upward, since
            the extra stock is a new entry and needs a date.
        location_id: Where the corrected stock is, if not the usual spot.
    """
    if actual_amount is None or actual_amount < 0:
        raise ValueError(f"actual_amount cannot be negative, got {actual_amount!r}.")

    p = resolve(product)
    before = stock_amount(p["id"])
    if actual_amount == before:
        return {
            "product": p["name"],
            "unchanged": fmt_amount(before, p["unit"]),
            "note": "Tracked amount already matches — nothing booked.",
        }

    body = {"new_amount": actual_amount}
    if actual_amount > before:
        if not best_before_date:
            raise ValueError(
                f"Correcting {p['name']} up from {fmt_amount(before, p['unit'])} "
                f"to {fmt_amount(actual_amount, p['unit'])} adds stock, so "
                "best_before_date is required."
            )
        body["best_before_date"] = _parse_date(best_before_date).isoformat()
    if location_id is not None:
        body["location_id"] = resolve_location(location_id)

    result = post(f"/stock/products/{p['id']}/inventory", body)
    after, warning = amount_after_write(p["id"])
    out = {
        "product": p["name"],
        "was": fmt_amount(before, p["unit"]),
        "now": fmt_amount(actual_amount, p["unit"]),
        "difference": fmt_amount(abs(actual_amount - before), p["unit"]),
        "direction": "up" if actual_amount > before else "down",
        "transaction_id": transaction_id(result),
    }
    if warning:
        out["note"] = warning
    elif after != actual_amount:
        out["note"] = (
            f"Grocy now reports {fmt_amount(after, p['unit'])}, not the "
            "requested amount — check list_stock_entries."
        )
    return out


def transfer_stock(
    product: str,
    to_location: int | str,
    amount: float = 1,
    from_location: int | str | None = None,
) -> dict:
    """Move stock from one location to another — freezer to fridge, and so on.

    This changes where something is, not how much there is. Both locations take
    either an id or an exact name.

    Args:
        product: Product name, description fragment, or barcode.
        to_location: Where it is going.
        amount: How much is moving, in the product's stock unit.
        from_location: Where it is coming from. Optional when the product is
            only stored in one place; required when its stock is split, since
            guessing would move the wrong batch.
    """
    check_amount(amount)
    p = resolve(product)
    locations = locations_by_id()
    dest = resolve_location(to_location, locations)

    held = get(f"/stock/products/{p['id']}/locations") or []
    by_location: dict[int, float] = {}
    for row in held:
        # Rows are per stock entry, so two batches on the same shelf arrive as
        # two rows — they have to be added up, not overwritten. Note `id` here
        # is the entry, not the location.
        lid = row.get("location_id")
        if lid is None:
            continue
        by_location[int(lid)] = by_location.get(int(lid), 0) + float(row.get("amount") or 0)
    by_location = {lid: amt for lid, amt in by_location.items() if amt > 0}

    if from_location is not None:
        source = resolve_location(from_location, locations)
    elif len(by_location) == 1:
        source = next(iter(by_location))
    elif not by_location:
        raise ValueError(f"{p['name']} has no stock to move.")
    else:
        where = ", ".join(
            f"{locations.get(lid, lid)}: {fmt_amount(amt, p['unit'])}"
            for lid, amt in sorted(by_location.items())
        )
        raise ValueError(
            f"{p['name']} is stored in more than one place ({where}) — pass "
            "from_location to say which one it is moving out of."
        )

    if source == dest:
        raise ValueError(
            f"from_location and to_location are both {locations.get(dest, dest)!r}."
        )
    available = by_location.get(source, 0)
    if amount > available:
        raise ValueError(
            f"Only {fmt_amount(available, p['unit'])} of {p['name']} in "
            f"{locations.get(source, source)}, cannot move "
            f"{fmt_amount(amount, p['unit'])}."
        )

    result = post(
        f"/stock/products/{p['id']}/transfer",
        {"amount": amount, "location_id_from": source, "location_id_to": dest},
    )
    return {
        "product": p["name"],
        "moved": fmt_amount(amount, p["unit"]),
        "from": locations.get(source, source),
        "to": locations.get(dest, dest),
        "transaction_id": transaction_id(result),
    }


def edit_stock_entry(
    entry_id: int,
    best_before_date: str | None = None,
    location_id: int | str | None = None,
    amount: float | None = None,
) -> dict:
    """Fix one stock entry: its date, its location, or its amount.

    For correcting a best-before date that was estimated wrong, or a batch
    filed on the wrong shelf. Get the entry_id from list_stock_entries. Only
    the arguments given are changed; the rest of the entry is preserved.

    Args:
        entry_id: From list_stock_entries.
        best_before_date: YYYY-MM-DD.
        location_id: Either an id or an exact location name.
        amount: New amount for this entry, in the product's stock unit. Prefer
            correct_stock for "the total is wrong" — this is for when one
            specific batch is wrong.
    """
    if best_before_date is None and location_id is None and amount is None:
        raise ValueError("Nothing to change — pass a date, a location or an amount.")

    current = get(f"/stock/entry/{entry_id}")
    if not current:
        raise ValueError(f"No stock entry {entry_id!r} — call list_stock_entries.")

    # Grocy rejects a partial body here: this endpoint replaces the entry
    # rather than patching it, so every field has to be sent back even when
    # only one is changing.
    body = {
        "amount": float(current.get("amount") or 0),
        "best_before_date": current.get("best_before_date"),
        "location_id": current.get("location_id"),
        "price": current.get("price"),
        "open": current.get("open") or 0,
    }
    changed = {}
    if best_before_date is not None:
        body["best_before_date"] = _parse_date(best_before_date).isoformat()
        changed["best_before"] = body["best_before_date"]
    if location_id is not None:
        locations = locations_by_id()
        body["location_id"] = resolve_location(location_id, locations)
        changed["location"] = locations[body["location_id"]]
    if amount is not None:
        check_amount(amount)
        body["amount"] = amount
        changed["amount"] = amount

    put(f"/stock/entry/{entry_id}", body)
    return {"entry_id": entry_id, "changed": changed}


def undo_transaction(transaction_id: str) -> dict:
    """Reverse a stock transaction that should not have been recorded.

    Every write tool returns a transaction_id; stock_history shows older ones.
    Undoing is the right way to fix a mistake — booking an opposite consume or
    purchase instead leaves both entries in the history and gets the
    best-before dates wrong.

    Args:
        transaction_id: From a write tool's response, or from stock_history.
    """
    if not (transaction_id or "").strip():
        raise ValueError("No transaction_id given.")
    post(f"/stock/transactions/{transaction_id.strip()}/undo", {})
    return {
        "undone": transaction_id.strip(),
        "note": "Reversed. Call product_details or list_stock to confirm the "
                "amount is back where it should be.",
    }


TOOLS = [
    list_stock,
    expiring_soon,
    out_of_stock,
    below_min_stock,
    list_stock_entries,
    product_details,
    stock_history,
    add_stock,
    consume_product,
    open_product,
    correct_stock,
    transfer_stock,
    edit_stock_entry,
    undo_transaction,
]


def register(mcp):
    for fn in TOOLS:
        mcp.tool()(fn)
