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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traceroute_ts ON traceroute(first_seen_ts)")
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
        _ensure_column(conn, "traceroute_link", "route_len", "INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS traceroute_link_ts ON traceroute_link(ts)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_traceroute_link_start ON traceroute_link(link_start)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_traceroute_link_end ON traceroute_link(link_end)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                nodenum      INTEGER PRIMARY KEY,
                long_name    TEXT,
                short_name   TEXT,
                role         TEXT,
                last_seen_ts INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_lookup ON nodes(last_seen_ts DESC, nodenum DESC)"
        )
        _migrate_traceroute_uplink(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traceroute_uplink (
                trace_id  INTEGER NOT NULL,
                from_id   INTEGER NOT NULL,
                to_id     INTEGER NOT NULL,
                uplink_id INTEGER NOT NULL,
                ts        INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                hop_start INTEGER,
                hop_limit INTEGER,
                is_reply  BOOLEAN NOT NULL DEFAULT 0,
                prev_node NOT NULL,
                FOREIGN KEY (from_id, trace_id, to_id) REFERENCES traceroute,
                PRIMARY KEY (trace_id, from_id, to_id, uplink_id, is_reply, prev_node)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS traceroute_uplink_lookup "
            "ON traceroute_uplink(trace_id, from_id, to_id, ts, uplink_id)"
        )


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


def record_trace_uplink(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: int,
    to_id: int,
    uplink_id: int,
    is_reply: bool,
    prev_node,
    hop_start: Optional[int] = None,
    hop_limit: Optional[int] = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO traceroute_uplink "
            "(trace_id, from_id, to_id, uplink_id, is_reply, prev_node, hop_start, hop_limit) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (trace_id, from_id, to_id, uplink_id, is_reply, prev_node, hop_start, hop_limit),
        )


def _migrate_traceroute_uplink(conn: sqlite3.Connection) -> None:
    """Drop and recreate traceroute_uplink if it predates the multi-observation schema."""
    cols = {c["name"] for c in conn.execute("PRAGMA table_info(traceroute_uplink)").fetchall()}
    if not cols:
        return
    if "is_reply" in cols and "prev_node" in cols and "ts" in cols:
        return
    conn.execute("DROP TABLE traceroute_uplink")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(c["name"] == column for c in cols):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _node_id_str(nodenum: int) -> str:
    return f"!{nodenum:08x}"


def parse_node_id(node_id: str) -> int:
    """Accept '!aabbccdd', 0x-prefixed hex, plain hex, or decimal."""
    s = node_id.strip()
    if s.startswith("!"):
        return int(s[1:], 16)
    if s.lower().startswith("0x"):
        return int(s, 16)
    if s.isdigit():
        return int(s, 10)
    try:
        return int(s, 16)
    except ValueError:
        return int(s)


def _batched(iterable: list, n: int):
    """Yield successive n-sized chunks from iterable."""
    for i in range(0, len(iterable), n):
        yield iterable[i : i + n]


def get_node_attrs(
    conn: sqlite3.Connection,
    label_mode: str = "full",
    relevant_nodenums: Optional[set[int]] = None,
) -> dict:
    if label_mode not in {"full", "compact"}:
        raise ValueError(f"Unsupported label_mode '{label_mode}'")

    with traced_span("db.get_node_attrs", warn_ms=500) as span:
        attrs: dict = {}
        node_rows = 0

        if relevant_nodenums is not None:
            all_relevant: set[int] = set(relevant_nodenums)
            for batch in _batched(list(all_relevant), 500):
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT nodenum, long_name, short_name, role FROM nodes "
                    f"WHERE nodenum IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
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

            # Create stubs for any relevant nodenums not in the nodes table
            known_nodenums = {int(k[1:], 16) for k in attrs if k.startswith("!")}
            missing = all_relevant - known_nodenums
            stub_rows = len(missing)
            for nodenum in missing:
                name = _node_id_str(nodenum)
                attrs[name] = {"label": name, "color": _node_color(nodenum)}
        else:
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


def get_nodes(
    conn: sqlite3.Connection,
    cursor: Optional[int] = None,
    limit: int = 100,
    search: Optional[str] = None,
) -> tuple[list[sqlite3.Row], Optional[int]]:
    if cursor is None:
        cursor = int(time.time())

    if search:
        search_clean = search.strip().lstrip("!").lower().replace("0x", "")
        try:
            node_num = int(search_clean, 16)
            where = "(nodenum = ? OR long_name LIKE ? OR short_name LIKE ?)"
            params: list = [node_num, f"%{search}%", f"%{search}%"]
        except ValueError:
            where = "(long_name LIKE ? OR short_name LIKE ?)"
            params = [f"%{search}%", f"%{search}%"]

        query = (
            f"SELECT nodenum, long_name, short_name, role, last_seen_ts FROM nodes "
            f"WHERE {where} "
            f"ORDER BY last_seen_ts DESC, nodenum DESC LIMIT ?"
        )
        params.append(limit + 1)
    else:
        query = (
            "SELECT nodenum, long_name, short_name, role, last_seen_ts FROM nodes "
            "WHERE last_seen_ts <= ? "
            "ORDER BY last_seen_ts DESC, nodenum DESC LIMIT ?"
        )
        params = [cursor, limit + 1]

    rows = conn.execute(query, params).fetchall()
    next_cursor: Optional[int] = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1]["last_seen_ts"]
        rows = rows[:limit]
    return rows, next_cursor


