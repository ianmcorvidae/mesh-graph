"""
Tests for position ingestion and snapshotting.

Covers:
  - DB schema: positions table, nodes.position_id/position_received_ts,
    traceroute_link position columns + received timestamps
  - upsert_position: dedup, priority logic (POSITION_APP > MapReport),
    position_received_ts updates
  - POSITION_APP handler
  - MapReport handler (via ServiceEnvelope with mp.from for nodenum)
  - _snapshot_link_positions: materializing positions onto traceroute links,
    including received timestamp snapshotting
"""

import sqlite3

import pytest
from meshtastic.protobuf import config_pb2, mesh_pb2, mqtt_pb2, portnums_pb2

from mesh_graph.db import (
    get_link_positions_for_trace,
    get_position,
    init_db,
    upsert_node,
    upsert_position,
)
from mesh_graph.ingestion.mqtt import (
    MQTTDataSource,
    _snapshot_link_positions,
)

DEFAULT_KEY = "1PG7OiApB1nwvP+rz05pAQ=="
FROM_ID = 0xAAAA0001
TO_ID = 0xAAAA0002
GATEWAY_ID = 0xAAAA0099


def _get_node_positions(conn, node_ids):
    """Test helper: return position data for each node via nodes.position_id."""
    if not node_ids:
        return {}
    result = {}
    placeholders = ",".join("?" * len(node_ids))
    for row in conn.execute(
        f"SELECT n.nodenum, p.* FROM nodes n "
        f"JOIN positions p ON n.position_id = p.id "
        f"WHERE n.nodenum IN ({placeholders})",
        node_ids,
    ).fetchall():
        result[row["nodenum"]] = row
    return result


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


@pytest.fixture
def source():
    return MQTTDataSource(encryption_key=DEFAULT_KEY)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_positions_table_exists(db):
    tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "positions" in tables


def test_positions_table_columns(db):
    cols = {c["name"] for c in db.execute("PRAGMA table_info(positions)").fetchall()}
    assert "id" in cols
    assert "latitude_i" in cols
    assert "longitude_i" in cols
    assert "precision" in cols
    assert "source" in cols


