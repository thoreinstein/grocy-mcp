"""Sync ledger: what gets written, and what is refused."""


import pytest

from grocy_mcp.tools import sync_log


def test_add_writes_a_row_with_all_four_fields(fake_grocy, monkeypatch):
    class FixedNow:
        @staticmethod
        def now(tz):
            assert str(tz) == "America/New_York"
            from datetime import datetime
            return datetime(2026, 11, 19, 23, 30, tzinfo=tz)

    monkeypatch.setattr(sync_log, "datetime", FixedNow)
    out = sync_log.sync_log_add("receipt", " R-1 ", "2026-11-18")
    assert fake_grocy.body_of("POST", "/objects/userobjects") == {"userentity_id": 1}
    assert fake_grocy.body_of("PUT", "/userfields/userentity-sync_log/1") == {
        "type": "receipt",
        "external_id": "R-1",
        "event_date": "2026-11-18",
        "processed": "2026-11-19",
    }
    assert out["row_id"] == 1


def test_add_refuses_a_duplicate_for_the_same_type(fake_grocy):
    sync_log.sync_log_add("receipt", "R-1", "2026-11-18")
    with pytest.raises(ValueError, match="already logged"):
        sync_log.sync_log_add("receipt", "R-1", "2026-11-19")
    assert len(fake_grocy.userobjects) == 1


def test_same_id_under_another_type_is_not_a_duplicate(fake_grocy):
    sync_log.sync_log_add("receipt", "X-9", "2026-11-18")
    sync_log.sync_log_add("order", "X-9", "2026-11-18")
    assert len(fake_grocy.userobjects) == 2


def test_add_rejects_a_bad_date(fake_grocy):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        sync_log.sync_log_add("receipt", "R-1", "11/18/2026")
    assert fake_grocy.writes == []


def test_add_removes_the_row_if_the_fields_fail(fake_grocy, monkeypatch):
    def boom(path, body):
        raise RuntimeError("PUT failed")

    monkeypatch.setattr(sync_log, "put", boom)
    with pytest.raises(RuntimeError):
        sync_log.sync_log_add("receipt", "R-1", "2026-11-18")
    assert fake_grocy.userobjects == []


def test_check_splits_logged_from_new_and_keeps_order(fake_grocy):
    sync_log.sync_log_add("receipt", "R-2", "2026-11-18")
    sync_log.sync_log_add("order", "R-3", "2026-11-18")
    out = sync_log.sync_log_check("receipt", ["R-3", "R-2", "R-4", "R-2"])
    assert out == {
        "type": "receipt",
        "already_logged": ["R-2"],
        "not_logged": ["R-3", "R-4"],
    }


def test_missing_entity_is_explained(fake_grocy):
    fake_grocy.userentities = []
    with pytest.raises(ValueError, match="no 'sync_log' user entity"):
        sync_log.sync_log_check("receipt", ["R-1"])


def test_check_needs_ids(fake_grocy):
    with pytest.raises(ValueError, match="No external_ids"):
        sync_log.sync_log_check("receipt", [])
