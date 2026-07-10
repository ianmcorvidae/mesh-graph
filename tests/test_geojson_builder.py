"""Tests for GeoJSON builders."""

from mesh_graph.db import get_position, init_db, upsert_node, upsert_position
from mesh_graph.geo.builder import (
    build_network_geojson,
    build_node_geojson,
    build_trace_geojson,
)

NODE_A = 0xAAAA0001
NODE_B = 0xAAAA0002
NODE_C = 0xAAAA0003


def _insert_positioned_link(db, trace_id, from_id, to_id, start, end, **kwargs):
    now = 1000000
    defaults = {
        "ts": now,
        "snr": 5.0,
        "is_reply": 0,
        "is_fast_path": 0,
        "route_len": None,
        "link_start_position_id": None,
        "link_end_position_id": None,
    }
    defaults.update(kwargs)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (trace_id, from_id, to_id, defaults["ts"]),
        )
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path, "
            "route_len, link_start_position_id, link_end_position_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trace_id,
                from_id,
                to_id,
                defaults["ts"],
                start,
                end,
                defaults["snr"],
                defaults["is_reply"],
                defaults["is_fast_path"],
                defaults["route_len"],
                defaults["link_start_position_id"],
                defaults["link_end_position_id"],
            ),
        )


def _set_node_positions(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="CLIENT")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="ROUTER")
    upsert_node(db, NODE_C, long_name="C", short_name="C", role="CLIENT")
    upsert_position(db, NODE_A, 1000000, 2000000, 32, "position_app")
    upsert_position(db, NODE_B, 3000000, 4000000, 32, "position_app")
    # NODE_C has no position


def test_build_network_geojson_basic(db):
    init_db(db)
    _set_node_positions(db)
    pos_a = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_A,)).fetchone()[0]
    )
    pos_b = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_B,)).fetchone()[0]
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        NODE_A,
        NODE_B,
        link_start_position_id=pos_a["id"],
        link_end_position_id=pos_b["id"],
    )

    geo = build_network_geojson(db)
    assert geo["type"] == "FeatureCollection"
    nodes = [f for f in geo["features"] if f["properties"]["layer"] == "node"]
    edges = [f for f in geo["features"] if f["properties"]["layer"] == "edge"]
    assert len(nodes) == 2  # A and B have positions; C is not in links
    assert len(edges) == 1

    edge = edges[0]
    assert edge["geometry"]["type"] == "LineString"
    assert edge["properties"]["link_start"] == NODE_A
    assert edge["properties"]["link_end"] == NODE_B
    assert edge["properties"]["snr"] == 5.0


def test_build_network_geojson_approximates_positionless_node(db):
    init_db(db)
    _set_node_positions(db)
    pos_a = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_A,)).fetchone()[0]
    )
    pos_b = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_B,)).fetchone()[0]
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_C,
        NODE_A,
        NODE_C,
        link_start_position_id=pos_a["id"],
        link_end_position_id=None,
    )
    _insert_positioned_link(
        db,
        2,
        NODE_C,
        NODE_B,
        NODE_C,
        NODE_B,
        link_start_position_id=None,
        link_end_position_id=pos_b["id"],
    )

    geo = build_network_geojson(db, include_clients=True)
    nodes = {
        f["properties"]["nodenum"]: f for f in geo["features"] if f["properties"]["layer"] == "node"
    }
    assert NODE_C in nodes
    assert nodes[NODE_C]["properties"]["is_approximated"] is True
    assert nodes[NODE_C]["geometry"]["coordinates"] != nodes[NODE_A]["geometry"]["coordinates"]


def test_build_network_geojson_respects_direction(db):
    init_db(db)
    _set_node_positions(db)
    pos_a = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_A,)).fetchone()[0]
    )
    pos_b = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_B,)).fetchone()[0]
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        NODE_A,
        NODE_B,
        is_reply=0,
        link_start_position_id=pos_a["id"],
        link_end_position_id=pos_b["id"],
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        NODE_B,
        NODE_A,
        is_reply=1,
        link_start_position_id=pos_b["id"],
        link_end_position_id=pos_a["id"],
    )

    out_geo = build_network_geojson(db, direction="outbound")
    assert len([f for f in out_geo["features"] if f["properties"]["layer"] == "edge"]) == 1
    assert all(
        f["properties"]["direction"] == "out"
        for f in out_geo["features"]
        if f["properties"]["layer"] == "edge"
    )

    in_geo = build_network_geojson(db, direction="inbound")
    assert len([f for f in in_geo["features"] if f["properties"]["layer"] == "edge"]) == 1
    assert all(
        f["properties"]["direction"] == "in"
        for f in in_geo["features"]
        if f["properties"]["layer"] == "edge"
    )


