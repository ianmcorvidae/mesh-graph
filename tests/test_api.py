"""
API tests using FastAPI's TestClient (synchronous httpx).

The app is configured to use an in-memory SQLite DB injected via app.state,
populated with minimal fixture data per test.
"""

import sqlite3
import time
import pytest
from fastapi.testclient import TestClient

from mesh_graph.api.app import _parse_node_id, create_app
from mesh_graph.db import init_db

NOW = int(time.time())
PAST = NOW - 7200
NODE_A = 0xAAAA0001
NODE_B = 0xAAAA0002
TRACE_1 = 1001


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


@pytest.fixture
def client(db):
    app = create_app(db)
    return TestClient(app)


def _insert(db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B,
            link_start=None, link_end=None, ts=None, first_seen_ts=None, snr=5.0):
    link_start = link_start if link_start is not None else from_id
    link_end = link_end if link_end is not None else to_id
    ts = ts or NOW
    first_seen_ts = first_seen_ts if first_seen_ts is not None else ts
    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (trace_id, from_id, to_id, first_seen_ts),
        )
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trace_id, from_id, to_id, ts, link_start, link_end, snr, 0, 0),
        )


# ---------------------------------------------------------------------------
# /graph/network
# ---------------------------------------------------------------------------

def test_network_graph_returns_png_by_default(client, db):
    _insert(db)
    resp = client.get("/graph/network")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:4] == b"\x89PNG"


def test_network_graph_returns_svg(client, db):
    _insert(db)
    resp = client.get("/graph/network?format=svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]
    assert b"<svg" in resp.content


def test_network_graph_time_range(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, ts=PAST)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B, ts=NOW)
    resp = client.get(f"/graph/network?start={_iso(NOW - 60)}")
    assert resp.status_code == 200


def test_network_graph_invalid_format(client, db):
    _insert(db)
    resp = client.get("/graph/network?format=gif")
    assert resp.status_code == 400


def test_network_simple_graph_returns_svg(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, snr=2.0)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, snr=6.0)
    resp = client.get("/graph/network/simple?format=svg")
    assert resp.status_code == 200
    assert b"<svg" in resp.content
    assert b"2.0..6.0dB" in resp.content


