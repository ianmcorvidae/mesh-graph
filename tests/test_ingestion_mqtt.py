"""
Tests for MQTTDataSource ingestion logic.

We test the packet-processing functions directly (not the MQTT broker connection),
by calling the internal handler with fabricated ServiceEnvelope payloads.
"""

import base64
import sqlite3
import pytest

from meshtastic.protobuf import mqtt_pb2, mesh_pb2, portnums_pb2, config_pb2
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from google.protobuf import json_format

from mesh_graph.db import init_db, get_links_for_trace, get_links_for_network
from mesh_graph.ingestion.mqtt import MQTTDataSource

DEFAULT_KEY = "1PG7OiApB1nwvP+rz05pAQ=="
FROM_ID = 0xAAAA0001
TO_ID = 0xAAAA0002
GATEWAY_ID = 0xAAAA0099
TRACE_ID = 12345


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


def test_traceroute_inserts_traceroute_record(db, source):
    payload = _make_traceroute_se()
    source.handle_message(db, payload)
    row = db.execute("SELECT * FROM traceroute WHERE trace_id = ?", (TRACE_ID,)).fetchone()
    assert row is not None
    assert row["from_id"] == FROM_ID
    assert row["to_id"] == TO_ID


# ---------------------------------------------------------------------------
# NODEINFO_APP
# ---------------------------------------------------------------------------

def test_nodeinfo_upserts_node(db, source):
    payload = _make_nodeinfo_se(nodenum=FROM_ID, long_name="Test Node", role=config_pb2.Config.DeviceConfig.ROUTER)
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