def get_traceroutes(
    conn: sqlite3.Connection,
    cursor: Optional[int] = None,
    limit: int = 100,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
) -> tuple[list[sqlite3.Row], Optional[int]]:
    if cursor is None:
        cursor = int(time.time())

    params: list = [cursor]
    query = (
        "SELECT trace_id, from_id, to_id, first_seen_ts FROM traceroute WHERE first_seen_ts <= ?"
    )
    if from_id is not None:
        query += " AND from_id = ?"
        params.append(from_id)
    if to_id is not None:
        query += " AND to_id = ?"
        params.append(to_id)
    query += " ORDER BY first_seen_ts DESC, trace_id DESC, from_id DESC, to_id DESC LIMIT ?"
    params.append(limit + 1)

    rows = conn.execute(query, params).fetchall()
    next_cursor: Optional[int] = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1]["first_seen_ts"]
        rows = rows[:limit]
    return rows, next_cursor


def get_node(conn: sqlite3.Connection, nodenum: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT nodenum, long_name, short_name, role, last_seen_ts FROM nodes WHERE nodenum = ?",
        (nodenum,),
    ).fetchone()


def get_traceroutes_for_node(
    conn: sqlite3.Connection, nodenum: int, limit: int = 20
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT DISTINCT t.trace_id, t.from_id, t.to_id, t.first_seen_ts "
        "FROM traceroute t "
        "JOIN traceroute_link tl ON t.trace_id = tl.trace_id "
        "AND t.from_id = tl.from_id AND t.to_id = tl.to_id "
        "WHERE tl.link_start = ? OR tl.link_end = ? "
        "ORDER BY t.first_seen_ts DESC LIMIT ?",
        (nodenum, nodenum, limit),
    ).fetchall()