def test_network_simple_graph_time_range_accepted(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, ts=PAST)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, ts=NOW)
    resp = client.get(f"/graph/network/simple?start={_iso(NOW - 60)}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /graph/trace/{trace_id}
# ---------------------------------------------------------------------------

def test_trace_graph_returns_png(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}")
    assert resp.status_code == 200
    assert resp.content[:4] == b"\x89PNG"


def test_trace_graph_returns_svg(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}?format=svg")
    assert resp.status_code == 200
    assert b"<svg" in resp.content


def test_trace_graph_404_for_unknown(client, db):
    resp = client.get("/graph/trace/9999")
    assert resp.status_code == 404


def test_trace_graph_defaults_to_most_recent_when_multiple_candidates(client, db):
    trace_id = 2001
    _insert(db, trace_id=trace_id, from_id=0x11111111, to_id=0x22222222, first_seen_ts=PAST, ts=PAST)
    _insert(db, trace_id=trace_id, from_id=0x33333333, to_id=0x44444444, first_seen_ts=NOW, ts=NOW)
    resp = client.get(f"/graph/trace/{trace_id}?format=svg")
    assert resp.status_code == 200
    assert b"!33333333" in resp.content
    assert b"!44444444" in resp.content
    assert b"!11111111" not in resp.content


def test_trace_graph_can_filter_by_from_and_to(client, db):
    trace_id = 2002
    _insert(db, trace_id=trace_id, from_id=0x11111111, to_id=0x22222222, first_seen_ts=PAST, ts=PAST)
    _insert(db, trace_id=trace_id, from_id=0x33333333, to_id=0x44444444, first_seen_ts=NOW, ts=NOW)
    resp = client.get(f"/graph/trace/{trace_id}?format=svg&from=!11111111&to=!22222222")
    assert resp.status_code == 200
    assert b"!11111111" in resp.content
    assert b"!22222222" in resp.content
    assert b"!33333333" not in resp.content


def test_trace_graph_can_filter_by_approximate_date(client, db):
    trace_id = 2003
    _insert(db, trace_id=trace_id, from_id=0x11111111, to_id=0x22222222, first_seen_ts=PAST, ts=PAST)
    _insert(db, trace_id=trace_id, from_id=0x33333333, to_id=0x44444444, first_seen_ts=NOW, ts=NOW)
    resp = client.get(f"/graph/trace/{trace_id}?format=svg&date={_iso(PAST + 1)}")
    assert resp.status_code == 200
    assert b"!11111111" in resp.content
    assert b"!22222222" in resp.content
    assert b"!33333333" not in resp.content


def test_trace_graph_invalid_from_node_returns_422(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}?from=not-a-node")
    assert resp.status_code == 422


def test_trace_graph_invalid_date_returns_422(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}?date=not-a-date")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /graph/node/{node_id}
# ---------------------------------------------------------------------------

def test_node_graph_returns_png(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}")
    assert resp.status_code == 200
    assert resp.content[:4] == b"\x89PNG"


def test_node_graph_returns_svg(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?format=svg")
    assert resp.status_code == 200
    assert b"<svg" in resp.content


def test_node_graph_collapses_links_with_snr_range(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B, snr=2.0)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B, snr=6.0)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?format=svg&direction=outbound")
    assert resp.status_code == 200
    assert b"2.0..6.0dB" in resp.content


def test_node_graph_time_range_accepted(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?start={_iso(PAST)}&end={_iso(PAST + 7200)}")
    assert resp.status_code == 200


def test_node_graph_invalid_time_returns_422(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?start=not-a-date")
    assert resp.status_code == 422


def test_node_graph_invalid_node_id(client, db):
    resp = client.get("/graph/node/notanodeid")
    assert resp.status_code == 422


def test_node_graph_accepts_direction_and_depth(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B)
    _insert(db, trace_id=1, from_id=NODE_B, to_id=0xAAAA0003, link_start=NODE_B, link_end=0xAAAA0003)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?format=svg&direction=outbound&depth=2")
    assert resp.status_code == 200
    assert b"!aaaa0003" in resp.content


def test_node_graph_both_is_default_and_splits_overlap_nodes(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B)
    _insert(db, trace_id=2, from_id=NODE_B, to_id=NODE_A, link_start=NODE_B, link_end=NODE_A)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?format=svg")
    assert resp.status_code == 200
    assert b"[out]" in resp.content
    assert b"[in]" in resp.content
    assert f"!{NODE_A:08x}".encode() in resp.content
    assert f"!{NODE_A:08x} [out]".encode() not in resp.content
    assert f"!{NODE_A:08x} [in]".encode() not in resp.content


def test_node_graph_network_direction_is_supported(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B)
    _insert(db, trace_id=2, from_id=NODE_B, to_id=NODE_A, link_start=NODE_B, link_end=NODE_A)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?format=svg&direction=network")
    assert resp.status_code == 200
    assert b"[out]" not in resp.content
    assert b"[in]" not in resp.content


def test_node_graph_invalid_direction_returns_422(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?direction=sideways")
    assert resp.status_code == 422


def test_node_graph_invalid_depth_returns_422(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?depth=0")
    assert resp.status_code == 422


def test_node_id_plain_decimal_parses_as_decimal():
    assert _parse_node_id("10") == 10


def test_node_id_plain_hex_still_supported():
    assert _parse_node_id("a") == 10


# ---------------------------------------------------------------------------
# /nodes
# ---------------------------------------------------------------------------

def test_nodes_returns_json_list(client, db):
    with db:
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_A, "Alpha", "A", "CLIENT", NOW),
        )
    resp = client.get("/nodes")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(n["nodenum"] == NODE_A for n in data)


def test_nodes_empty_list(client, db):
    resp = client.get("/nodes")
    assert resp.status_code == 200
    assert resp.json() == []


def test_nodes_are_ordered_by_last_seen_desc(client, db):
    with db:
        db.execute("INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)", (0x1, "Old", "O", "CLIENT", PAST))
        db.execute("INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)", (0x2, "New", "N", "CLIENT", NOW))
    resp = client.get("/nodes?after=%d&limit=10" % NOW)
    assert resp.status_code == 200
    data = resp.json()
    assert [n["nodenum"] for n in data] == [0x2, 0x1]


def test_nodes_support_after_cursor_and_limit(client, db):
    with db:
        db.execute("INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)", (0x10, "A", "A", "CLIENT", NOW))
        db.execute("INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)", (0x20, "B", "B", "CLIENT", NOW - 10))
        db.execute("INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)", (0x30, "C", "C", "CLIENT", NOW - 20))
    resp = client.get("/nodes?after=%d&limit=2" % (NOW - 10))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert [n["nodenum"] for n in data] == [0x20, 0x30]


# ---------------------------------------------------------------------------
# /traceroutes
# ---------------------------------------------------------------------------

def test_traceroutes_returns_json_list(client, db):
    _insert(db)
    resp = client.get("/traceroutes")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(t["trace_id"] == TRACE_1 for t in data)


def test_traceroutes_empty_list(client, db):
    resp = client.get("/traceroutes")
    assert resp.status_code == 200
    assert resp.json() == []


def test_traceroutes_are_ordered_by_first_seen_desc(client, db):
    _insert(db, trace_id=3001, from_id=0x01, to_id=0x02, first_seen_ts=PAST, ts=PAST)
    _insert(db, trace_id=3002, from_id=0x03, to_id=0x04, first_seen_ts=NOW, ts=NOW)
    resp = client.get("/traceroutes?after=%d&limit=10" % NOW)
    assert resp.status_code == 200
    data = resp.json()
    assert [t["trace_id"] for t in data] == [3002, 3001]


def test_traceroutes_support_after_cursor_and_limit(client, db):
    _insert(db, trace_id=4001, from_id=0x01, to_id=0x02, first_seen_ts=NOW, ts=NOW)
    _insert(db, trace_id=4002, from_id=0x03, to_id=0x04, first_seen_ts=NOW - 10, ts=NOW - 10)
    _insert(db, trace_id=4003, from_id=0x05, to_id=0x06, first_seen_ts=NOW - 20, ts=NOW - 20)
    resp = client.get("/traceroutes?after=%d&limit=2" % (NOW - 10))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert [t["trace_id"] for t in data] == [4002, 4003]


def test_traceroutes_can_filter_by_from_and_to_nodes(client, db):
    _insert(db, trace_id=5001, from_id=0xAAAA0001, to_id=0xBBBB0001, first_seen_ts=NOW, ts=NOW)
    _insert(db, trace_id=5002, from_id=0xAAAA0001, to_id=0xBBBB0002, first_seen_ts=NOW - 5, ts=NOW - 5)
    _insert(db, trace_id=5003, from_id=0xCCCC0001, to_id=0xBBBB0001, first_seen_ts=NOW - 10, ts=NOW - 10)
    resp = client.get("/traceroutes?from=!aaaa0001&to=!bbbb0001")
    assert resp.status_code == 200
    data = resp.json()
    assert [t["trace_id"] for t in data] == [5001]


def test_traceroutes_invalid_from_node_returns_422(client, db):
    _insert(db)
    resp = client.get("/traceroutes?from=not-a-node")
    assert resp.status_code == 422


def test_network_graph_invalid_time_returns_422(client, db):
    _insert(db)
    resp = client.get("/graph/network?start=not-a-date")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