def test_build_trace_geojson(db):
    init_db(db)
    _set_node_positions(db)
    pos_a = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_A,)).fetchone()[0]
    )
    pos_b = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_B,)).fetchone()[0]
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        NODE_A,
        NODE_B,
        link_start_position_id=pos_a["id"],
        link_end_position_id=pos_b["id"],
    )

    geo = build_trace_geojson(db, trace_id=1, from_id=NODE_A, to_id=NODE_B)
    assert geo is not None
    nodes = {
        f["properties"]["nodenum"]: f for f in geo["features"] if f["properties"]["layer"] == "node"
    }
    edges = [f for f in geo["features"] if f["properties"]["layer"] == "edge"]
    assert nodes[NODE_A]["properties"]["is_source"] is True
    assert nodes[NODE_B]["properties"]["is_dest"] is True
    assert len(edges) == 1
    assert edges[0]["properties"]["direction"] == "out"


def test_build_trace_geojson_unknown_hop(db):
    init_db(db)
    _set_node_positions(db)
    pos_a = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_A,)).fetchone()[0]
    )
    pos_b = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_B,)).fetchone()[0]
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        NODE_A,
        "aaaa0001-aaaa0002-1",
        link_start_position_id=pos_a["id"],
        link_end_position_id=None,
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        "aaaa0001-aaaa0002-1",
        NODE_B,
        link_start_position_id=None,
        link_end_position_id=pos_b["id"],
    )

    geo = build_trace_geojson(db, trace_id=1, from_id=NODE_A, to_id=NODE_B)
    assert geo is not None
    edges = [f for f in geo["features"] if f["properties"]["layer"] == "edge"]
    assert len(edges) == 2
    assert all(f["properties"]["is_unknown_hop"] for f in edges)
    assert all(f["properties"]["direction"] == "out" for f in edges)


def test_build_node_geojson(db):
    init_db(db)
    _set_node_positions(db)
    pos_a = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_A,)).fetchone()[0]
    )
    pos_b = get_position(
        db, db.execute("SELECT position_id FROM nodes WHERE nodenum = ?", (NODE_B,)).fetchone()[0]
    )
    _insert_positioned_link(
        db,
        1,
        NODE_A,
        NODE_B,
        NODE_A,
        NODE_B,
        link_start_position_id=pos_a["id"],
        link_end_position_id=pos_b["id"],
    )

    geo = build_node_geojson(db, node_id=NODE_A, depth=1)
    assert geo["type"] == "FeatureCollection"
    nodes = {
        f["properties"]["nodenum"]: f for f in geo["features"] if f["properties"]["layer"] == "node"
    }
    edges = [f for f in geo["features"] if f["properties"]["layer"] == "edge"]
    assert NODE_A in nodes
    assert nodes[NODE_A]["properties"]["is_center"] is True
    assert NODE_B in nodes
    assert len(edges) == 1


def test_build_network_geojson_falls_back_to_current_node_position(db):
    """If traceroute_link has no materialized positions, use nodes.position_id."""
    init_db(db)
    _set_node_positions(db)
    # Insert a link WITHOUT materialized positions
    _insert_positioned_link(db, 1, NODE_A, NODE_B, NODE_A, NODE_B)

    geo = build_network_geojson(db)
    nodes = {
        f["properties"]["nodenum"]: f for f in geo["features"] if f["properties"]["layer"] == "node"
    }
    assert NODE_A in nodes
    assert NODE_B in nodes
    # Both should be real positions, not approximated to 0,0
    assert nodes[NODE_A]["properties"]["is_approximated"] is False
    assert nodes[NODE_B]["properties"]["is_approximated"] is False
    assert nodes[NODE_A]["geometry"]["coordinates"] != [0.0, 0.0]
    assert nodes[NODE_B]["geometry"]["coordinates"] != [0.0, 0.0]
