import time
import pytest
from mesh_graph.db import init_db, upsert_node, get_node_attrs, get_links_for_network, get_links_for_trace, get_links_for_node


NOW = int(time.time())
PAST = NOW - 7200
FUTURE = NOW + 7200


def _insert_link(db, trace_id, from_id, to_id, link_start, link_end, snr=None, is_reply=0, is_fast_path=0, ts=None):
    ts = ts or NOW
    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)",
            (trace_id, from_id, to_id),
        )
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path),
        )


# --- schema ---

def test_init_db_idempotent(db):
    init_db(db)  # second call must not raise or duplicate tables
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"traceroute", "traceroute_link", "nodes"}.issubset(names)


# --- upsert_node / get_node_attrs ---

def test_upsert_node_insert(db):
    upsert_node(db, 0x12345678, long_name="Test Node", short_name="TN", role="ROUTER")
    attrs = get_node_attrs(db)
    assert "!12345678" in attrs
    assert attrs["!12345678"]["label"].startswith("!12345678")


def test_upsert_node_update(db):
    upsert_node(db, 0xAABBCCDD, long_name="Old", short_name="O", role="CLIENT")
    upsert_node(db, 0xAABBCCDD, long_name="New", short_name="N", role="ROUTER")
    attrs = get_node_attrs(db)
    assert "New" in attrs["!aabbccdd"]["label"]


def test_get_node_attrs_shape_router(db):
    upsert_node(db, 0x11111111, long_name="R", short_name="R", role="ROUTER")
    attrs = get_node_attrs(db)
    assert attrs["!11111111"]["shape"] == "rect"


def test_get_node_attrs_shape_client(db):
    upsert_node(db, 0x22222222, long_name="C", short_name="C", role="CLIENT")
    attrs = get_node_attrs(db)
    assert attrs["!22222222"]["shape"] == "diamond"


def test_get_node_attrs_unknown_node_from_links(db):
    _insert_link(db, 1001, 0xAAAA0001, 0xAAAA0002, 0xAAAA0001, 0xAAAA0002)
    attrs = get_node_attrs(db)
    assert "!aaaa0001" in attrs or "!aaaa0002" in attrs


# --- get_links_for_network ---

def test_get_links_for_network_all(db):
    _insert_link(db, 1, 0x01, 0x02, 0x01, 0x02, snr=5.0)
    _insert_link(db, 2, 0x03, 0x04, 0x03, 0x04, snr=3.0)
    rows = get_links_for_network(db)
    assert len(rows) == 2


def test_get_links_for_network_time_range(db):
    _insert_link(db, 1, 0x01, 0x02, 0x01, 0x02, ts=PAST)
    _insert_link(db, 2, 0x03, 0x04, 0x03, 0x04, ts=NOW)
    rows = get_links_for_network(db, start_ts=NOW - 60)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == 2


def test_get_links_for_network_end_range(db):
    _insert_link(db, 1, 0x01, 0x02, 0x01, 0x02, ts=PAST)
    _insert_link(db, 2, 0x03, 0x04, 0x03, 0x04, ts=NOW)
    rows = get_links_for_network(db, end_ts=NOW - 60)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == 1


# --- get_links_for_trace ---

def test_get_links_for_trace(db):
    _insert_link(db, 42, 0x01, 0x02, 0x01, 0x02)
    _insert_link(db, 99, 0x03, 0x04, 0x03, 0x04)
    rows = get_links_for_trace(db, trace_id=42)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == 42


def test_get_links_for_trace_includes_traceroute_endpoints(db):
    _insert_link(db, 77, 0xAA, 0xBB, 0xAA, 0xBB)
    result = get_links_for_trace(db, trace_id=77)
    assert result[0]["from_id"] == 0xAA
    assert result[0]["to_id"] == 0xBB


# --- get_links_for_node ---

def test_get_links_for_node_as_start(db):
    _insert_link(db, 1, 0x01, 0x02, 0x01, 0x02)
    _insert_link(db, 2, 0x03, 0x04, 0x03, 0x04)
    rows = get_links_for_node(db, node_id=0x01)
    assert len(rows) == 1


def test_get_links_for_node_as_end(db):
    _insert_link(db, 1, 0x01, 0x02, 0x01, 0x02)
    rows = get_links_for_node(db, node_id=0x02)
    assert len(rows) == 1


def test_get_links_for_node_time_range(db):
    _insert_link(db, 1, 0x01, 0x02, 0x01, 0x02, ts=PAST)
    _insert_link(db, 2, 0x01, 0x03, 0x01, 0x03, ts=NOW)
    rows = get_links_for_node(db, node_id=0x01, start_ts=NOW - 60)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == 2
