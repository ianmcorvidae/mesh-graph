import sqlite3

import networkx as nx
import pytest

from mesh_graph.db import init_db
from mesh_graph.graph.builder import build_simple_network_graph
from mesh_graph.graph.renderer import _to_pydot, render


@pytest.fixture
def simple_graph(tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    with conn:
        conn.execute("INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (1, 1, 2)")
        conn.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, link_start, link_end, snr, is_reply, is_fast_path) "
            "VALUES (1, 1, 2, 1, 2, 5.0, 0, 0)"
        )
    G = build_simple_network_graph(conn)
    conn.close()
    return G


def test_png_output_has_magic_bytes(simple_graph):
    data = render(simple_graph, "png")
    assert data[:4] == b"\x89PNG"


def test_svg_output_contains_svg_tag(simple_graph):
    data = render(simple_graph, "svg")
    assert b"<svg" in data


def test_unknown_format_raises(simple_graph):
    with pytest.raises(ValueError):
        render(simple_graph, "gif")


def test_png_output_is_non_empty(simple_graph):
    data = render(simple_graph, "png")
    assert len(data) > 100


def test_svg_output_is_non_empty(simple_graph):
    data = render(simple_graph, "svg")
    assert len(data) > 100


def test_to_pydot_adds_rank_source_and_sink_subgraphs():
    G = nx.MultiDiGraph()
    G.add_edge("src", "mid")
    G.add_edge("mid", "dst")
    G.graph["rank_source_node"] = "src"
    G.graph["rank_sink_node"] = "dst"

    pd = _to_pydot(G, layout_prog="dot")
    dot = pd.to_string()

    assert "rank=source" in dot
    assert "rank=sink" in dot
    assert "src;" in dot
    assert "dst;" in dot


def test_to_pydot_with_community_clusters():
    G = nx.MultiDiGraph()
    G.add_edge("a", "b")
    G.add_edge("c", "d")
    G.nodes["a"]["community_id"] = 0
    G.nodes["b"]["community_id"] = 0
    G.nodes["c"]["community_id"] = 1
    G.nodes["d"]["community_id"] = 1
    G.graph["community_labels"] = {0: "HubA (2 nodes)", 1: "HubC (2 nodes)"}

    pd = _to_pydot(G, layout_prog="dot")
    dot = pd.to_string()

    assert "cluster_0" in dot
    assert "cluster_1" in dot
    assert "HubA (2 nodes)" in dot
    assert "HubC (2 nodes)" in dot
    assert "compound=true" in dot
    assert "rounded" in dot


def test_to_pydot_without_communities_no_clusters():
    G = nx.MultiDiGraph()
    G.add_edge("a", "b")
    G.add_edge("c", "d")

    pd = _to_pydot(G, layout_prog="dot")
    dot = pd.to_string()

    assert "cluster_" not in dot
    assert "compound" not in dot


def test_to_pydot_community_uses_distinct_colors():
    G = nx.MultiDiGraph()
    G.add_edge("a", "b")
    G.add_edge("c", "d")
    G.nodes["a"]["community_id"] = 0
    G.nodes["b"]["community_id"] = 0
    G.nodes["c"]["community_id"] = 1
    G.nodes["d"]["community_id"] = 1
    G.graph["community_labels"] = {0: "C0", 1: "C1"}

    pd = _to_pydot(G, layout_prog="dot")
    dot = pd.to_string()

    assert "cluster_" in dot

    for sub in pd.get_subgraphs():
        color = sub.get("color")
        assert color is not None
        assert str(color).startswith("#")
