import sqlite3
import time
import pytest
import networkx as nx

from mesh_graph.db import init_db, upsert_node
from mesh_graph.graph.builder import (
    build_network_graph,
    build_simple_network_graph,
    build_trace_graph,
    build_node_graph,
)

NOW = int(time.time())
PAST = NOW - 7200

# Node IDs used across tests
NODE_A = 0xAAAA0001
NODE_B = 0xAAAA0002
NODE_C = 0xAAAA0003
TRACE_1 = 1001
TRACE_2 = 1002


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _insert(db, trace_id, from_id, to_id, link_start, link_end, snr=None, is_reply=0, is_fast_path=0, ts=None):
    ts = ts or NOW
    with db:
        db.execute("INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)", (trace_id, from_id, to_id))
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path),
        )


# ---------------------------------------------------------------------------
# build_network_graph
# ---------------------------------------------------------------------------

def test_network_graph_contains_all_edges(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_C, NODE_A, NODE_C, NODE_A)
    G = build_network_graph(db)
    assert G.number_of_edges() == 2


def test_network_graph_time_range_filters(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, ts=PAST)
    _insert(db, TRACE_2, NODE_C, NODE_A, NODE_C, NODE_A, ts=NOW)
    G = build_network_graph(db, start_ts=NOW - 60)
    assert G.number_of_edges() == 1


def test_network_graph_nodes_have_color(db):
    upsert_node(db, NODE_A, long_name="Alpha", short_name="A", role="CLIENT")
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_network_graph(db)
    node_a_name = f"!{NODE_A:08x}"
    assert node_a_name in G.nodes
    assert "color" in G.nodes[node_a_name]


def test_network_graph_edges_have_snr_label(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=5.25)
    G = build_network_graph(db)
    edge_data = list(G.edges(data=True))
    assert any("5.25" in str(d.get("label", "")) for _, _, d in edge_data)


def test_network_graph_missing_snr_shows_question_mark(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=None)
    G = build_network_graph(db)
    edge_data = list(G.edges(data=True))
    assert any("?" in str(d.get("label", "")) for _, _, d in edge_data)


# ---------------------------------------------------------------------------
# build_simple_network_graph
# ---------------------------------------------------------------------------

def test_simple_network_graph_deduplicates(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_simple_network_graph(db)
    assert G.number_of_edges() == 1


# ---------------------------------------------------------------------------
# build_trace_graph
# ---------------------------------------------------------------------------

def test_trace_graph_isolates_single_trace(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_C, NODE_A, NODE_C, NODE_A)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edges = list(G.edges())
    node_a_str = f"!{NODE_A:08x}"
    node_b_str = f"!{NODE_B:08x}"
    assert (node_a_str, node_b_str) in edges
    assert not any(f"!{NODE_C:08x}" in str(e) for e in edges)


def test_trace_graph_returns_none_for_unknown_trace(db):
    G = build_trace_graph(db, trace_id=9999)
    assert G is None


def test_trace_graph_fast_path_edge_style(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, is_fast_path=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge_data = list(G.edges(data=True))
    assert any(d.get("style") == "bold" for _, _, d in edge_data)


def test_trace_graph_reply_edge_style(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_A, is_reply=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge_data = list(G.edges(data=True))
    assert any(d.get("style") == "dashed" for _, _, d in edge_data)


def test_trace_graph_highlights_from_to_nodes(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_trace_graph(db, trace_id=TRACE_1)
    node_a_str = f"!{NODE_A:08x}"
    node_b_str = f"!{NODE_B:08x}"
    assert G.nodes[node_a_str].get("fillcolor") is not None
    assert G.nodes[node_b_str].get("fillcolor") is not None


# ---------------------------------------------------------------------------
# build_node_graph
# ---------------------------------------------------------------------------

def test_node_graph_includes_links_as_start(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_C, NODE_B, NODE_C, NODE_B)
    G = build_node_graph(db, node_id=NODE_A)
    assert G.number_of_edges() == 1


def test_node_graph_includes_links_as_end(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_node_graph(db, node_id=NODE_B)
    assert G.number_of_edges() == 1


def test_node_graph_time_range(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, ts=PAST)
    _insert(db, TRACE_2, NODE_A, NODE_C, NODE_A, NODE_C, ts=NOW)
    G = build_node_graph(db, node_id=NODE_A, start_ts=NOW - 60)
    assert G.number_of_edges() == 1
