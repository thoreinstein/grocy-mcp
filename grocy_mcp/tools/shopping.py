"""Shopping list tools — the middle of the pantry loop.

out_of_stock and below_min_stock say what to buy; add_stock records what came
back. Without these, the trip in between has to happen in the web UI, so a
conversation that notices the rice has run out cannot do anything about it.

A list row is one of two things: a row linked to a catalog product (which is
what makes "we bought this" resolvable back to a product), or a free-text note
for something the pantry does not track as a product at all, like kitchen roll.
Both are handled here; the response says which kind was written.
"""

from __future__ import annotations

from ..api import delete, get, post, put
from ..lookup import full_catalog, possible_duplicates, resolve
from ..stock import fmt_amount


def _list_id():
    """The shopping list to work on.

    Grocy supports several. Rather than putting a list argument on every tool
    for a feature most instances never use, take the first one Grocy reports
    and fall back to the seeded id.
    """
    lists = get("/objects/shopping_lists") or []
    return lists[0]["id"] if lists else 1


def _rows(list_id=None):
    """Shopping list rows, joined to product names and units."""
    list_id = list_id or _list_id()
    products = {p["id"]: p for p in get("/objects/products")}
    units = {u["id"]: u["name"] for u in get("/objects/quantity_units")}

    out = []
    for row in get("/objects/shopping_list") or []:
        if int(row.get("shopping_list_id") or 1) != int(list_id):
            continue
        pid = row.get("product_id")
        product = products.get(int(pid)) if pid else None
        unit = units.get(row.get("qu_id")) or (
            units.get(product["qu_id_stock"], "") if product else ""
        )
        amount = float(row.get("amount") or 0)
        out.append({
            "row_id": row["id"],
            "item": product["name"] if product else (row.get("note") or "").strip(),
            "amount": fmt_amount(amount, unit) if unit else f"{amount:g}",
            "done": str(row.get("done") or "0") not in ("0", ""),
            "product_id": product["id"] if product else None,
            "note": (row.get("note") or "").strip() or None,
        })
    return out


