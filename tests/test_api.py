"""
API tests using FastAPI's TestClient (synchronous httpx).

The app is configured to use an in-memory SQLite DB injected via app.state,
populated with minimal fixture data per test.
"""

import sqlite3
import time
import pytest
from fastapi.testclient import TestClient

from mesh_graph.api.app import create_app
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
            link_start=None, link_end=None, ts=None):
    link_start = link_start if link_start is not None else from_id
    link_end = link_end if link_end is not None else to_id
    ts = ts or NOW
    with db:
        db.execute("INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)", (trace_id, from_id, to_id))
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trace_id, from_id, to_id, ts, link_start, link_end, 5.0, 0, 0),
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


def test_node_graph_time_range_accepted(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?start={_iso(PAST)}&end={_iso(PAST + 7200)}")
    assert resp.status_code == 200


def test_node_graph_invalid_node_id(client, db):
    resp = client.get("/graph/node/notanodeid")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /nodes
# ---------------------------------------------------------------------------

def test_nodes_returns_json_list(client, db):
    with db:
        db.execute("INSERT INTO nodes (nodenum, long_name, short_name, role) VALUES (?,?,?,?)",
                   (NODE_A, "Alpha", "A", "CLIENT"))
    resp = client.get("/nodes")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(n["nodenum"] == NODE_A for n in data)


def test_nodes_empty_list(client, db):
    resp = client.get("/nodes")
    assert resp.status_code == 200
    assert resp.json() == []


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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
