from __future__ import annotations

import base64
import logging
import sqlite3
from typing import Optional

import paho.mqtt.client as mqtt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from google.protobuf import json_format
from meshtastic.protobuf import config_pb2, mesh_pb2, mqtt_pb2

from mesh_graph.db import get_connection, record_trace_uplink, upsert_node
from mesh_graph.ingestion.base import DataSource

logger = logging.getLogger(__name__)

UNK_SNR = -128


class MQTTDataSource(DataSource):
    def __init__(
        self,
        broker: str = "",
        port: int = 1883,
        username: str = "",
        password: str = "",
        topic: str = "msh/#",
        encryption_key: str = "1PG7OiApB1nwvP+rz05pAQ==",
    ) -> None:
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.topic = topic
        self.encryption_key = encryption_key
        self._client: Optional[mqtt.Client] = None
        self._db_path: Optional[str] = None

    # ------------------------------------------------------------------
    # DataSource interface
    # ------------------------------------------------------------------

    def start(self, db_path: str) -> None:
        self._db_path = db_path

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="",
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        if self.username:
            self._client.username_pw_set(self.username, self.password)
        self._client.connect(self.broker, self.port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logger.info("Connected to MQTT broker, subscribing to %s", self.topic)
            client.subscribe(self.topic)
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        logger.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        conn = get_connection(self._db_path)
        try:
            self.handle_message(conn, msg.payload)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Packet processing (public for testing)
    # ------------------------------------------------------------------

    def handle_message(self, conn: sqlite3.Connection, raw: bytes) -> None:
        try:
            se = mqtt_pb2.ServiceEnvelope()
            se.ParseFromString(raw)
            mp = se.packet
        except Exception as exc:
            logger.debug("Failed to parse ServiceEnvelope: %s", exc)
            return

        if mp.HasField("encrypted") and not mp.HasField("decoded"):
            try:
                self._decrypt(mp)
            except Exception as exc:
                logger.debug("Packet decryption failed: %s", exc)
                return

        try:
            as_dict = json_format.MessageToDict(mp)
        except Exception as exc:
            logger.debug("MessageToDict failed: %s", exc)
            return

        decoded = as_dict.get("decoded")
        if not decoded:
            return

        decoded["payload"] = mp.decoded.payload
        portnum = decoded.get("portnum", "")

        via = int(se.gateway_id[1:], 16) if se.gateway_id.startswith("!") else None

        if portnum == "TRACEROUTE_APP":
            try:
                self._handle_traceroute(conn, as_dict, via)
            except Exception as exc:
                logger.warning("Error processing TRACEROUTE_APP: %s", exc)

        elif portnum == "NODEINFO_APP":
            try:
                self._handle_nodeinfo(conn, mp)
            except Exception as exc:
                logger.warning("Error processing NODEINFO_APP: %s", exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _decrypt(self, mp: mesh_pb2.MeshPacket) -> None:
        key_bytes = base64.b64decode(self.encryption_key.encode("ascii"))
        nonce = getattr(mp, "id").to_bytes(8, "little") + getattr(mp, "from").to_bytes(8, "little")
        cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(nonce), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(mp.encrypted) + decryptor.finalize()
        data = mesh_pb2.Data()
        data.ParseFromString(decrypted)
        mp.decoded.CopyFrom(data)

    def _handle_nodeinfo(self, conn: sqlite3.Connection, mp: mesh_pb2.MeshPacket) -> None:
        user = mesh_pb2.User()
        user.ParseFromString(mp.decoded.payload)
        nodenum = getattr(mp, "from")
        role_val = user.role
        role_name = config_pb2.Config.DeviceConfig.Role.Name(role_val) if role_val else "CLIENT"
        upsert_node(
            conn, nodenum, long_name=user.long_name, short_name=user.short_name, role=role_name
        )
        logger.debug("Updated node info for !%08x (%s)", nodenum, user.long_name)

    def _handle_traceroute(
        self,
        conn: sqlite3.Connection,
        packet: dict,
        via: Optional[int],
    ) -> None:
        p = packet
        route_discovery = mesh_pb2.RouteDiscovery()
        route_discovery.ParseFromString(p["decoded"]["payload"])
        rd = json_format.MessageToDict(route_discovery)

        trace_direction = "REPLY" if "requestId" in p.get("decoded", {}) else "SEND"
        is_mqtt = via is not None

        trace_id = p["decoded"].get("requestId", p["id"])
        from_id = p["to"] if trace_direction == "REPLY" else p["from"]
        to_id = p["from"] if trace_direction == "REPLY" else p["to"]
        hop_start = self._extract_hop_field(p, "hopStart")
        hop_limit = self._extract_hop_field(p, "hopLimit") or 0

        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)",
                (trace_id, from_id, to_id),
            )
        if via is not None:
            is_reply = trace_direction == "REPLY"
            if is_reply:
                route_back = rd.get("routeBack", [])
                prev_node = route_back[-1] if route_back else to_id
            else:
                route = rd.get("route", [])
                prev_node = route[-1] if route else from_id
            record_trace_uplink(
                conn,
                trace_id=trace_id,
                from_id=from_id,
                to_id=to_id,
                uplink_id=via,
                is_reply=is_reply,
                prev_node=prev_node,
                hop_start=hop_start,
                hop_limit=hop_limit,
            )

        outbound_edges = self._build_outbound_edges(
            p, rd, trace_direction, is_mqtt, via, from_id, to_id
        )
        logger.debug("OUTBOUND edges: %s", outbound_edges)

        with conn:
            if trace_direction == "REPLY":
                query = (
                    "INSERT OR REPLACE INTO traceroute_link "
                    "(trace_id, from_id, to_id, link_start, link_end, snr, route_len, is_reply, is_fast_path) "
                    "VALUES (?,?,?,?,?,?,?,0,1)"
                )
            else:
                query = (
                    "INSERT OR IGNORE INTO traceroute_link "
                    "(trace_id, from_id, to_id, link_start, link_end, snr, route_len, is_reply, is_fast_path) "
                    "VALUES (?,?,?,?,?,?,?,0,0)"
                )
            conn.executemany(
                query,
                [(trace_id, from_id, to_id, e[0], e[1], e[2], e[3]) for e in outbound_edges],
            )

        if trace_direction == "REPLY":
            inbound_edges = self._build_inbound_edges(p, rd, is_mqtt, via, to_id)
            logger.debug("INBOUND edges: %s", inbound_edges)
            reply_fast_path = bool(is_mqtt and via == from_id)
            with conn:
                conn.executemany(
                    "INSERT INTO traceroute_link "
                    "(trace_id, from_id, to_id, link_start, link_end, snr, route_len, is_reply, is_fast_path) "
                    "VALUES (?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(trace_id, from_id, to_id, link_start, link_end, is_reply) "
                    "DO UPDATE SET is_fast_path = MAX(traceroute_link.is_fast_path, excluded.is_fast_path)",
                    [
                        (
                            trace_id,
                            from_id,
                            to_id,
                            e[0],
                            e[1],
                            e[2],
                            e[3],
                            1 if reply_fast_path else 0,
                        )
                        for e in inbound_edges
                    ],
                )

    def _build_outbound_edges(self, p, rd, trace_direction, is_mqtt, via, from_id, to_id):
        edges = []
        node_a = p["to"] if trace_direction == "REPLY" else p["from"]
        route = rd.get("route", [])
        route_len = len(route)
        snr_towards = rd.get("snrTowards", [])

        for idx, node_num in enumerate(route):
            node_b = node_num
            snr = None
            if len(snr_towards) >= len(route) and snr_towards[idx] != UNK_SNR:
                snr = snr_towards[idx] / 4
            if node_b == 4294967295:
                node_b = self._unknown_hop_id(
                    route, idx, from_id, p["from"] if trace_direction == "REPLY" else via
                )
            edges.append((node_a, node_b, snr, None))
            node_a = node_b

        if trace_direction == "REPLY":
            node_b = p["from"]
        elif is_mqtt:
            node_b = via
        else:
            node_b = p["to"]

        if node_a != node_b:
            snr = None
            if len(snr_towards) == len(route) + 1 and snr_towards[-1] != UNK_SNR:
                snr = snr_towards[-1] / 4
            elif p.get("rxSnr") is not None and p["rxSnr"] != UNK_SNR:
                snr = p["rxSnr"]
            edges.append((node_a, node_b, snr, route_len))

        return edges

    def _build_inbound_edges(self, p, rd, is_mqtt, via, to_id):
        edges = []
        node_a = p["from"]
        route_back = rd.get("routeBack", [])
        route_back_len = len(route_back)
        snr_back = rd.get("snrBack", [])

        for idx, node_num in enumerate(route_back):
            node_b = node_num
            snr = None
            if len(snr_back) >= len(route_back) and "hopStart" in p and snr_back[idx] != UNK_SNR:
                snr = snr_back[idx] / 4
            if node_b == 4294967295:
                node_b = self._unknown_hop_id(route_back, idx, to_id, p["to"])
            edges.append((node_a, node_b, snr, None))
            node_a = node_b

        node_b = via if is_mqtt else p["to"]
        if node_a != node_b:
            snr = None
            if len(snr_back) == len(route_back) + 1 and snr_back[-1] != UNK_SNR:
                snr = snr_back[-1] / 4
            elif p.get("rxSnr") is not None and p["rxSnr"] != UNK_SNR:
                snr = p["rxSnr"]
            edges.append((node_a, node_b, snr, route_back_len))

        return edges

    @staticmethod
    def _unknown_hop_id(route, idx, default_before, default_after):
        before = default_before
        before_idx = -1
        after = default_after
        for i in range(idx - 1, -1, -1):
            if route[i] != 4294967295:
                before = route[i]
                before_idx = i
                break
        for i in range(idx + 1, len(route)):
            if route[i] != 4294967295:
                after = route[i]
                break
        return f"{before:08x}-{after:08x}-{idx - before_idx}"

    @classmethod
    def _extract_hop_field(cls, packet: dict, field: str) -> Optional[int]:
        value = packet.get(field)
        if value is None:
            value = packet.get("decoded", {}).get(field)
        return cls._normalize_hop_value(value)

    @staticmethod
    def _normalize_hop_value(value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        if 0 <= normalized <= 7:
            return normalized
        return None
