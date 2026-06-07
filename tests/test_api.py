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


def _insert(
    db,
    trace_id=TRACE_1,
    from_id=NODE_A,
    to_id=NODE_B,
    link_start=None,
    link_end=None,
    ts=None,
    first_seen_ts=None,
    snr=5.0,
    is_reply=0,
):
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
            (trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, 0),
        )


# ---------------------------------------------------------------------------
# /graph/network
# ---------------------------------------------------------------------------


def test_network_graph_returns_svg_by_default(client, db):
    _insert(db)
    resp = client.get("/graph/network")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in resp.content


def test_network_graph_returns_svg(client, db):
    _insert(db)
    resp = client.get("/graph/network?format=svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]
    assert b"<svg" in resp.content


def test_network_graph_time_range(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, ts=PAST)
    _insert(
        db, trace_id=2, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B, ts=NOW
    )
    resp = client.get(f"/graph/network?start={_iso(NOW - 60)}")
    assert resp.status_code == 200


def test_network_graph_invalid_format(client, db):
    _insert(db)
    resp = client.get("/graph/network?format=gif")
    assert resp.status_code == 400


def test_network_graph_unknown_query_param_returns_400(client, db):
    _insert(db)
    resp = client.get("/graph/network?since=123")
    assert resp.status_code == 400
    assert "since" in resp.json()["detail"]


def test_network_graph_can_include_snr_labels(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, snr=3.5)
    resp = client.get("/graph/network?format=svg&snr_labels=true&include_clients=true")
    assert resp.status_code == 200
    assert b"3.5dB" in resp.content


def test_network_graph_suppresses_unknown_nodes_by_default(client, db):
    _insert(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start="a038868c-698282d0-1",
        link_end=NODE_B,
    )
    resp = client.get("/graph/network?format=svg")
    assert resp.status_code == 200
    assert b"a038868c" not in resp.content


def test_network_graph_can_include_unknown_nodes(client, db):
    _insert(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end="a038868c-698282d0-1",
    )
    resp = client.get("/graph/network?format=svg&include_unknown_nodes=true&include_clients=true")
    assert resp.status_code == 200
    assert b"a038868c" in resp.content


def test_network_graph_suppresses_snr_labels_by_default(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, snr=2.0)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, snr=6.0)
    resp = client.get("/graph/network?format=svg&include_clients=true")
    assert resp.status_code == 200
    assert b"<svg" in resp.content
    assert b"2.0..6.0dB" not in resp.content


def test_network_graph_can_include_snr_range_labels(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, snr=2.0)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, snr=6.0)
    resp = client.get("/graph/network?format=svg&snr_labels=true&include_clients=true")
    assert resp.status_code == 200
    assert b"2.0..6.0dB" in resp.content


def test_network_graph_route_does_not_exist(client, db):
    _insert(db)
    resp = client.get("/graph/network/simple")
    assert resp.status_code == 404


def test_network_graph_suppresses_unknown_nodes_by_default_on_collapsed_view(client, db):
    _insert(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end="a038868c-698282d0-1",
    )
    resp = client.get("/graph/network?format=svg&include_clients=true")
    assert resp.status_code == 200
    assert b"a038868c" not in resp.content


def test_network_graph_can_include_unknown_nodes_on_collapsed_view(client, db):
    _insert(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end="a038868c-698282d0-1",
    )
    resp = client.get("/graph/network?format=svg&include_unknown_nodes=true&include_clients=true")
    assert resp.status_code == 200
    assert b"a038868c" in resp.content


def test_network_graph_uses_compact_labels(client, db):
    with db:
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_A, "Alpha Long Name", "ALPHA", "ROUTER", NOW),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_B, "Beta Long Name", "BETA", "ROUTER", NOW),
        )
    _insert(db, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B)
    resp = client.get("/graph/network")
    assert resp.status_code == 200
    assert f"!{NODE_A:08x}".encode() in resp.content
    assert b"ALPHA" in resp.content
    assert b"Alpha Long Name" not in resp.content


