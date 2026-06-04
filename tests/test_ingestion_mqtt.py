"""
Tests for MQTTDataSource ingestion logic.

We test the packet-processing functions directly (not the MQTT broker connection),
by calling the internal handler with fabricated ServiceEnvelope payloads.
"""

import base64
import sqlite3

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from hypothesis import given, settings
from hypothesis import strategies as st
from meshtastic.protobuf import config_pb2, mesh_pb2, mqtt_pb2, portnums_pb2

from mesh_graph.db import get_links_for_network, get_links_for_trace, init_db
from mesh_graph.ingestion.mqtt import MQTTDataSource

DEFAULT_KEY = "1PG7OiApB1nwvP+rz05pAQ=="
FROM_ID = 0xAAAA0001
TO_ID = 0xAAAA0002
GATEWAY_ID = 0xAAAA0099
TRACE_ID = 12345

NODE_IDS = st.integers(min_value=5, max_value=0xFFFFFFFE)


def _expected_prev(route: list[int], via: int, fallback: int) -> int:
    for hop in reversed(route):
        if hop != via:
            return hop
    return fallback


def _new_db_and_source():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn, MQTTDataSource(encryption_key=DEFAULT_KEY)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


@pytest.fixture
def source():
    return MQTTDataSource(encryption_key=DEFAULT_KEY)


def _make_traceroute_se(
    packet_id=TRACE_ID,
    from_id=FROM_ID,
    to_id=TO_ID,
    gateway_id=GATEWAY_ID,
    route=None,
    snr_towards=None,
    route_back=None,
    snr_back=None,
    want_response=True,
    request_id=None,
    hop_start=None,
    hop_limit=None,
):
    rd = mesh_pb2.RouteDiscovery()
    if route:
        rd.route.extend(route)
    if snr_towards:
        rd.snr_towards.extend(snr_towards)
    if route_back:
        rd.route_back.extend(route_back)
    if snr_back:
        rd.snr_back.extend(snr_back)

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    data.payload = rd.SerializeToString()
    if want_response:
        data.want_response = True

    mp = mesh_pb2.MeshPacket()
    mp.id = packet_id
    setattr(mp, "from", from_id)
    mp.to = to_id
    if hop_start is not None:
        mp.hop_start = hop_start
    if hop_limit is not None:
        mp.hop_limit = hop_limit
    mp.decoded.CopyFrom(data)
    if request_id is not None:
        mp.decoded.request_id = request_id

    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = "LongFast"
    se.gateway_id = f"!{gateway_id:08x}"
    return se.SerializeToString()


def _make_nodeinfo_se(
    nodenum=FROM_ID,
    long_name="Test Node",
    short_name="TN",
    role=config_pb2.Config.DeviceConfig.ROUTER,
    gateway_id=GATEWAY_ID,
):
    user = mesh_pb2.User()
    user.id = f"!{nodenum:08x}"
    user.long_name = long_name
    user.short_name = short_name
    user.role = role

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.NODEINFO_APP
    data.payload = user.SerializeToString()

    mp = mesh_pb2.MeshPacket()
    mp.id = 9999
    setattr(mp, "from", nodenum)
    mp.to = 0xFFFFFFFF
    mp.decoded.CopyFrom(data)

    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = "LongFast"
    se.gateway_id = f"!{gateway_id:08x}"
    return se.SerializeToString()


def _make_encrypted_traceroute_se(
    packet_id=55555,
    from_id=FROM_ID,
    to_id=TO_ID,
    gateway_id=GATEWAY_ID,
    key=DEFAULT_KEY,
):
    rd = mesh_pb2.RouteDiscovery()
    rd.route.extend([0xAAAA1111])

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
    data.payload = rd.SerializeToString()

    key_bytes = base64.b64decode(key.encode("ascii"))
    nonce = packet_id.to_bytes(8, "little") + from_id.to_bytes(8, "little")
    cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(nonce), backend=default_backend())
    enc = cipher.encryptor()
    ciphertext = enc.update(data.SerializeToString()) + enc.finalize()

    mp = mesh_pb2.MeshPacket()
    mp.id = packet_id
    setattr(mp, "from", from_id)
    mp.to = to_id
    mp.encrypted = ciphertext

    se = mqtt_pb2.ServiceEnvelope()
    se.packet.CopyFrom(mp)
    se.channel_id = "LongFast"
    se.gateway_id = f"!{gateway_id:08x}"
    return se.SerializeToString()


