from grocy_mcp import api, notify


class Calls:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


def test_writes_announce_only_when_configured(monkeypatch):
    fake = Calls()
    monkeypatch.setattr(api, "client", lambda: type("C", (), {"call": lambda self, *a, **k: {}})())
    monkeypatch.setattr(notify, "_announcer", fake)
    monkeypatch.delenv("GROCY_MCP_NATS_URL", raising=False)
    api.post("/stock/products/1/consume", {"amount": 1})
    assert fake.pokes == 0                      # off unless configured
    monkeypatch.setenv("GROCY_MCP_NATS_URL", "nats://127.0.0.1:4222")
    api.post("/stock/products/1/consume", {"amount": 1})
    api.put("/stock/entry/3", {"amount": 2})
    api.delete("/objects/product_barcodes/9")
    api.post("/stock/shoppinglist/add-product", {"product_id": 1})   # not the pantry
    assert fake.pokes == 3