def test_network_graph_time_range_collapsed_accepted(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, ts=PAST)
    _insert(db, trace_id=2, from_id=NODE_A, to_id=NODE_B, ts=NOW)
    resp = client.get(f"/graph/network?start={_iso(NOW - 60)}&include_clients=true")
    assert resp.status_code == 200


def test_network_graph_default_hides_client_only_paths(client, db):
    with db:
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_A, "A", "A", "CLIENT", NOW),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_B, "B", "B", "CLIENT", NOW),
        )
    _insert(db, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B)
    resp = client.get("/graph/network")
    assert resp.status_code == 200
    assert f"!{NODE_A:08x}".encode() not in resp.content


def test_network_graph_default_does_not_infer_core_link_via_client(client, db):
    node_c = 0xAAAA0003
    with db:
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_A, "A", "A", "ROUTER", NOW),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (NODE_B, "B", "B", "CLIENT", NOW),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (node_c, "C", "C", "CLIENT_BASE", NOW),
        )
    _insert(db, trace_id=1, from_id=NODE_A, to_id=node_c, link_start=NODE_A, link_end=NODE_B)
    _insert(db, trace_id=1, from_id=NODE_A, to_id=node_c, link_start=NODE_B, link_end=node_c)
    resp = client.get("/graph/network?format=svg")
    assert resp.status_code == 200
    assert f"!{NODE_A:08x}".encode() not in resp.content
    assert f"!{node_c:08x}".encode() not in resp.content


# ---------------------------------------------------------------------------
# /graph/trace/{trace_id}
# ---------------------------------------------------------------------------


def test_trace_graph_returns_svg_by_default(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}")
    assert resp.status_code == 200
    assert b"<svg" in resp.content


def test_trace_graph_returns_svg(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}?format=svg")
    assert resp.status_code == 200
    assert b"<svg" in resp.content


def test_trace_graph_404_for_unknown(client, db):
    resp = client.get("/graph/trace/9999")
    assert resp.status_code == 404


def test_trace_graph_renders_sparse_graph_when_trace_has_no_links(client, db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NOW),
        )
    resp = client.get(f"/graph/trace/{TRACE_1}?format=svg")
    assert resp.status_code == 200
    assert b"<svg" in resp.content
    assert b"!aaaa0001" in resp.content
    assert b"!aaaa0002" in resp.content


def test_trace_graph_defaults_to_most_recent_when_multiple_candidates(client, db):
    trace_id = 2001
    _insert(
        db, trace_id=trace_id, from_id=0x11111111, to_id=0x22222222, first_seen_ts=PAST, ts=PAST
    )
    _insert(db, trace_id=trace_id, from_id=0x33333333, to_id=0x44444444, first_seen_ts=NOW, ts=NOW)
    resp = client.get(f"/graph/trace/{trace_id}?format=svg")
    assert resp.status_code == 200
    assert b"!33333333" in resp.content
    assert b"!44444444" in resp.content
    assert b"!11111111" not in resp.content


def test_trace_graph_can_filter_by_from_and_to(client, db):
    trace_id = 2002
    _insert(
        db, trace_id=trace_id, from_id=0x11111111, to_id=0x22222222, first_seen_ts=PAST, ts=PAST
    )
    _insert(db, trace_id=trace_id, from_id=0x33333333, to_id=0x44444444, first_seen_ts=NOW, ts=NOW)
    resp = client.get(f"/graph/trace/{trace_id}?format=svg&from=!11111111&to=!22222222")
    assert resp.status_code == 200
    assert b"!11111111" in resp.content
    assert b"!22222222" in resp.content
    assert b"!33333333" not in resp.content


def test_trace_graph_can_filter_by_approximate_date(client, db):
    trace_id = 2003
    _insert(
        db, trace_id=trace_id, from_id=0x11111111, to_id=0x22222222, first_seen_ts=PAST, ts=PAST
    )
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


def test_trace_graph_unknown_query_param_returns_400(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}?start=2024-01-01T00:00:00Z")
    assert resp.status_code == 400
    assert "start" in resp.json()["detail"]


