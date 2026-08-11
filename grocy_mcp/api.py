"""HTTP access to the Grocy REST API.

Configuration is read from the environment on first use rather than at import,
so the package can be imported (and tested) without a live Grocy to point at.

    GROCY_API_URL   default https://localhost/api
    GROCY_API_KEY   required; created in the Grocy UI under Manage API keys
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://localhost/api"
TIMEOUT = 30


class GrocyError(RuntimeError):
    """A request Grocy refused, carrying the reason Grocy gave.

    urllib's HTTPError stringifies to "HTTP Error 400: Bad Request" and puts
    the useful part — Grocy's own error_message — in the response body, which
    is discarded unless it is read before the exception propagates. MCP tools
    show exception text to the model, so without this every validation failure
    arrives as an unactionable "Bad Request".
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class GrocyClient:
    """A thin, dependency-free client for one Grocy instance."""

    def __init__(self, url: str, key: str, *, timeout: int = TIMEOUT):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def call(self, path, body=None, *, tries=1, method=None):
        last = None
        for attempt in range(tries):
            try:
                return self._once(path, body, method)
            except urllib.error.HTTPError as exc:
                last = exc
                if attempt == tries - 1:
                    self._raise(exc)
                time.sleep(15)
            except Exception as exc:
                last = exc
                if attempt == tries - 1:
                    raise
                time.sleep(15)
        raise last  # pragma: no cover - unreachable while tries >= 1

    def _once(self, path, body, method):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                "GROCY-API-KEY": self.key,
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw.strip() else None

    @staticmethod
    def _raise(exc):
        detail = ""
        try:
            payload = json.loads(exc.read() or b"{}")
            detail = payload.get("error_message") or payload.get("message") or ""
        except Exception:
            pass
        raise GrocyError(exc.code, f"Grocy refused this ({exc.code}): {detail or exc.reason}")


_client: GrocyClient | None = None


def configure(url: str | None = None, key: str | None = None) -> GrocyClient:
    """Set the client explicitly. Otherwise it is built from the environment."""
    global _client
    _client = GrocyClient(
        url or os.environ.get("GROCY_API_URL", DEFAULT_URL),
        key if key is not None else os.environ.get("GROCY_API_KEY", ""),
    )
    return _client


def client() -> GrocyClient:
    global _client
    if _client is None:
        key = os.environ.get("GROCY_API_KEY")
        if not key:
            raise RuntimeError(
                "GROCY_API_KEY is not set. Create an API key in the Grocy web "
                "UI (wrench icon -> Manage API keys) and export it, along with "
                "GROCY_API_URL if Grocy is not at " + DEFAULT_URL + "."
            )
        _client = GrocyClient(os.environ.get("GROCY_API_URL", DEFAULT_URL), key)
    return _client


def get(path, tries=3):
    # Retried: a read can land while Grocy's PHP-FPM pool is restarting and get
    # an error page instead of JSON.
    return client().call(path, tries=tries)


def post(path, body):
    # Deliberately no retry: stock transactions are not idempotent, and a retry
    # after a timeout that actually succeeded would double-book.
    return client().call(path, body=body, tries=1)


def put(path, body):
    # Same reasoning as post. A PUT to /objects/<entity>/<id> is idempotent in
    # principle, but /stock/entry/<id> rewrites an amount, so a blind retry
    # after a timeout that may have succeeded is not safe in general.
    return client().call(path, body=body, tries=1, method="PUT")


def delete(path):
    # Same reasoning as post.
    return client().call(path, tries=1, method="DELETE")


def query(path, filters=None, order=None, limit=None):
    """GET an /objects endpoint using Grocy's query syntax.

    `filters` are Grocy's `query[]` expressions, e.g.
    "row_created_timestamp>2026-08-01" or "product_id=12".
    """
    params = [("query[]", f) for f in (filters or [])]
    if order:
        params.append(("order", order))
    if limit:
        params.append(("limit", str(limit)))
    if not params:
        return get(path)
    return get(f"{path}?{urllib.parse.urlencode(params)}")