def get_recent_nodes(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT nodenum, long_name, short_name, role, last_seen_ts FROM nodes "
        "ORDER BY last_seen_ts DESC, nodenum DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_recent_traceroutes(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT trace_id, from_id, to_id, first_seen_ts FROM traceroute "
        "ORDER BY first_seen_ts DESC, trace_id DESC, from_id DESC, to_id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_dashboard_stats(conn: sqlite3.Connection) -> dict:
    node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    trace_count = conn.execute("SELECT COUNT(*) FROM traceroute").fetchone()[0]
    return {"node_count": node_count, "trace_count": trace_count}


def _node_color(nodenum: int) -> str:
    r = (nodenum & 0xFF0000) >> 16
    g = (nodenum & 0x00FF00) >> 8
    b = nodenum & 0x0000FF
    return f"#{r:02x}{g:02x}{b:02x}"


def _role_shape(role: Optional[str]) -> Optional[str]:
    if role in ("ROUTER", "ROUTER_CLIENT", "ROUTER_LATE", "REPEATER"):
        return "diamond"
    if role in ("CLIENT", "CLIENT_BASE", None):
        return "rect"
    return None  # CLIENT_MUTE, TRACKER, SENSOR, etc.


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
    trace = _select_trace_candidate(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )
    if trace is None:
        return []
    return conn.execute(
        "SELECT tl.*, t.from_id, t.to_id FROM traceroute_link tl "
        "JOIN traceroute t ON tl.trace_id = t.trace_id AND tl.from_id = t.from_id AND tl.to_id = t.to_id "
        "WHERE tl.trace_id = ? AND tl.from_id = ? AND tl.to_id = ? "
        "ORDER BY tl.ts ASC, tl.is_reply ASC, tl.is_fast_path DESC, tl.link_start ASC, tl.link_end ASC",
        (trace["trace_id"], trace["from_id"], trace["to_id"]),
    ).fetchall()


def get_trace_for_selector(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
    approx_ts: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    return _select_trace_candidate(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )


def get_uplinks_for_trace(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
    approx_ts: Optional[int] = None,
) -> list[sqlite3.Row]:
    trace = _select_trace_candidate(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )
    if trace is None:
        return []
    return conn.execute(
        "SELECT * FROM traceroute_uplink "
        "WHERE trace_id = ? AND from_id = ? AND to_id = ? "
        "ORDER BY ts ASC, uplink_id ASC, is_reply ASC",
        (trace["trace_id"], trace["from_id"], trace["to_id"]),
    ).fetchall()


def _select_trace_candidate(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
    approx_ts: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    query = "SELECT trace_id, from_id, to_id FROM traceroute WHERE trace_id = ?"
    params: list = [trace_id]
    if from_id is not None:
        query += " AND from_id = ?"
        params.append(from_id)
    if to_id is not None:
        query += " AND to_id = ?"
        params.append(to_id)
    if approx_ts is not None:
        query += (
            " ORDER BY ABS(first_seen_ts - ?) ASC, first_seen_ts DESC, from_id DESC, to_id DESC"
        )
        params.append(approx_ts)
    else:
        query += " ORDER BY first_seen_ts DESC, from_id DESC, to_id DESC"
    query += " LIMIT 1"

    return conn.execute(query, params).fetchone()


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


def get_links_for_nodes(
    conn: sqlite3.Connection,
    node_ids: list[int],
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    direction: str = "both",
    exclude_string_nodes: bool = False,
) -> list[sqlite3.Row]:
    if not node_ids:
        return []

    with traced_span("db.get_links_for_nodes", warn_ms=500) as span:
        all_rows: list[sqlite3.Row] = []
        for batch in _batched(node_ids, 500):
            placeholders = ",".join("?" * len(batch))
            conditions: list[str] = []
            params: list = []

            if direction in ("outbound", "both"):
                conditions.append(f"link_start IN ({placeholders})")
                params.extend(batch)
            if direction in ("inbound", "both"):
                conditions.append(f"link_end IN ({placeholders})")
                params.extend(batch)

            query = f"SELECT * FROM traceroute_link WHERE ({' OR '.join(conditions)})"
            if exclude_string_nodes:
                query += " AND typeof(link_start) = 'integer' AND typeof(link_end) = 'integer'"
            if start_ts is not None:
                query += " AND ts >= ?"
                params.append(start_ts)
            if end_ts is not None:
                query += " AND ts <= ?"
                params.append(end_ts)

            all_rows.extend(conn.execute(query, params).fetchall())

        span.set_attribute("db.row_count", len(all_rows))
        span.set_attribute("db.node_count", len(node_ids))
        span.set_attribute("db.direction", direction)
        return all_rows
