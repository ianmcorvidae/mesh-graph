import time

from mesh_graph.db import (
    get_links_for_network,
    get_links_for_node,
    get_links_for_trace,
    get_node_attrs,
    get_uplinks_for_trace,
    init_db,
    upsert_node,
)

NOW = int(time.time())
PAST = NOW - 7200
FUTURE = NOW + 7200


def _insert_link(
    db,
    trace_id,
    from_id,
    to_id,
    link_start,
    link_end,
    snr=None,
    is_reply=0,
    is_fast_path=0,
    ts=None,
):
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
    assert {"traceroute", "traceroute_link", "nodes", "traceroute_uplink"}.issubset(names)
    cols = db.execute("PRAGMA table_info(traceroute_link)").fetchall()
    assert "route_len" in {c["name"] for c in cols}


def test_init_db_migrates_old_uplink_table_to_new_schema():
    import sqlite3

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE traceroute (
                trace_id INTEGER NOT NULL,
                from_id  INTEGER NOT NULL,
                to_id    INTEGER NOT NULL,
                first_seen_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (from_id, trace_id, to_id)
            )
        """)
        conn.execute("""
            CREATE TABLE traceroute_uplink (
                trace_id      INTEGER NOT NULL,
                from_id       INTEGER NOT NULL,
                to_id         INTEGER NOT NULL,
                uplink_id     INTEGER NOT NULL,
                first_seen_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                hop_start     INTEGER,
                hop_limit     INTEGER,
                FOREIGN KEY (from_id, trace_id, to_id) REFERENCES traceroute,
                PRIMARY KEY (trace_id, from_id, to_id, uplink_id)
            )
        """)
    init_db(conn)
    cols = conn.execute("PRAGMA table_info(traceroute_uplink)").fetchall()
    names = {c["name"] for c in cols}
    assert "is_reply" in names
    assert "prev_node" in names
    assert "ts" in names
    assert "first_seen_ts" not in names
    conn.close()


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


def test_get_links_for_trace_uses_latest_trace_for_same_trace_id(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (88, 0x01, 0x02, NOW - 10),
        )
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (88, 0x03, 0x04, NOW),
        )
        db.execute(
            "INSERT INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end) VALUES (?,?,?,?,?,?)",
            (88, 0x01, 0x02, NOW - 10, 0x01, 0x02),
        )
        db.execute(
            "INSERT INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end) VALUES (?,?,?,?,?,?)",
            (88, 0x03, 0x04, NOW, 0x03, 0x04),
        )

    rows = get_links_for_trace(db, trace_id=88)
    assert len(rows) == 1
    assert rows[0]["from_id"] == 0x03
    assert rows[0]["to_id"] == 0x04


def test_get_links_for_trace_filters_by_from_and_to(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (89, 0x10, 0x20, NOW - 10),
        )
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (89, 0x30, 0x40, NOW),
        )
        db.execute(
            "INSERT INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end) VALUES (?,?,?,?,?,?)",
            (89, 0x10, 0x20, NOW - 10, 0x10, 0x20),
        )
        db.execute(
            "INSERT INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end) VALUES (?,?,?,?,?,?)",
            (89, 0x30, 0x40, NOW, 0x30, 0x40),
        )

    rows = get_links_for_trace(db, trace_id=89, from_id=0x10, to_id=0x20)
    assert len(rows) == 1
    assert rows[0]["from_id"] == 0x10
    assert rows[0]["to_id"] == 0x20


def test_get_links_for_trace_uses_approx_ts_when_selecting_candidate(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (90, 0x50, 0x60, PAST),
        )
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (90, 0x70, 0x80, NOW),
        )
        db.execute(
            "INSERT INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end) VALUES (?,?,?,?,?,?)",
            (90, 0x50, 0x60, PAST, 0x50, 0x60),
        )
        db.execute(
            "INSERT INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end) VALUES (?,?,?,?,?,?)",
            (90, 0x70, 0x80, NOW, 0x70, 0x80),
        )

    rows = get_links_for_trace(db, trace_id=90, approx_ts=PAST + 2)
    assert len(rows) == 1
    assert rows[0]["from_id"] == 0x50
    assert rows[0]["to_id"] == 0x60


def test_get_uplinks_for_trace_orders_by_ts(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (91, 0x50, 0x60, NOW),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (91, 0x50, 0x60, 0xAAAA0099, NOW + 4, 0, 0x50),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (91, 0x50, 0x60, 0xAAAA0001, NOW, 0, 0x50),
        )
    rows = get_uplinks_for_trace(db, trace_id=91)
    assert [r["uplink_id"] for r in rows] == [0xAAAA0001, 0xAAAA0099]


def test_get_uplinks_for_trace_uses_same_candidate_selection(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (92, 0x10, 0x20, PAST),
        )
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (92, 0x30, 0x40, NOW),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (92, 0x10, 0x20, 0xAAAA0001, PAST, 0, 0x10),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (92, 0x30, 0x40, 0xAAAA0002, NOW, 0, 0x30),
        )
    rows_latest = get_uplinks_for_trace(db, trace_id=92)
    assert len(rows_latest) == 1
    assert rows_latest[0]["from_id"] == 0x30
    assert rows_latest[0]["to_id"] == 0x40
    assert rows_latest[0]["uplink_id"] == 0xAAAA0002

    rows_approx = get_uplinks_for_trace(db, trace_id=92, approx_ts=PAST + 1)
    assert len(rows_approx) == 1
    assert rows_approx[0]["from_id"] == 0x10
    assert rows_approx[0]["to_id"] == 0x20
    assert rows_approx[0]["uplink_id"] == 0xAAAA0001


def test_get_uplinks_for_trace_includes_hop_fields(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (93, 0x50, 0x60, NOW),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node, hop_start, hop_limit) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (93, 0x50, 0x60, 0xAAAA0099, NOW, 0, 0x50, 7, 4),
        )
    rows = get_uplinks_for_trace(db, trace_id=93)
    assert rows[0]["hop_start"] == 7
    assert rows[0]["hop_limit"] == 4


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
