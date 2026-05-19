from __future__ import annotations

import sqlite3
import time
from typing import Optional

from mesh_graph.observability import traced_span


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traceroute (
                trace_id INTEGER NOT NULL,
                from_id  INTEGER NOT NULL,
                to_id    INTEGER NOT NULL,
                first_seen_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (from_id, trace_id, to_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traceroute_link (
                trace_id     INTEGER NOT NULL,
                from_id      INTEGER NOT NULL,
                to_id        INTEGER NOT NULL,
                ts           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                link_start   NOT NULL,
                link_end     NOT NULL,
                snr          REAL,
                is_reply     BOOLEAN NOT NULL DEFAULT 0,
                is_fast_path BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY (from_id, trace_id, to_id) REFERENCES traceroute,
                PRIMARY KEY (trace_id, from_id, to_id, link_start, link_end, is_reply)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS traceroute_link_ts ON traceroute_link(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                nodenum      INTEGER PRIMARY KEY,
                long_name    TEXT,
                short_name   TEXT,
                role         TEXT,
                last_seen_ts INTEGER DEFAULT (strftime('%s','now'))
            )
        """)


def upsert_node(
    conn: sqlite3.Connection,
    nodenum: int,
    long_name: str = "",
    short_name: str = "",
    role: str = "CLIENT",
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO nodes (nodenum, long_name, short_name, role, last_seen_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(nodenum) DO UPDATE SET
                long_name    = excluded.long_name,
                short_name   = excluded.short_name,
                role         = excluded.role,
                last_seen_ts = excluded.last_seen_ts
            """,
            (nodenum, long_name, short_name, role, int(time.time())),
        )


def _node_id_str(nodenum: int) -> str:
    return f"!{nodenum:08x}"


def get_node_attrs(conn: sqlite3.Connection, label_mode: str = "full") -> dict:
    if label_mode not in {"full", "compact"}:
        raise ValueError(f"Unsupported label_mode '{label_mode}'")

    with traced_span("db.get_node_attrs", warn_ms=500) as span:
        attrs: dict = {}
        node_rows = 0
        for row in conn.execute("SELECT nodenum, long_name, short_name, role FROM nodes"):
            node_rows += 1
            nodenum = row["nodenum"]
            name = _node_id_str(nodenum)
            label = name
            if label_mode == "compact" and row["short_name"]:
                label = f"{name}\n{row['short_name']}"
            elif label_mode == "full" and row["long_name"]:
                label = f"{name}\n{row['long_name']}\n{row['role'] or 'CLIENT'}"
            color = _node_color(nodenum)
            shape = _role_shape(row["role"])
            entry = {"label": label, "color": color}
            if shape:
                entry["shape"] = shape
            attrs[name] = entry

        # Ensure every node that appears in links has at least a stub entry
        stub_rows = 0
        for row in conn.execute(
            "SELECT DISTINCT link_start, link_end FROM traceroute_link "
            "WHERE typeof(link_start)='integer' OR typeof(link_end)='integer'"
        ):
            stub_rows += 1
            for val in (row["link_start"], row["link_end"]):
                if isinstance(val, int):
                    name = _node_id_str(val)
                    if name not in attrs:
                        attrs[name] = {"label": name, "color": _node_color(val)}
        span.set_attribute("db.nodes_rows", node_rows)
        span.set_attribute("db.stub_rows", stub_rows)
        span.set_attribute("db.attrs_count", len(attrs))
        span.set_attribute("db.label_mode", label_mode)
        return attrs


def _node_color(nodenum: int) -> str:
    r = (nodenum & 0xFF0000) >> 16
    g = (nodenum & 0x00FF00) >> 8
    b = nodenum & 0x0000FF
    return f"#{r:02x}{g:02x}{b:02x}"


def _role_shape(role: Optional[str]) -> Optional[str]:
    if role in ("ROUTER", "ROUTER_CLIENT", "REPEATER"):
        return "rect"
    if role in ("CLIENT", "CLIENT_BASE", "ROUTER_LATE", None):
        return "diamond"
    return None


def get_links_for_network(
    conn: sqlite3.Connection,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> list[sqlite3.Row]:
    with traced_span(
        "db.get_links_for_network",
        warn_ms=500,
        attributes={"db.start_ts": start_ts, "db.end_ts": end_ts},
    ) as span:
        query = "SELECT * FROM traceroute_link WHERE 1=1"
        params: list = []
        if start_ts is not None:
            query += " AND ts >= ?"
            params.append(start_ts)
        if end_ts is not None:
            query += " AND ts <= ?"
            params.append(end_ts)
        rows = conn.execute(query, params).fetchall()
        span.set_attribute("db.row_count", len(rows))
        return rows


def get_links_for_trace(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
    approx_ts: Optional[int] = None,
) -> list[sqlite3.Row]:
    query = (
        "SELECT trace_id, from_id, to_id FROM traceroute "
        "WHERE trace_id = ?"
    )
    params: list = [trace_id]
    if from_id is not None:
        query += " AND from_id = ?"
        params.append(from_id)
    if to_id is not None:
        query += " AND to_id = ?"
        params.append(to_id)
    if approx_ts is not None:
        query += " ORDER BY ABS(first_seen_ts - ?) ASC, first_seen_ts DESC, from_id DESC, to_id DESC"
        params.append(approx_ts)
    else:
        query += " ORDER BY first_seen_ts DESC, from_id DESC, to_id DESC"
    query += " LIMIT 1"

    trace = conn.execute(query, params).fetchone()
    if trace is None:
        return []
    return conn.execute(
        "SELECT tl.*, t.from_id, t.to_id FROM traceroute_link tl "
        "JOIN traceroute t ON tl.trace_id = t.trace_id AND tl.from_id = t.from_id AND tl.to_id = t.to_id "
        "WHERE tl.trace_id = ? AND tl.from_id = ? AND tl.to_id = ?",
        (trace["trace_id"], trace["from_id"], trace["to_id"]),
    ).fetchall()


def get_links_for_node(
    conn: sqlite3.Connection,
    node_id: int,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> list[sqlite3.Row]:
    query = "SELECT * FROM traceroute_link WHERE (link_start = ? OR link_end = ?)"
    params: list = [node_id, node_id]
    if start_ts is not None:
        query += " AND ts >= ?"
        params.append(start_ts)
    if end_ts is not None:
        query += " AND ts <= ?"
        params.append(end_ts)
    return conn.execute(query, params).fetchall()