def _find_row(item, rows):
    """Find the one list row `item` refers to, by product or by note text."""
    q = (item or "").strip()
    if not q:
        raise ValueError("No item given.")

    hits = [r for r in rows if r["item"].casefold() == q.casefold()]
    if not hits:
        hits = [r for r in rows if q.casefold() in r["item"].casefold()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = ", ".join(sorted(r["item"] for r in hits))
        raise ValueError(
            f"{len(hits)} list entries match {q!r}: {names}. Call again with "
            "the exact one."
        )
    on_list = ", ".join(r["item"] for r in rows) or "(the list is empty)"
    raise ValueError(f"{q!r} is not on the shopping list. On it: {on_list}.")


def list_shopping_list() -> dict:
    """Show the shopping list: what to buy, how much, and what's checked off.

    Rows either point at a catalog product or are free-text notes for things
    the pantry doesn't track. Checked-off rows stay on the list until
    clear_shopping_list removes them.
    """
    rows = _rows()
    rows.sort(key=lambda r: (r["done"], r["item"].casefold()))
    return {
        "items": len(rows),
        "still_to_buy": sum(1 for r in rows if not r["done"]),
        "list": [
            {k: v for k, v in r.items() if k not in ("product_id", "note") or v}
            for r in rows
        ],
    }


def add_to_shopping_list(item: str, amount: float = 1, note: str | None = None) -> dict:
    """Add something to the shopping list.

    If `item` names a catalog product the row is linked to that product, which
    is what lets a later add_stock match it up. If it names nothing the pantry
    tracks, the row is added as a free-text note instead — right for kitchen
    roll, wrong for a misspelled product name, so this refuses when the text
    looks close to an existing product rather than quietly writing a note.

    Args:
        item: Product name, description fragment, barcode, or free text.
        amount: How much to buy, in the product's stock unit.
        note: Optional extra text on the row, e.g. "the big tin".
    """
    if amount is None or amount <= 0:
        raise ValueError(f"amount must be greater than 0, got {amount!r}.")

    q = (item or "").strip()
    if not q:
        raise ValueError("No item given.")

    try:
        product = resolve(q)
    except ValueError as exc:
        if not str(exc).startswith("No product matches"):
            raise
        product = None

    if product is not None:
        body = {
            "product_id": product["id"],
            "list_id": _list_id(),
            "product_amount": amount,
        }
        if note and note.strip():
            body["note"] = note.strip()
        post("/stock/shoppinglist/add-product", body)
        return {
            "added": product["name"],
            "amount": fmt_amount(amount, product["unit"]),
            "kind": "product",
        }

    similar = possible_duplicates(q, [i["name"] for i in full_catalog()])
    if similar:
        raise ValueError(
            f"{q!r} isn't a product, but it is close to: {', '.join(similar)}. "
            "Call again with the exact product name, or with clearly different "
            "text if it really is a note item."
        )

    text = q if not (note and note.strip()) else f"{q} ({note.strip()})"
    post("/objects/shopping_list", {
        "shopping_list_id": _list_id(),
        "note": text,
        "amount": amount,
    })
    return {"added": text, "amount": f"{amount:g}", "kind": "note"}


def remove_from_shopping_list(item: str) -> dict:
    """Take a row off the shopping list entirely.

    For something added by mistake or no longer needed. If it was bought,
    prefer check_off_shopping_item (and add_stock), which leaves a record that
    the trip covered it.

    Args:
        item: Product name or note text, as shown by list_shopping_list.
    """
    row = _find_row(item, _rows())
    # Deleted by row rather than through /stock/shoppinglist/remove-product,
    # which only understands product-linked rows and would leave a note row
    # stuck on the list with no way to shift it.
    delete(f"/objects/shopping_list/{row['row_id']}")
    return {"removed": row["item"]}


def check_off_shopping_item(item: str, done: bool = True) -> dict:
    """Mark a shopping list row as bought (or un-mark it).

    Checking off does not add anything to stock — call add_stock for that, with
    the real best-before date from the package.

    Args:
        item: Product name or note text, as shown by list_shopping_list.
        done: False to un-check a row checked off by mistake.
    """
    row = _find_row(item, _rows())
    put(f"/objects/shopping_list/{row['row_id']}", {"done": 1 if done else 0})
    return {"item": row["item"], "done": bool(done)}


def add_missing_to_shopping_list() -> dict:
    """Add everything below its minimum stock level to the shopping list.

    Grocy's own "add missing products". It only sees products that have a
    minimum set (update_product's min_stock), so in an instance where few do,
    read out_of_stock and add what's wanted by name instead — that needs no
    minimums.
    """
    before = {r["row_id"] for r in _rows()}
    post("/stock/shoppinglist/add-missing-products", {"list_id": _list_id()})
    after = _rows()
    added = [r["item"] for r in after if r["row_id"] not in before]
    return {
        "added": len(added),
        "items": sorted(added),
        "note": None if added else
                "Nothing was added — no product is below a minimum. Products "
                "without a minimum set are invisible to this.",
    }


def clear_shopping_list(done_only: bool = True) -> dict:
    """Clear the shopping list after a shop.

    Args:
        done_only: True (the default) removes only the rows checked off,
            leaving anything still to buy. Pass False to wipe the list
            completely — that discards unbought items too.
    """
    rows = _rows()
    if done_only:
        # Done-only clearing goes row by row rather than through
        # /stock/shoppinglist/clear, so the response can name what was removed.
        # It also means a Grocy version that ignored the done_only flag could
        # not quietly wipe the unbought half of the list, which is the whole
        # point of this argument.
        targets = [r for r in rows if r["done"]]
        for row in targets:
            delete(f"/objects/shopping_list/{row['row_id']}")
        return {
            "removed": len(targets),
            "items": sorted(r["item"] for r in targets),
            "still_on_list": len(rows) - len(targets),
        }

    post("/stock/shoppinglist/clear", {"list_id": _list_id(), "done_only": False})
    return {
        "removed": len(rows),
        "items": sorted(r["item"] for r in rows),
        "still_on_list": 0,
    }


TOOLS = [
    list_shopping_list,
    add_to_shopping_list,
    remove_from_shopping_list,
    check_off_shopping_item,
    add_missing_to_shopping_list,
    clear_shopping_list,
]


def register(mcp):
    for fn in TOOLS:
        mcp.tool()(fn)