def test_positions_unique_constraint(db):
    db.execute(
        "INSERT INTO positions (latitude_i, longitude_i, precision, source) VALUES (?,?,?,?)",
        (1000000, 2000000, 32, "position_app"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO positions (latitude_i, longitude_i, precision, source) VALUES (?,?,?,?)",
            (1000000, 2000000, 32, "position_app"),
        )


def test_nodes_has_position_id_column(db):
    cols = {c["name"] for c in db.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "position_id" in cols


def test_traceroute_link_has_position_columns(db):
    cols = {c["name"] for c in db.execute("PRAGMA table_info(traceroute_link)").fetchall()}
    assert "link_start_position_id" in cols
    assert "link_end_position_id" in cols
    assert "link_start_position_received_ts" in cols
    assert "link_end_position_received_ts" in cols


def test_nodes_has_position_received_ts_column(db):
    cols = {c["name"] for c in db.execute("PRAGMA table_info(nodes)").fetchall()}
    assert "position_received_ts" in cols


def test_init_db_idempotent_with_positions(db):
    init_db(db)  # second call must not raise
    tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "positions" in tables


# ---------------------------------------------------------------------------
# upsert_position
# ---------------------------------------------------------------------------


def test_upsert_position_inserts_new_row(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    row = db.execute("SELECT * FROM positions").fetchone()
    assert row is not None
    assert row["latitude_i"] == 1000000
    assert row["longitude_i"] == 2000000
    assert row["precision"] == 32
    assert row["source"] == "position_app"


def test_upsert_position_creates_stub_node_if_missing(db):
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    node = db.execute("SELECT * FROM nodes WHERE nodenum = ?", (0x11111111,)).fetchone()
    assert node is not None
    assert node["position_id"] is not None


def test_upsert_position_updates_node_pointer(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    pos_id_1 = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()[0]
    assert pos_id_1 is not None

    upsert_position(db, 0x11111111, 1000001, 2000001, 24, "position_app")
    pos_id_2 = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()[0]
    assert pos_id_2 is not None
    assert pos_id_2 != pos_id_1


def test_upsert_position_deduplicates_identical_rows(db):
    upsert_node(db, 0x11111111)
    upsert_node(db, 0x22222222)

    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x22222222, 1000000, 2000000, 32, "position_app")

    count = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 1  # same position shared

    pos_1 = db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (0x11111111,)).fetchone()[
        0
    ]
    pos_2 = db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (0x22222222,)).fetchone()[
        0
    ]
    assert pos_1 == pos_2  # both point at same row


def test_upsert_position_different_source_creates_separate_row(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "map_report")

    count = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 2  # different source = different row

    # Node should still point at the position_app row
    node = db.execute(
        "SELECT n.position_id, p.source FROM nodes n JOIN positions p ON n.position_id = p.id "
        "WHERE n.nodenum = ?",
        (0x11111111,),
    ).fetchone()
    assert node["source"] == "position_app"


def test_upsert_position_map_report_does_not_overwrite_position_app(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    pos_app_id = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()[0]

    # MapReport should not update the pointer
    upsert_position(db, 0x11111111, 3000000, 4000000, 16, "map_report")
    pos_after = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()[0]
    assert pos_after == pos_app_id


def test_upsert_position_map_report_works_when_no_position_app(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 3000000, 4000000, 16, "map_report")
    node = db.execute(
        "SELECT n.position_id, p.source FROM nodes n JOIN positions p ON n.position_id = p.id "
        "WHERE n.nodenum = ?",
        (0x11111111,),
    ).fetchone()
    assert node is not None
    assert node["source"] == "map_report"


def test_upsert_position_replaces_same_source(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")

    count = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 1  # deduplicated


def test_upsert_position_sets_received_ts(db):
    import time

    upsert_node(db, 0x11111111)
    before = int(time.time())
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    after = int(time.time())

    row = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()
    assert row is not None
    assert before <= row["position_received_ts"] <= after


def test_upsert_position_map_report_sets_received_ts(db):
    import time

    upsert_node(db, 0x11111111)
    before = int(time.time())
    upsert_position(db, 0x11111111, 1000000, 2000000, 16, "map_report")
    after = int(time.time())

    row = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()
    assert row is not None
    assert before <= row["position_received_ts"] <= after


def test_upsert_position_map_report_does_not_update_received_ts(db):
    import time

    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    app_ts = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()["position_received_ts"]

    time.sleep(0.1)
    upsert_position(db, 0x11111111, 3000000, 4000000, 16, "map_report")

    row = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()
    assert row["position_received_ts"] == app_ts  # unchanged — map_report didn't overwrite


# ---------------------------------------------------------------------------
# get_position / get_link_positions_for_trace
# ---------------------------------------------------------------------------


def test_get_position(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    pos_id = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()[0]
    row = get_position(db, pos_id)
    assert row is not None
    assert row["latitude_i"] == 1000000


# ---------------------------------------------------------------------------
# POSITION_APP ingestion
# ---------------------------------------------------------------------------


def _make_position_se(
    nodenum=FROM_ID,
    latitude_i=1000000,
    longitude_i=2000000,
    precision_bits=32,
    timestamp=0,
    gateway_id=GATEWAY_ID,
):
    pos = mesh_pb2.Position()
    pos.latitude_i = latitude_i
    pos.longitude_i = longitude_i
    pos.precision_bits = precision_bits
    if timestamp:
        pos.timestamp = timestamp

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.POSITION_APP
    data.payload = pos.SerializeToString()

    mp = mesh_pb2.MeshPacket()
    mp.id = 88888
    setattr(mp, "from", nodenum)
    mp.to = 0xFFFFFFFF
    mp.decoded.CopyFrom(data)

    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = "LongFast"
    se.gateway_id = f"!{gateway_id:08x}"
    return se.SerializeToString()


def test_position_app_ingestion(db, source):
    upsert_node(db, FROM_ID)
    payload = _make_position_se(latitude_i=5000000, longitude_i=6000000, precision_bits=24)
    source.handle_message(db, payload)

    result = _get_node_positions(db, [FROM_ID])
    assert FROM_ID in result
    assert result[FROM_ID]["latitude_i"] == 5000000
    assert result[FROM_ID]["longitude_i"] == 6000000
    assert result[FROM_ID]["precision"] == 24
    assert result[FROM_ID]["source"] == "position_app"


def test_position_app_skips_zero_precision(db, source):
    upsert_node(db, FROM_ID)
    payload = _make_position_se(precision_bits=0)
    source.handle_message(db, payload)

    result = _get_node_positions(db, [FROM_ID])
    assert FROM_ID not in result


def test_position_app_skips_zero_coords(db, source):
    upsert_node(db, FROM_ID)
    payload = _make_position_se(latitude_i=0, longitude_i=0, precision_bits=32)
    source.handle_message(db, payload)

    result = _get_node_positions(db, [FROM_ID])
    assert FROM_ID not in result


def test_position_app_updates_on_repeat(db, source):
    upsert_node(db, FROM_ID)
    payload1 = _make_position_se(latitude_i=1000000, longitude_i=2000000, precision_bits=32)
    source.handle_message(db, payload1)

    payload2 = _make_position_se(latitude_i=3000000, longitude_i=4000000, precision_bits=24)
    source.handle_message(db, payload2)

    result = _get_node_positions(db, [FROM_ID])
    assert result[FROM_ID]["latitude_i"] == 3000000
    assert result[FROM_ID]["precision"] == 24


# ---------------------------------------------------------------------------
# MapReport ingestion
# ---------------------------------------------------------------------------


def _make_mapreport(
    latitude_i=1000000,
    longitude_i=2000000,
    position_precision=24,
    nodenum=FROM_ID,
    gateway_id=GATEWAY_ID,
):
    mr = mqtt_pb2.MapReport()
    mr.latitude_i = latitude_i
    mr.longitude_i = longitude_i
    mr.position_precision = position_precision
    mr.long_name = "Test Node"
    mr.short_name = "TN"
    mr.role = config_pb2.Config.DeviceConfig.Role.Value("CLIENT")

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.Value("MAP_REPORT_APP")
    data.payload = mr.SerializeToString()

    mp = mesh_pb2.MeshPacket()
    mp.id = 77777
    setattr(mp, "from", nodenum)
    mp.to = 0xFFFFFFFF
    mp.decoded.CopyFrom(data)

    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = "LongFast"
    se.gateway_id = f"!{gateway_id:08x}"
    return se.SerializeToString()


def test_mapreport_ingestion(db, source):
    upsert_node(db, FROM_ID)
    raw = _make_mapreport(latitude_i=5000000, longitude_i=6000000, position_precision=20)
    source.handle_message(db, raw)

    result = _get_node_positions(db, [FROM_ID])
    assert FROM_ID in result
    assert result[FROM_ID]["latitude_i"] == 5000000
    assert result[FROM_ID]["source"] == "map_report"


def test_mapreport_does_not_overwrite_position_app(db, source):
    upsert_node(db, FROM_ID)
    # First: POSITION_APP sets position
    pos_payload = _make_position_se(latitude_i=1000000, longitude_i=2000000, precision_bits=32)
    source.handle_message(db, pos_payload)

    pos_app_id = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (FROM_ID,)
    ).fetchone()[0]

    # Then: MapReport should not overwrite
    mr_raw = _make_mapreport(latitude_i=3000000, longitude_i=4000000, position_precision=16)
    source.handle_message(db, mr_raw)

    pos_after = db.execute(
        "SELECT position_id FROM nodes WHERE nodenum = ?", (FROM_ID,)
    ).fetchone()[0]
    assert pos_after == pos_app_id


def test_mapreport_skips_zero_precision(db, source):
    upsert_node(db, FROM_ID)
    raw = _make_mapreport(position_precision=0)
    source.handle_message(db, raw)

    result = _get_node_positions(db, [FROM_ID])
    assert FROM_ID not in result


def test_mapreport_skips_zero_coords(db, source):
    upsert_node(db, FROM_ID)
    raw = _make_mapreport(latitude_i=0, longitude_i=0, position_precision=24)
    source.handle_message(db, raw)

    result = _get_node_positions(db, [FROM_ID])
    assert FROM_ID not in result


# ---------------------------------------------------------------------------
# _snapshot_link_positions
# ---------------------------------------------------------------------------


def _insert_trace_with_links(db, trace_id, from_id, to_id, links):
    """Helper: insert a traceroute and its links."""
    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)",
            (trace_id, from_id, to_id),
        )
        for link in links:
            db.execute(
                "INSERT OR IGNORE INTO traceroute_link "
                "(trace_id, from_id, to_id, link_start, link_end, snr, is_reply) "
                "VALUES (?,?,?,?,?,?,?)",
                (trace_id, from_id, to_id, *link),
            )


def test_snapshot_link_positions(db):
    upsert_node(db, 0x11111111)
    upsert_node(db, 0x22222222)
    upsert_node(db, 0x33333333)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x22222222, 3000000, 4000000, 32, "position_app")
    # 0x33333333 has no position

    # Capture the timestamps set by upsert_position
    ts_11 = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()["position_received_ts"]
    ts_22 = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x22222222,)
    ).fetchone()["position_received_ts"]

    _insert_trace_with_links(
        db,
        100,
        0x11111111,
        0x33333333,
        [
            (0x11111111, 0x22222222, 5.0, 0),
            (0x22222222, 0x33333333, 3.0, 0),
        ],
    )

    _snapshot_link_positions(db, 100, 0x11111111, 0x33333333)

    rows = db.execute(
        "SELECT link_start, link_end, link_start_position_id, link_end_position_id, "
        "link_start_position_received_ts, link_end_position_received_ts "
        "FROM traceroute_link WHERE trace_id = 100 ORDER BY link_start"
    ).fetchall()

    # First link: 0x11111111 -> 0x22222222
    r0 = rows[0]
    assert r0["link_start_position_id"] is not None
    assert r0["link_end_position_id"] is not None
    assert r0["link_start_position_received_ts"] == ts_11
    assert r0["link_end_position_received_ts"] == ts_22

    # Second link: 0x22222222 -> 0x33333333
    r1 = rows[1]
    assert r1["link_start_position_id"] is not None
    assert r1["link_end_position_id"] is None  # 0x33333333 has no position
    assert r1["link_start_position_received_ts"] == ts_22
    assert r1["link_end_position_received_ts"] is None