# ---------------------------------------------------------------------------
# TRACEROUTE_APP
# ---------------------------------------------------------------------------


def test_traceroute_outbound_creates_db_rows(db, source):
    payload = _make_traceroute_se(route=[0xAAAA1111], snr_towards=[12, 8])
    source.handle_message(db, payload)

    rows = get_links_for_network(db)
    assert len(rows) > 0


def test_traceroute_reply_stores_return_links(db, source):
    """A reply packet (has request_id) should produce is_reply=1 rows."""
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        route=[0xAAAA1111],
        snr_towards=[8],
        route_back=[0xAAAA2222],
        snr_back=[6],
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)
    rows = get_links_for_network(db)
    reply_rows = [r for r in rows if r["is_reply"] == 1]
    assert len(reply_rows) > 0


def test_traceroute_reply_uplinked_by_origin_marks_route_back_fast_path(db, source):
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        gateway_id=FROM_ID,
        route_back=[0xAAAA2222],
        snr_back=[6],
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)
    rows = get_links_for_trace(db, trace_id=TRACE_ID, from_id=FROM_ID, to_id=TO_ID)
    reply_rows = [r for r in rows if r["is_reply"] == 1]
    assert len(reply_rows) == 2
    assert all(r["is_fast_path"] == 1 for r in reply_rows)


def test_traceroute_reply_non_origin_uplink_does_not_mark_route_back_fast_path(db, source):
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        gateway_id=GATEWAY_ID,
        route_back=[0xAAAA2222],
        snr_back=[6],
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)
    rows = get_links_for_trace(db, trace_id=TRACE_ID, from_id=FROM_ID, to_id=TO_ID)
    reply_rows = [r for r in rows if r["is_reply"] == 1]
    assert len(reply_rows) == 2
    assert all(r["is_fast_path"] == 0 for r in reply_rows)


def test_traceroute_inserts_traceroute_record(db, source):
    payload = _make_traceroute_se()
    source.handle_message(db, payload)
    row = db.execute("SELECT * FROM traceroute WHERE trace_id = ?", (TRACE_ID,)).fetchone()
    assert row is not None
    assert row["from_id"] == FROM_ID
    assert row["to_id"] == TO_ID


def test_traceroute_records_uplink_observation(db, source):
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID)
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT * FROM traceroute_uplink WHERE trace_id = ? AND from_id = ? AND to_id = ?",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchone()
    assert row is not None
    assert row["uplink_id"] == GATEWAY_ID
    assert row["is_reply"] == 0
    assert row["prev_node"] == FROM_ID  # no route hops → origin


def test_traceroute_records_multiple_uplinks_for_same_trace(db, source):
    payload_1 = _make_traceroute_se(gateway_id=GATEWAY_ID)
    payload_2 = _make_traceroute_se(gateway_id=0xAAAA00AB)
    source.handle_message(db, payload_1)
    source.handle_message(db, payload_2)
    rows = db.execute(
        "SELECT uplink_id FROM traceroute_uplink WHERE trace_id = ? AND from_id = ? AND to_id = ? "
        "ORDER BY uplink_id",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchall()
    assert [r["uplink_id"] for r in rows] == [GATEWAY_ID, 0xAAAA00AB]


def test_traceroute_records_hop_fields_on_uplink(db, source):
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID, hop_start=7, hop_limit=4)
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT hop_start, hop_limit FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND uplink_id = ?",
        (TRACE_ID, FROM_ID, TO_ID, GATEWAY_ID),
    ).fetchone()
    assert row["hop_start"] == 7
    assert row["hop_limit"] == 4


def test_traceroute_uplink_idempotent_on_duplicate_ingest(db, source):
    """Ingesting the same packet twice must not create a second row."""
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID)
    source.handle_message(db, payload)
    source.handle_message(db, payload)
    count = db.execute(
        "SELECT COUNT(*) FROM traceroute_uplink WHERE trace_id = ? AND from_id = ? AND to_id = ?",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchone()[0]
    assert count == 1


def test_traceroute_uplink_ignores_invalid_hop_values(db, source):
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID, hop_start=99, hop_limit=8)
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT hop_start, hop_limit FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND uplink_id = ?",
        (TRACE_ID, FROM_ID, TO_ID, GATEWAY_ID),
    ).fetchone()
    assert row["hop_start"] is None  # invalid value stays None
    assert row["hop_limit"] == 0  # invalid value coalesced to 0


