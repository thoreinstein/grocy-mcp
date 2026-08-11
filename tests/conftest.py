"""A fake Grocy, so the tests need no live instance and no network.

`fake_grocy` installs a stub in place of the HTTP client and records every
write, which is what lets the tests assert on the *body* a tool sent — the
part that is easy to get subtly wrong and impossible to see from a return
value.
"""

from __future__ import annotations

import copy

import pytest

from grocy_mcp import api, config


class FakeGrocy:
    """Stands in for GrocyClient with a small in-memory instance."""

    def __init__(self):
        self.locations = [
            {"id": 2, "name": "Fridge"},
            {"id": 3, "name": "Pantry"},
            {"id": 4, "name": "Freezer"},
        ]
        self.groups = [
            {"id": 1, "name": "Snacks"},
            {"id": 2, "name": "Spices & Herbs"},
        ]
        self.units = [
            {"id": 2, "name": "Piece"},
            {"id": 3, "name": "Pack"},
            {"id": 4, "name": "Gram"},
        ]
        self.products = [
            self._product(1, "Long-grain Rice", 3, 1, 3, "White rice, 1 kg"),
            self._product(2, "Turmeric Powder", 4, 2, 3, "Ground turmeric, 100 g"),
            self._product(3, "Rice Noodles", 3, 1, 3, "Dried flat noodles"),
            self._product(4, "Butter", 3, 1, 2, "Dairy butter"),
        ]
        self.barcodes = [{"id": 1, "product_id": 4, "barcode": "40349558"}]
        self.stock = [
            self._stock(1, 2, "2027-01-31"),
            self._stock(2, 100, "2028-01-31"),
            self._stock(4, 1, "2026-08-21"),
        ]
        self.shopping_lists = [{"id": 1, "name": "Shopping list"}]
        self.shopping_list = []
        self.writes = []          # (method, path, body)
        self.responses = {}       # path -> canned response

    # --- fixture data ---------------------------------------------------

    @staticmethod
    def _product(pid, name, qu, group, location, description=""):
        return {
            "id": pid,
            "name": name,
            "description": description,
            "product_group_id": group,
            "location_id": location,
            "qu_id_stock": qu,
            "qu_id_purchase": qu,
            "qu_id_consume": qu,
            "qu_id_price": qu,
            "min_stock_amount": 0,
        }

    def _stock(self, pid, amount, bbd):
        product = next(p for p in self.products if p["id"] == pid)
        return {
            "product_id": pid,
            "amount": amount,
            "amount_aggregated": amount,
            "amount_opened_aggregated": 0,
            "best_before_date": bbd,
            "product": product,
        }

    def _entry(self, pid):
        """One stock entry per product, with id = 100 + product id."""
        product = next(p for p in self.products if p["id"] == pid)
        return {
            "id": 100 + pid,
            "product_id": pid,
            "amount": self.amount_of(pid),
            "best_before_date": "2027-01-31",
            "location_id": product["location_id"],
            "price": None,
            "open": 0,
            "purchased_date": "2026-08-01",
        }

    def amount_of(self, pid):
        for row in self.stock:
            if row["product_id"] == pid:
                return float(row["amount_aggregated"])
        return 0.0

    # --- client interface -----------------------------------------------

    def call(self, path, body=None, *, tries=1, method=None):
        verb = method or ("POST" if body is not None else "GET")
        if verb != "GET":
            self.writes.append((verb, path, body))
        base = path.split("?", 1)[0]
        if base in self.responses:
            return copy.deepcopy(self.responses[base])
        handler = getattr(self, f"_{verb.lower()}", None)
        result = handler(base, body) if handler else None
        # Deep-copied on the way out, because a real API hands back freshly
        # parsed JSON every time. Returning the internal lists by reference
        # would let a later write mutate data a caller is still holding — an
        # aliasing bug the code under test cannot have but the fake would.
        return copy.deepcopy(result)

    def _get(self, path, _body):
        simple = {
            "/objects/locations": self.locations,
            "/objects/product_groups": self.groups,
            "/objects/quantity_units": self.units,
            "/objects/products": self.products,
            "/objects/product_barcodes": self.barcodes,
            "/objects/shopping_lists": self.shopping_lists,
            "/objects/shopping_list": self.shopping_list,
            "/stock": self.stock,
        }
        if path in simple:
            return simple[path]
        if path.startswith("/objects/products/"):
            pid = int(path.rsplit("/", 1)[1])
            return next((p for p in self.products if p["id"] == pid), None)
        if path.startswith("/stock/products/") and path.count("/") == 3:
            pid = int(path.rsplit("/", 1)[1])
            product = next((p for p in self.products if p["id"] == pid), None)
            return {
                "product": product,
                "stock_amount": self.amount_of(pid),
                "stock_amount_opened": 0,
                "product_barcodes": [
                    b for b in self.barcodes if b["product_id"] == pid
                ],
            }
        if path.endswith("/locations"):
            pid = int(path.split("/")[3])
            product = next(p for p in self.products if p["id"] == pid)
            return [{
                "id": 1,
                "product_id": pid,
                "amount": self.amount_of(pid),
                "location_id": product["location_id"],
            }]
        if path.endswith("/entries"):
            return [self._entry(int(path.split("/")[3]))]
        if path.startswith("/stock/entry/"):
            return self._entry(int(path.rsplit("/", 1)[1]) - 100)
        if path == "/stock/volatile":
            return {"missing_products": [], "due_products": [], "expired_products": []}
        return None

    def _post(self, path, body):
        if path.endswith("/consume"):
            pid = int(path.split("/")[3])
            self._adjust(pid, -float(body["amount"]))
        elif path.endswith("/add"):
            pid = int(path.split("/")[3])
            self._adjust(pid, float(body["amount"]))
        elif path == "/objects/products":
            new_id = max(p["id"] for p in self.products) + 1
            self.products.append({**self._product(
                new_id, body["name"], body["qu_id_stock"],
                body["product_group_id"], body["location_id"],
                body.get("description", ""),
            ), "min_stock_amount": body.get("min_stock_amount", 0)})
            return {"created_object_id": new_id}
        elif path == "/stock/shoppinglist/add-product":
            self.shopping_list.append({
                "id": len(self.shopping_list) + 1,
                "shopping_list_id": body["list_id"],
                "product_id": body["product_id"],
                "amount": body["product_amount"],
                "note": body.get("note"),
                "done": 0,
            })
        elif path == "/objects/shopping_list":
            self.shopping_list.append({
                "id": len(self.shopping_list) + 1,
                "shopping_list_id": body["shopping_list_id"],
                "product_id": None,
                "amount": body["amount"],
                "note": body["note"],
                "done": 0,
            })
        return [{"transaction_id": "txn-test", "id": 1}]

    def _put(self, path, body):
        if path.startswith("/objects/products/"):
            pid = int(path.rsplit("/", 1)[1])
            product = next(p for p in self.products if p["id"] == pid)
            product.update(body)
        elif path.startswith("/objects/shopping_list/"):
            rid = int(path.rsplit("/", 1)[1])
            row = next(r for r in self.shopping_list if r["id"] == rid)
            row.update(body)
        return None

    def _delete(self, path, _body):
        if path.startswith("/objects/products/"):
            pid = int(path.rsplit("/", 1)[1])
            self.products = [p for p in self.products if p["id"] != pid]
        elif path.startswith("/objects/shopping_list/"):
            rid = int(path.rsplit("/", 1)[1])
            self.shopping_list = [r for r in self.shopping_list if r["id"] != rid]
        elif path.startswith("/objects/product_barcodes/"):
            bid = int(path.rsplit("/", 1)[1])
            self.barcodes = [b for b in self.barcodes if b["id"] != bid]
        return None

    def _adjust(self, pid, delta):
        for row in self.stock:
            if row["product_id"] == pid:
                row["amount"] += delta
                row["amount_aggregated"] += delta
                if row["amount_aggregated"] <= 0:
                    self.stock.remove(row)
                return
        if delta > 0:
            self.stock.append(self._stock(pid, delta, "2027-12-31"))

    def body_of(self, verb, path):
        """The body of the one matching write, asserting there was exactly one."""
        hits = [b for (v, p, b) in self.writes if v == verb and p == path]
        assert len(hits) == 1, f"expected one {verb} {path}, got {len(hits)}"
        return hits[0]


@pytest.fixture
def fake_grocy(monkeypatch):
    fake = FakeGrocy()
    monkeypatch.setattr(api, "_client", fake)
    monkeypatch.setattr(config, "_config", config.PantryConfig())
    yield fake
    monkeypatch.setattr(api, "_client", None)
    monkeypatch.setattr(config, "_config", None)