def test_snapshot_link_positions_no_positions(db):
    upsert_node(db, 0x11111111)
    upsert_node(db, 0x22222222)
    # No positions set

    _insert_trace_with_links(
        db,
        200,
        0x11111111,
        0x22222222,
        [
            (0x11111111, 0x22222222, 5.0, 0),
        ],
    )

    _snapshot_link_positions(db, 200, 0x11111111, 0x22222222)

    rows = db.execute(
        "SELECT link_start_position_id, link_end_position_id, "
        "link_start_position_received_ts, link_end_position_received_ts "
        "FROM traceroute_link WHERE trace_id = 200"
    ).fetchall()
    assert rows[0]["link_start_position_id"] is None
    assert rows[0]["link_end_position_id"] is None
    assert rows[0]["link_start_position_received_ts"] is None
    assert rows[0]["link_end_position_received_ts"] is None


def test_snapshot_link_positions_shared_position_row(db):
    """Two nodes at same position share one row; snapshot should use the same position_id."""
    upsert_node(db, 0x11111111)
    upsert_node(db, 0x22222222)
    upsert_node(db, 0x33333333)
    # 0x11111111 and 0x22222222 share the same position
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x22222222, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x33333333, 3000000, 4000000, 32, "position_app")

    _insert_trace_with_links(
        db,
        300,
        0x11111111,
        0x33333333,
        [
            (0x11111111, 0x22222222, 5.0, 0),
            (0x22222222, 0x33333333, 3.0, 0),
        ],
    )

    _snapshot_link_positions(db, 300, 0x11111111, 0x33333333)

    rows = db.execute(
        "SELECT link_start_position_id, link_end_position_id "
        "FROM traceroute_link WHERE trace_id = 300 ORDER BY link_start"
    ).fetchall()

    # Both links should reference the same shared position for 0x11111111/0x22222222
    assert rows[0]["link_start_position_id"] == rows[0]["link_end_position_id"]