def test_traceroute_outbound_prev_node_is_origin_when_route_empty(db, source):
    """No intermediate hops → prev_node should be the traceroute origin."""
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID, route=None)
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT prev_node FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 0",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchone()
    assert row["prev_node"] == FROM_ID


def test_traceroute_outbound_prev_node_is_last_route_hop(db, source):
    """With intermediate hops, prev_node should be the last hop in route."""
    HOP_A = 0xAAAA1111
    HOP_B = 0xAAAA2222
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID, route=[HOP_A, HOP_B])
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT prev_node FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 0",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchone()
    assert row["prev_node"] == HOP_B


def test_traceroute_outbound_prev_node_uses_prior_hop_when_route_ends_with_uplink(db, source):
    hop_before_uplink = 0xAAAA1111
    payload = _make_traceroute_se(gateway_id=GATEWAY_ID, route=[hop_before_uplink, GATEWAY_ID])
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT prev_node FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 0",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchone()
    assert row["prev_node"] == hop_before_uplink


def test_traceroute_reply_records_one_uplink_row(db, source):
    """A REPLY packet should produce exactly one uplink row (is_reply=True) for the return path."""
    HOP_FWD = 0xAAAA1111
    HOP_BACK = 0xAAAA2222
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        gateway_id=GATEWAY_ID,
        route=[HOP_FWD],
        route_back=[HOP_BACK],
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)
    rows = db.execute(
        "SELECT is_reply, prev_node FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND uplink_id = ?",
        (TRACE_ID, FROM_ID, TO_ID, GATEWAY_ID),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["is_reply"] == 1
    assert rows[0]["prev_node"] == HOP_BACK


def test_traceroute_reply_prev_node_falls_back_when_route_back_empty(db, source):
    """REPLY with empty route_back → prev_node uses the traceroute's destination."""
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        gateway_id=GATEWAY_ID,
        route=None,
        route_back=None,
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)
    rows = db.execute(
        "SELECT is_reply, prev_node FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND uplink_id = ?",
        (TRACE_ID, FROM_ID, TO_ID, GATEWAY_ID),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["is_reply"] == 1
    assert rows[0]["prev_node"] == TO_ID  # destination of traceroute


def test_traceroute_reply_prev_node_uses_prior_hop_when_route_back_ends_with_uplink(db, source):
    hop_before_uplink = 0xAAAA2222
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        gateway_id=GATEWAY_ID,
        route_back=[hop_before_uplink, GATEWAY_ID],
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)
    row = db.execute(
        "SELECT prev_node FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND uplink_id = ? AND is_reply = 1",
        (TRACE_ID, FROM_ID, TO_ID, GATEWAY_ID),
    ).fetchone()
    assert row["prev_node"] == hop_before_uplink


