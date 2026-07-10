"""Tests for GeoJSON API endpoints."""

import json

import pytest
from fastapi.testclient import TestClient

from mesh_graph.api.app import create_app
from mesh_graph.db import upsert_node, upsert_position

from .conftest import insert_link

NODE_A = 0xAAAA0001
NODE_B = 0xAAAA0002


@pytest.fixture
def client(db):
    app = create_app(db)
    return TestClient(app)


def _set_positions(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="CLIENT")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="ROUTER")
    upsert_position(db, NODE_A, 1000000, 2000000, 32, "position_app")
    upsert_position(db, NODE_B, 3000000, 4000000, 32, "position_app")
    pos_a = db.execute("SELECT id FROM positions WHERE latitude_i = ?", (1000000,)).fetchone()[0]
    pos_b = db.execute("SELECT id FROM positions WHERE latitude_i = ?", (3000000,)).fetchone()[0]
    return pos_a, pos_b


def _insert_positioned_link(db, **kwargs):
    pos_a, pos_b = _set_positions(db)
    insert_link(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        link_start_position_id=pos_a,
        link_end_position_id=pos_b,
        **kwargs,
    )


def test_geojson_network_endpoint(client, db):
    _insert_positioned_link(db)
    resp = client.get("/api/geojson/network")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/geo+json"
    data = json.loads(resp.content)
    assert data["type"] == "FeatureCollection"
    assert any(f["properties"].get("layer") == "node" for f in data["features"])
    assert any(f["properties"].get("layer") == "edge" for f in data["features"])


def test_geojson_network_unknown_param_returns_400(client, db):
    _insert_positioned_link(db)
    resp = client.get("/api/geojson/network?since=123")
    assert resp.status_code == 400


def test_geojson_network_invalid_time_returns_422(client, db):
    resp = client.get("/api/geojson/network?start=not-a-time")
    assert resp.status_code == 422


def test_geojson_trace_endpoint(client, db):
    _insert_positioned_link(db)
    resp = client.get("/api/geojson/trace/1")
    assert resp.status_code == 200
    data = json.loads(resp.content)
    assert data["type"] == "FeatureCollection"
    nodes = [f for f in data["features"] if f["properties"]["layer"] == "node"]
    edges = [f for f in data["features"] if f["properties"]["layer"] == "edge"]
    assert len(nodes) == 2
    assert len(edges) == 1


def test_geojson_trace_not_found(client, db):
    resp = client.get("/api/geojson/trace/99999")
    assert resp.status_code == 404


def test_geojson_trace_direction_filter(client, db):
    _set_positions(db)
    pos_a, pos_b = _set_positions(db)
    insert_link(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        is_reply=0,
        link_start_position_id=pos_a,
        link_end_position_id=pos_b,
    )
    insert_link(
        db,
        trace_id=1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        is_reply=1,
        link_start_position_id=pos_b,
        link_end_position_id=pos_a,
    )

    resp = client.get("/api/geojson/trace/1?direction=out")
    assert resp.status_code == 200
    data = json.loads(resp.content)
    edges = [f for f in data["features"] if f["properties"]["layer"] == "edge"]
    assert len(edges) == 1
    assert edges[0]["properties"]["direction"] == "out"


def test_geojson_node_endpoint(client, db):
    _insert_positioned_link(db)
    resp = client.get(f"/api/geojson/node/!{NODE_A:08x}")
    assert resp.status_code == 200
    data = json.loads(resp.content)
    assert data["type"] == "FeatureCollection"
    nodes = [f for f in data["features"] if f["properties"]["layer"] == "node"]
    assert any(f["properties"]["nodenum"] == NODE_A for f in nodes)


def test_geojson_node_invalid_id(client, db):
    resp = client.get("/api/geojson/node/notanid")
    assert resp.status_code == 422


def test_geojson_node_unknown_param_returns_400(client, db):
    _insert_positioned_link(db)
    resp = client.get(f"/api/geojson/node/!{NODE_A:08x}?foo=bar")
    assert resp.status_code == 400