def test_snapshot_link_positions_string_nodes_get_null(db):
    upsert_node(db, 0x11111111)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    ts_11 = db.execute(
        "SELECT position_received_ts FROM nodes WHERE nodenum = ?", (0x11111111,)
    ).fetchone()["position_received_ts"]

    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)",
            (400, 0x11111111, 0x22222222),
        )
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, link_start, link_end, snr, is_reply) "
            "VALUES (?,?,?,?,?,?,?)",
            (400, 0x11111111, 0x22222222, 0x11111111, "aaaa0001-aaaa0002-1", 5.0, 0),
        )

    _snapshot_link_positions(db, 400, 0x11111111, 0x22222222)

    row = db.execute(
        "SELECT link_start_position_id, link_end_position_id, "
        "link_start_position_received_ts, link_end_position_received_ts "
        "FROM traceroute_link WHERE trace_id = 400"
    ).fetchone()
    assert row["link_start_position_id"] is not None
    assert row["link_end_position_id"] is None  # string node
    assert row["link_start_position_received_ts"] == ts_11
    assert row["link_end_position_received_ts"] is None


# ---------------------------------------------------------------------------
# get_link_positions_for_trace
# ---------------------------------------------------------------------------


def test_get_link_positions_for_trace(db):
    upsert_node(db, 0x11111111)
    upsert_node(db, 0x22222222)
    upsert_position(db, 0x11111111, 1000000, 2000000, 32, "position_app")
    upsert_position(db, 0x22222222, 3000000, 4000000, 32, "position_app")

    _insert_trace_with_links(
        db,
        500,
        0x11111111,
        0x22222222,
        [
            (0x11111111, 0x22222222, 5.0, 0),
        ],
    )
    _snapshot_link_positions(db, 500, 0x11111111, 0x22222222)

    rows = get_link_positions_for_trace(db, trace_id=500)
    assert len(rows) == 1
    assert rows[0]["start_lat_i"] == 1000000
    assert rows[0]["start_lon_i"] == 2000000
    assert rows[0]["end_lat_i"] == 3000000
    assert rows[0]["end_lon_i"] == 4000000


