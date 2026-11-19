"""Sync ledger: which external items a sync job has already processed.

Bookkeeping only, kept in a Grocy user entity named `sync_log` with the
user fields type, external_id, event_date and processed. User entities have
their own page under master data and never appear in stock views, so the
ledger stays out of the pantry. Create the entity and its fields in Grocy
first; these tools only read and append rows.

Grocy's API has no bulk read of a user entity's field values, so a check
reads each ledger row's fields separately. That is fine on a loopback
connection for a ledger of a few thousand rows.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..api import delete, get, post, put, query

ENTITY_NAME = "sync_log"
ENTITY = f"userentity-{ENTITY_NAME}"
# The sync runs on Eastern time; "processed" is the local calendar day.
TZ = ZoneInfo("America/New_York")


def _entity_id() -> int:
    rows = query("/objects/userentities", [f"name={ENTITY_NAME}"]) or []
    hit = next((r for r in rows if r["name"] == ENTITY_NAME), None)
    if hit is None:
        raise ValueError(
            f"Grocy has no {ENTITY_NAME!r} user entity — create it (with user "
            "fields type, external_id, event_date, processed) before using the "
            "sync log."
        )
    return int(hit["id"])


def _rows(entity_id: int) -> list[dict]:
    objects = query("/objects/userobjects", [f"userentity_id={entity_id}"]) or []
    out = []
    for obj in objects:
        if int(obj["userentity_id"]) != entity_id:
            continue
        values = get(f"/userfields/{ENTITY}/{obj['id']}") or {}
        out.append({"id": int(obj["id"]), **values})
    return out


def _text(value: str, field: str) -> str:
    q = (value or "").strip()
    if not q:
        raise ValueError(f"No {field} given.")
    return q


def sync_log_check(type: str, external_ids: list[str]) -> dict:
    """Which of these external items has a sync already processed?

    Bookkeeping for a sync job, not pantry data. Call before processing a
    batch, then process only not_logged and record each with sync_log_add.

    Args:
        type: The kind of item, e.g. "receipt" or "order". Matched exactly.
        external_ids: IDs from the source system to check.
    """
    t = _text(type, "type")
    wanted = list(dict.fromkeys(_text(i, "external_id") for i in external_ids))
    if not wanted:
        raise ValueError("No external_ids given.")
    seen = {
        r.get("external_id")
        for r in _rows(_entity_id())
        if r.get("type") == t
    }
    return {
        "type": t,
        "already_logged": [i for i in wanted if i in seen],
        "not_logged": [i for i in wanted if i not in seen],
    }


def sync_log_add(type: str, external_id: str, event_date: str) -> dict:
    """Record one external item as processed by a sync, dated today (Eastern).

    Bookkeeping for a sync job, not pantry data. Refuses an item already
    logged for the same type, so a rerun cannot double-record it.

    Args:
        type: The kind of item, e.g. "receipt" or "order".
        external_id: The item's ID in the source system.
        event_date: When the item happened, YYYY-MM-DD.
    """
    t = _text(type, "type")
    xid = _text(external_id, "external_id")
    try:
        when = date.fromisoformat((event_date or "").strip()).isoformat()
    except ValueError:
        raise ValueError(f"event_date must be YYYY-MM-DD, got {event_date!r}.")
    entity_id = _entity_id()
    dup = next(
        (r for r in _rows(entity_id)
         if r.get("type") == t and r.get("external_id") == xid),
        None,
    )
    if dup is not None:
        raise ValueError(
            f"{t} {xid!r} is already logged (row {dup['id']}, processed "
            f"{dup.get('processed')})."
        )
    processed = datetime.now(TZ).date().isoformat()
    created = (post("/objects/userobjects", {"userentity_id": entity_id}) or {})
    row_id = created.get("created_object_id")
    if not row_id:
        raise ValueError(f"Grocy did not return an id for the new row: {created!r}")
    values = {
        "type": t,
        "external_id": xid,
        "event_date": when,
        "processed": processed,
    }
    try:
        put(f"/userfields/{ENTITY}/{row_id}", values)
    except Exception:
        # Don't leave an empty row behind that a later check can't match.
        delete(f"/objects/userobjects/{row_id}")
        raise
    return {"logged": values, "row_id": int(row_id)}


TOOLS = [sync_log_check, sync_log_add]


def register(mcp):
    for fn in TOOLS:
        mcp.tool()(fn)