def test_trace_graph_direction_out_shows_only_outbound_edges(client, db):
    _insert(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=4.0,
        is_reply=0,
    )
    _insert(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=7.0,
        is_reply=1,
    )
    resp = client.get(f"/graph/trace/{TRACE_1}?format=svg&direction=out")
    assert resp.status_code == 200
    assert b"4.0dB" in resp.content
    assert b"7.0dB" not in resp.content


def test_trace_graph_direction_in_shows_only_reply_edges(client, db):
    _insert(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=4.0,
        is_reply=0,
    )
    _insert(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=7.0,
        is_reply=1,
    )
    resp = client.get(f"/graph/trace/{TRACE_1}?format=svg&direction=in")
    assert resp.status_code == 200
    assert b"7.0dB" in resp.content
    assert b"4.0dB" not in resp.content


def test_trace_graph_invalid_direction_returns_422(client, db):
    _insert(db)
    resp = client.get(f"/graph/trace/{TRACE_1}?direction=sideways")
    assert resp.status_code == 422


def test_trace_graph_displays_uplink_times_on_edges(client, db):
    uplink_1 = 0xAAAA0099
    uplink_2 = 0xAAAA00AB
    _insert(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=uplink_1
    )
    _insert(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=uplink_1, link_end=uplink_2
    )
    _insert(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=uplink_2, link_end=NODE_B
    )
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 0, NODE_A),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_2, NOW + 4, 0, NODE_A),
        )
        db.execute(
            "UPDATE traceroute_uplink SET prev_node = ? WHERE trace_id = ? AND from_id = ? AND to_id = ? AND uplink_id = ?",
            (uplink_1, TRACE_1, NODE_A, NODE_B, uplink_2),
        )
    resp = client.get(f"/graph/trace/{TRACE_1}?format=svg")
    assert resp.status_code == 200
    assert b"!aaaa0099" in resp.content
    assert b"!aaaa00ab" in resp.content
    assert b"Uplink: +0s@0" in resp.content
    assert b"Uplink: +4s@0" in resp.content


# ---------------------------------------------------------------------------
# /graph/node/{node_id}
# ---------------------------------------------------------------------------


def test_node_graph_returns_svg_by_default(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}")
    assert resp.status_code == 200
    assert b"<svg" in resp.content


def test_node_graph_returns_svg(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?format=svg")
    assert resp.status_code == 200
    assert b"<svg" in resp.content


def test_node_graph_collapses_links_with_snr_range(client, db):
    _insert(
        db, trace_id=1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B, snr=2.0
    )
    _insert(
        db, trace_id=2, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B, snr=6.0
    )
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


def test_node_graph_unknown_query_params_return_400(client, db):
    _insert(db)
    resp = client.get(f"/graph/node/!{NODE_A:08x}?since=123&to=foo")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "since" in detail
    assert "to" in detail


def test_node_graph_accepts_direction_and_depth(client, db):
    _insert(db, trace_id=1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B)
    _insert(
        db, trace_id=1, from_id=NODE_B, to_id=0xAAAA0003, link_start=NODE_B, link_end=0xAAAA0003
    )
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
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (0x1, "Old", "O", "CLIENT", PAST),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (0x2, "New", "N", "CLIENT", NOW),
        )
    resp = client.get("/nodes?after=%d&limit=10" % NOW)
    assert resp.status_code == 200
    data = resp.json()
    assert [n["nodenum"] for n in data] == [0x2, 0x1]


def test_nodes_support_after_cursor_and_limit(client, db):
    with db:
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (0x10, "A", "A", "CLIENT", NOW),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (0x20, "B", "B", "CLIENT", NOW - 10),
        )
        db.execute(
            "INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts) VALUES (?,?,?,?,?)",
            (0x30, "C", "C", "CLIENT", NOW - 20),
        )
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
    _insert(
        db, trace_id=5002, from_id=0xAAAA0001, to_id=0xBBBB0002, first_seen_ts=NOW - 5, ts=NOW - 5
    )
    _insert(
        db, trace_id=5003, from_id=0xCCCC0001, to_id=0xBBBB0001, first_seen_ts=NOW - 10, ts=NOW - 10
    )
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