def test_get_link_positions_for_trace_no_positions(db):
    upsert_node(db, 0x11111111)
    upsert_node(db, 0x22222222)

    _insert_trace_with_links(
        db,
        600,
        0x11111111,
        0x22222222,
        [
            (0x11111111, 0x22222222, 5.0, 0),
        ],
    )
    _snapshot_link_positions(db, 600, 0x11111111, 0x22222222)

    rows = get_link_positions_for_trace(db, trace_id=600)
    assert len(rows) == 1
    assert rows[0]["start_lat_i"] is None
    assert rows[0]["end_lat_i"] is None


# ---------------------------------------------------------------------------
# End-to-end: traceroute ingestion triggers snapshotting
# ---------------------------------------------------------------------------


def test_traceroute_ingestion_snapshots_positions(db, source):
    upsert_node(db, FROM_ID, long_name="A", short_name="A", role="CLIENT")
    upsert_node(db, TO_ID, long_name="B", short_name="B", role="CLIENT")
    upsert_node(db, 0xAAAA1111, long_name="C", short_name="C", role="ROUTER")
    upsert_position(db, FROM_ID, 1000000, 2000000, 32, "position_app")
    upsert_position(db, TO_ID, 3000000, 4000000, 32, "position_app")
    upsert_position(db, 0xAAAA1111, 5000000, 6000000, 32, "position_app")

    rd = mesh_pb2.RouteDiscovery()
    rd.route.extend([0xAAAA1111])
    rd.snr_towards.extend([12, 8])

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    data.payload = rd.SerializeToString()
    data.want_response = True

    mp = mesh_pb2.MeshPacket()
    mp.id = 99999
    setattr(mp, "from", FROM_ID)
    mp.to = TO_ID
    mp.decoded.CopyFrom(data)

    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = "LongFast"
    se.gateway_id = f"!{GATEWAY_ID:08x}"

    source.handle_message(db, se.SerializeToString())

    rows = get_link_positions_for_trace(db, trace_id=99999, from_id=FROM_ID, to_id=TO_ID)
    assert len(rows) > 0

    # At least one link should have position data
    has_position = any(r["start_lat_i"] is not None for r in rows)
    assert has_position
