# grocy-mcp

An [MCP](https://modelcontextprotocol.io) server for [Grocy](https://grocy.info),
so an AI assistant can read and manage your pantry: what's in stock, what's
going off, what needs buying, and what you just used.

27 tools covering stock, the product catalog and the shopping list. Every call
reads live from Grocy — there is no cache to go stale.

```
You: what's going off this week, and can I make something with it?
     ...
You: right, I used the last of the coconut milk and two of the tomatoes
     ...
You: add coconut milk to the shopping list
```

## Install

```bash
pip install grocy-mcp            # stdio only
pip install 'grocy-mcp[http]'    # also serve over HTTP with OAuth
```

You need a Grocy API key: in Grocy, wrench icon → **Manage API keys** → add.

```bash
export GROCY_API_URL=https://grocy.example.com/api
export GROCY_API_KEY=...
```

## Use it from a local client

Most MCP clients launch the server themselves over stdio. For Claude Desktop,
add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "grocy": {
      "command": "grocy-mcp",
      "env": {
        "GROCY_API_URL": "https://grocy.example.com/api",
        "GROCY_API_KEY": "your-api-key",
        "GROCY_MCP_CONFIG": "/path/to/pantry.toml"
      }
    }
  }
}
```

`GROCY_MCP_CONFIG` is optional — see [Configuration](#configuration).

## Tools

**Stock**

| Tool | |
|---|---|
| `list_stock` | Everything on the shelf, filterable by location or category |
| `expiring_soon` | Expired or due within N days, worst first |
| `out_of_stock` | Products at zero. Needs no minimum levels |
| `below_min_stock` | Products under a configured minimum |
| `list_stock_entries` | The individual batches making up a total, with their own dates |
| `product_details` | Last bought, last used, average shelf life, spoil rate, minimum |
| `stock_history` | The journal: what was bought, used, opened or corrected |
| `add_stock` | Record a purchase |
| `consume_product` | Record use, or something thrown away |
| `open_product` | Mark a pack opened without consuming it |
| `correct_stock` | Set the amount to what you actually counted, either direction |
| `transfer_stock` | Move stock between locations |
| `edit_stock_entry` | Fix one batch's date, shelf or amount |
| `undo_transaction` | Reverse a stock transaction |

**Catalog** — `search_products`, `get_conventions`, `create_product`,
`update_product`, `delete_product`, `add_barcode`, `remove_barcode`

**Shopping list** — `list_shopping_list`, `add_to_shopping_list`,
`remove_from_shopping_list`, `check_off_shopping_item`,
`add_missing_to_shopping_list`, `clear_shopping_list`

### It refuses rather than guessing

Most of the value over raw API calls is in what these tools *won't* do. Grocy
will happily take stock negative or move a batch out of a shelf it isn't on;
an agent that does so is very hard to notice afterwards.

- An ambiguous product name raises with the candidates listed, instead of
  picking one. A wrong guess silently moves the wrong product's stock.
- Consuming, opening or transferring more than is on hand is refused.
- A transfer with stock split across shelves refuses until you say which shelf.
- Changing a product's unit while it holds stock is refused — Grocy would
  reinterpret the existing amount in the new unit.
- A barcode already belonging to another product is refused.
- Adding a *misspelled* product to the shopping list is refused rather than
  quietly written as a free-text note that can never be matched back to stock.
- Deleting a product that still has stock is refused.

Every stock write returns a `transaction_id`, so mistakes get undone properly
rather than cancelled out with an opposite booking that leaves both rows in the
journal and invents a best-before date.

## Configuration

Optional, and only for things Grocy has no field for. Locations, categories and
units are always read live from your instance, so they are never configured
here and cannot drift.

What you can configure is the *advice*: what each location is for, how long
things keep when the package has no date, how you name products. That is what
makes an agent's guesses good, and it is different in every kitchen.

```toml
[pantry]
summary = "Household inventory for a two-person kitchen."
soon_days = 7

expiry_guidance = """
Best-before estimates when the package date is unknown:
  fresh veg ~1 week    frozen ~2 months    whole spices ~3 years
"""

[pantry.location_notes]
"Fridge" = "Perishables: dairy, eggs, opened jars"
"Freezer" = "Frozen items, meat, fish"
```

See [`examples/pantry.toml`](examples/pantry.toml) for every option. Point at
it with `GROCY_MCP_CONFIG=/path/to/pantry.toml` or `--config`.

With no config file the server still works — it just describes your instance
without opinions about it.

## Serving over HTTP

For a remote client (e.g. a Claude custom connector) rather than a local one.
Bind to loopback and put a reverse proxy in front to terminate TLS.

```bash
grocy-mcp --transport http --public-host grocy-mcp.example.com
```

Authentication is **required by default** over HTTP, because a write-capable
server without it is the kind of default nobody notices until it is reachable
from somewhere it shouldn't be. Tokens are validated locally against the
issuer's published keys — no introspection call, so a public PKCE client needs
no secret here.

```bash
export GROCY_MCP_OIDC_ISSUER=https://auth.example.com/realms/home
export GROCY_MCP_OIDC_AUDIENCE=grocy-mcp     # usually the client id
export GROCY_MCP_OIDC_SCOPES=mcp             # optional, space separated
```

The JWKS endpoint is discovered from the issuer. Set
`GROCY_MCP_OIDC_JWKS_URI` if your provider doesn't publish standard discovery
metadata — [Kanidm](https://kanidm.com), for instance, serves per-client keys
at `<issuer>/public_key.jwk`, which this tries as a fallback.

`--no-auth` exists for a server on an interface nothing untrusted can reach.
Be sure that's true before using it.

<details>
<summary>nginx</summary>

The SSE response must not be buffered, and the timeouts need raising:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}

# RFC 9728 protected-resource metadata, served at the path-suffixed location.
location /.well-known/oauth-protected-resource/mcp {
    proxy_pass http://127.0.0.1:8765;
}
```

Two things are easy to get wrong here. The metadata lives at the
*path-suffixed* location (`…/oauth-protected-resource/mcp`), not the bare one.
And `--public-url` must equal the URL exactly as the client has it configured,
path included, or the metadata is rejected as not describing this server.
</details>

## Requirements

Python 3.11+, and Grocy 4.x. Developed against 4.6; every endpoint used is
checked against the instance's own OpenAPI spec.

## Development

```bash
pip install -e '.[http,dev]'
pytest
```

The tests run against an in-memory fake Grocy, so they need no instance and no
network. They assert on the request bodies the tools send, not just their
return values.

## License

MIT
