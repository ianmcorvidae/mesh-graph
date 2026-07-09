import sqlite3
import time

import pytest

from mesh_graph.db import init_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


def insert_link(
    db,
    trace_id,
    from_id,
    to_id,
    link_start,
    link_end,
    snr=None,
    is_reply=0,
    is_fast_path=0,
    route_len=None,
    ts=None,
    first_seen_ts=None,
):
    now = int(time.time())
    ts = ts if ts is not None else now
    first_seen_ts = first_seen_ts if first_seen_ts is not None else ts
    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (trace_id, from_id, to_id, first_seen_ts),
        )
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path, route_len) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                trace_id,
                from_id,
                to_id,
                ts,
                link_start,
                link_end,
                snr,
                is_reply,
                is_fast_path,
                route_len,
            ),
        )