def test_traceroute_outbound_stores_route_len_only_on_terminal_link(db, source):
    payload = _make_traceroute_se(route=[0xAAAA1111, 0xAAAA2222], gateway_id=GATEWAY_ID)
    source.handle_message(db, payload)

    rows = db.execute(
        "SELECT link_start, link_end, route_len FROM traceroute_link "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 0",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchall()
    assert len(rows) == 3
    terminal = [r for r in rows if r["route_len"] is not None]
    non_terminal = [r for r in rows if r["route_len"] is None]
    assert len(terminal) == 1
    assert terminal[0]["route_len"] == 2
    assert len(non_terminal) == 2


def test_traceroute_reply_stores_route_back_len_only_on_terminal_link(db, source):
    payload = _make_traceroute_se(
        packet_id=99999,
        from_id=TO_ID,
        to_id=FROM_ID,
        gateway_id=GATEWAY_ID,
        route_back=[0xAAAA2222, 0xAAAA3333],
        want_response=False,
        request_id=TRACE_ID,
    )
    source.handle_message(db, payload)

    rows = db.execute(
        "SELECT link_start, link_end, route_len FROM traceroute_link "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 1",
        (TRACE_ID, FROM_ID, TO_ID),
    ).fetchall()
    assert len(rows) == 3
    terminal = [r for r in rows if r["route_len"] is not None]
    non_terminal = [r for r in rows if r["route_len"] is None]
    assert len(terminal) == 1
    assert terminal[0]["route_len"] == 2
    assert len(non_terminal) == 2


@settings(max_examples=200, deadline=None)
@given(
    from_id=NODE_IDS,
    to_id=NODE_IDS,
    gateway_id=NODE_IDS,
    packet_id=st.integers(min_value=1, max_value=0xFFFFFFFF),
    data=st.data(),
)
def test_property_outbound_prev_node_matches_last_non_uplink_hop(
    from_id, to_id, gateway_id, packet_id, data
):
    route = data.draw(
        st.lists(
            st.one_of(
                st.sampled_from([gateway_id, from_id, to_id]),
                NODE_IDS,
            ),
            min_size=0,
            max_size=6,
        )
    )
    db, source = _new_db_and_source()
    try:
        payload = _make_traceroute_se(
            packet_id=packet_id,
            from_id=from_id,
            to_id=to_id,
            gateway_id=gateway_id,
            route=route,
        )
        source.handle_message(db, payload)
        row = db.execute(
            "SELECT prev_node, uplink_id, from_id FROM traceroute_uplink "
            "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 0",
            (packet_id, from_id, to_id),
        ).fetchone()
        assert row is not None
        assert row["uplink_id"] == gateway_id
        assert row["prev_node"] == _expected_prev(route, gateway_id, from_id)
        assert row["prev_node"] != gateway_id or gateway_id == from_id
    finally:
        db.close()


@settings(max_examples=200, deadline=None)
@given(
    from_id=NODE_IDS,
    to_id=NODE_IDS,
    gateway_id=NODE_IDS,
    packet_id=st.integers(min_value=1, max_value=0xFFFFFFFF),
    request_id=st.integers(min_value=1, max_value=0xFFFFFFFF),
    data=st.data(),
)
def test_property_reply_prev_node_matches_last_non_uplink_hop(
    from_id, to_id, gateway_id, packet_id, request_id, data
):
    route_back = data.draw(
        st.lists(
            st.one_of(
                st.sampled_from([gateway_id, from_id, to_id]),
                NODE_IDS,
            ),
            min_size=0,
            max_size=6,
        )
    )
    db, source = _new_db_and_source()
    try:
        payload = _make_traceroute_se(
            packet_id=packet_id,
            from_id=to_id,
            to_id=from_id,
            gateway_id=gateway_id,
            route_back=route_back,
            want_response=False,
            request_id=request_id,
        )
        source.handle_message(db, payload)
        row = db.execute(
            "SELECT prev_node, uplink_id, to_id FROM traceroute_uplink "
            "WHERE trace_id = ? AND from_id = ? AND to_id = ? AND is_reply = 1",
            (request_id, from_id, to_id),
        ).fetchone()
        assert row is not None
        assert row["uplink_id"] == gateway_id
        assert row["prev_node"] == _expected_prev(route_back, gateway_id, to_id)
        assert row["prev_node"] != gateway_id or gateway_id == to_id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# NODEINFO_APP
# ---------------------------------------------------------------------------


def test_nodeinfo_upserts_node(db, source):
    payload = _make_nodeinfo_se(
        nodenum=FROM_ID, long_name="Test Node", role=config_pb2.Config.DeviceConfig.ROUTER
    )
    source.handle_message(db, payload)
    row = db.execute("SELECT * FROM nodes WHERE nodenum = ?", (FROM_ID,)).fetchone()
    assert row is not None
    assert row["long_name"] == "Test Node"
    assert row["role"] == "ROUTER"


def test_nodeinfo_updates_existing_node(db, source):
    payload1 = _make_nodeinfo_se(nodenum=FROM_ID, long_name="Old Name")
    payload2 = _make_nodeinfo_se(nodenum=FROM_ID, long_name="New Name")
    source.handle_message(db, payload1)
    source.handle_message(db, payload2)
    row = db.execute("SELECT long_name FROM nodes WHERE nodenum = ?", (FROM_ID,)).fetchone()
    assert row["long_name"] == "New Name"


# ---------------------------------------------------------------------------
# Decryption
# ---------------------------------------------------------------------------


def test_encrypted_packet_is_decrypted_and_stored(db, source):
    payload = _make_encrypted_traceroute_se()
    source.handle_message(db, payload)
    rows = get_links_for_network(db)
    assert len(rows) > 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_malformed_payload_does_not_raise(db, source):
    source.handle_message(db, b"this is not a valid protobuf")


def test_undecryptable_packet_does_not_raise(db, source):
    """Packet encrypted with a different key should be silently skipped."""
    wrong_key = "AAAAAAAAAAAAAAAAAAAAAA=="
    payload = _make_encrypted_traceroute_se(key=wrong_key)
    source.handle_message(db, payload)
    rows = get_links_for_network(db)
    assert len(rows) == 0
