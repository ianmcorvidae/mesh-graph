import sqlite3
import time

import pytest

from mesh_graph.db import init_db, upsert_node
from mesh_graph.graph.builder import (
    build_node_graph,
    build_simple_network_graph,
    build_trace_graph,
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


def _insert(
    db,
    trace_id,
    from_id,
    to_id,
    link_start,
    link_end,
    snr=None,
    is_reply=0,
    is_fast_path=0,
    ts=None,
):
    ts = ts or NOW
    with db:
        db.execute(
            "INSERT OR IGNORE INTO traceroute (trace_id, from_id, to_id) VALUES (?,?,?)",
            (trace_id, from_id, to_id),
        )
        db.execute(
            "INSERT OR IGNORE INTO traceroute_link "
            "(trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (trace_id, from_id, to_id, ts, link_start, link_end, snr, is_reply, is_fast_path),
        )


# ---------------------------------------------------------------------------
# build_simple_network_graph
# ---------------------------------------------------------------------------


def test_simple_network_graph_deduplicates(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_simple_network_graph(db)
    assert G.number_of_edges() == 1


def test_simple_network_graph_time_range_filters(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, ts=PAST)
    _insert(db, TRACE_2, NODE_C, NODE_A, NODE_C, NODE_A, ts=NOW)
    G = build_simple_network_graph(db, start_ts=NOW - 60)
    assert G.number_of_edges() == 1


def test_simple_network_graph_nodes_use_compact_labels_with_white_fill(db):
    upsert_node(db, NODE_A, long_name="Alpha Long Name", short_name="ALPHA", role="ROUTER")
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_simple_network_graph(db)
    node_a_name = f"!{NODE_A:08x}"
    assert G.nodes[node_a_name]["label"] == f"{node_a_name}\nALPHA"
    assert G.nodes[node_a_name]["style"] == "filled"
    assert G.nodes[node_a_name]["fillcolor"] == "#ffffff"


def test_simple_network_graph_keeps_one_edge_per_direction(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_1, NODE_B, NODE_A, NODE_B, NODE_A)
    G = build_simple_network_graph(db)
    assert G.number_of_edges() == 2
    assert G.has_edge(f"!{NODE_A:08x}", f"!{NODE_B:08x}")
    assert G.has_edge(f"!{NODE_B:08x}", f"!{NODE_A:08x}")


def test_simple_network_graph_uses_xor_color_and_snr_range_label(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=2.0)
    _insert(db, TRACE_2, NODE_A, NODE_B, NODE_A, NODE_B, snr=6.0)
    G = build_simple_network_graph(db)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    expected_color = f"#{((NODE_A ^ NODE_B) & 0xFFFFFF):06x}"
    assert edge["color"] == expected_color
    assert edge["label"] == "2.0..6.0dB"


def test_simple_network_graph_can_disable_snr_labels(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=2.0)
    _insert(db, TRACE_2, NODE_A, NODE_B, NODE_A, NODE_B, snr=6.0)
    G = build_simple_network_graph(db, include_snr_labels=False)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    assert "label" not in edge


def test_simple_network_graph_collapses_bidirectional_edges_when_snr_labels_off(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=2.0)
    _insert(db, TRACE_2, NODE_B, NODE_A, NODE_B, NODE_A, snr=6.0)
    G = build_simple_network_graph(db, include_snr_labels=False)
    assert G.number_of_edges() == 1
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    assert edge["dir"] == "both"


def test_simple_network_graph_can_suppress_unknown_nodes(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, "a038868c-698282d0-1")
    G = build_simple_network_graph(db, include_unknown_nodes=False)
    assert "a038868c-698282d0-1" not in G.nodes


def test_simple_network_graph_can_include_unknown_nodes(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, "a038868c-698282d0-1")
    G = build_simple_network_graph(db, include_unknown_nodes=True)
    assert "a038868c-698282d0-1" in G.nodes


def test_simple_network_graph_core_only_mode_hides_clients(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="CLIENT")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="CLIENT")
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_simple_network_graph(db, include_clients=False)
    assert G.number_of_edges() == 0


def test_simple_network_graph_core_only_does_not_infer_links_via_clients(db):
    node_d = 0xAAAA0004
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="ROUTER")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="CLIENT")
    upsert_node(db, node_d, long_name="D", short_name="D", role="CLIENT_BASE")
    _insert(db, TRACE_1, NODE_A, node_d, NODE_A, NODE_B)
    _insert(db, TRACE_1, NODE_A, node_d, NODE_B, node_d)
    G = build_simple_network_graph(db, include_clients=False)
    assert not G.has_edge(f"!{NODE_A:08x}", f"!{node_d:08x}")


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


def test_trace_graph_returns_sparse_graph_when_trace_has_no_links(db):
    with db:
        db.execute(
            "INSERT INTO traceroute (trace_id, from_id, to_id, first_seen_ts) VALUES (?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NOW),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    assert G is not None
    assert G.number_of_edges() == 0
    assert set(G.nodes()) == {f"!{NODE_A:08x}", f"!{NODE_B:08x}"}


def test_trace_graph_fast_path_edge_style(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, is_fast_path=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    assert edge["style"] == "solid"
    assert edge["penwidth"] == 2
    assert edge["weight"] == 20


def test_trace_graph_reply_edge_style(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_A, is_reply=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    assert edge["style"] == "dashed"
    assert edge["dir"] == "back"
    assert edge["label"] == "?dB"
    assert edge["color"] == "#888888"


def test_trace_graph_keeps_outbound_and_reply_edges_separate(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=4.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_A, snr=7.0, is_reply=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    outbound = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    reply = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][1]
    assert outbound["style"] == "solid"
    assert outbound["label"] == "4.0dB"
    assert reply["style"] == "dashed"
    assert reply["label"] == "7.0dB"
    assert G.number_of_edges() == 2


def test_trace_graph_direction_filters_edges_by_is_reply(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=4.0, is_reply=0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_A, snr=7.0, is_reply=1)

    both = build_trace_graph(db, trace_id=TRACE_1, direction="both")
    out = build_trace_graph(db, trace_id=TRACE_1, direction="out")
    incoming = build_trace_graph(db, trace_id=TRACE_1, direction="in")

    assert both.number_of_edges() == 2
    assert out.number_of_edges() == 1
    assert incoming.number_of_edges() == 1
    out_edge = next(iter(out.edges(data=True)))[2]
    in_edge = next(iter(incoming.edges(data=True)))[2]
    assert out_edge["style"] == "solid"
    assert in_edge["style"] == "dashed"


def test_trace_graph_uses_snr_gradient_colors(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=-20.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_C, snr=0.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_C, NODE_A, snr=10.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_C, snr=None)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge_by_label = {d["label"]: d for _, _, d in G.edges(data=True)}
    assert edge_by_label["-20.0dB"]["color"] == "#cc2200"
    assert edge_by_label["0.0dB"]["color"] == "#cccc00"
    assert edge_by_label["10.0dB"]["color"] == "#00cc44"
    assert edge_by_label["?dB"]["color"] == "#888888"
    assert edge_by_label["0.0dB"]["fontcolor"] == "#cccc00"


def test_trace_graph_marks_ingested_reply_fast_path_with_penwidth(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_A, snr=1.0, is_reply=1, is_fast_path=1)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_C, NODE_B, snr=2.0, is_reply=1, is_fast_path=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    reply_edges = [d for _, _, d in G.edges(data=True) if d.get("dir") == "back"]
    assert len(reply_edges) == 2
    assert all(d.get("style") == "dashed" for d in reply_edges)
    assert all(d.get("penwidth") == 2 for d in reply_edges)
    assert all(d.get("weight") == 20 for d in reply_edges)


def test_trace_graph_fallback_marks_unique_chain_from_destination(db):
    node_d = 0xAAAA0004
    node_e = 0xAAAA0005
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_A, snr=1.0, is_reply=1)  # B->A
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, node_d, snr=2.0, is_reply=1)  # A->D
    _insert(
        db, TRACE_1, NODE_A, NODE_B, node_e, NODE_A, snr=3.0, is_reply=1
    )  # E->A (incoming to A only)
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge_ab = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    edge_da = G[f"!{node_d:08x}"][f"!{NODE_A:08x}"][0]
    edge_ae = G[f"!{NODE_A:08x}"][f"!{node_e:08x}"][0]
    assert edge_ab.get("penwidth") == 2
    assert edge_ab.get("weight") == 20
    assert edge_da.get("penwidth") == 2
    assert edge_da.get("weight") == 20
    assert "penwidth" not in edge_ae


def test_trace_graph_snr_weight_bins(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=-11.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, NODE_C, snr=-7.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_C, NODE_A, snr=-2.0)
    node_d = 0xAAAA0004
    node_e = 0xAAAA0005
    node_f = 0xAAAA0006
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, node_d, snr=3.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, node_d, node_e, snr=7.0)
    _insert(db, TRACE_1, NODE_A, NODE_B, node_e, node_f, snr=12.0)

    G = build_trace_graph(db, trace_id=TRACE_1)
    edge_by_label = {d["label"]: d for _, _, d in G.edges(data=True)}
    assert edge_by_label["-11.0dB"]["weight"] == 1
    assert edge_by_label["-7.0dB"]["weight"] == 2
    assert edge_by_label["-2.0dB"]["weight"] == 3
    assert edge_by_label["3.0dB"]["weight"] == 4
    assert edge_by_label["7.0dB"]["weight"] == 5
    assert edge_by_label["12.0dB"]["weight"] == 6


def test_trace_graph_does_not_mark_ambiguous_back_reply_path(db):
    node_d = 0xAAAA0004
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=1.0, is_reply=1)  # B->A
    _insert(db, TRACE_1, NODE_A, NODE_B, node_d, NODE_B, snr=2.0, is_reply=1)  # B->D
    G = build_trace_graph(db, trace_id=TRACE_1)
    reply_edges = [d for _, _, d in G.edges(data=True) if d.get("style") == "dashed"]
    assert len(reply_edges) == 2
    assert all("penwidth" not in d for d in reply_edges)


def test_trace_graph_highlights_from_to_nodes(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    G = build_trace_graph(db, trace_id=TRACE_1)
    node_a_str = f"!{NODE_A:08x}"
    node_b_str = f"!{NODE_B:08x}"
    assert G.nodes[node_a_str].get("fillcolor") is not None
    assert G.nodes[node_b_str].get("fillcolor") is not None
    assert G.graph["rank_source_node"] == node_a_str
    assert G.graph["rank_sink_node"] == node_b_str


def test_trace_graph_adds_relative_uplink_time_to_matching_edges(db):
    uplink_1 = 0xAAAA0099
    uplink_2 = 0xAAAA00AB
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, uplink_1)
    _insert(db, TRACE_1, NODE_A, NODE_B, uplink_1, uplink_2)
    _insert(db, TRACE_1, NODE_A, NODE_B, uplink_2, NODE_B)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 0, NODE_A),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_2, NOW + 4, 0, uplink_1),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    assert G is not None
    edge_1 = G[f"!{NODE_A:08x}"][f"!{uplink_1:08x}"][0]
    edge_2 = G[f"!{uplink_1:08x}"][f"!{uplink_2:08x}"][0]
    assert "Uplink: +0s@0" in edge_1["label"]
    assert "Uplink: +4s@0" in edge_2["label"]
    assert "Uplink (node):" not in G.nodes[f"!{uplink_1:08x}"]["label"]
    assert "Uplink (node):" not in G.nodes[f"!{uplink_2:08x}"]["label"]
    assert "label" not in G.graph


def test_trace_graph_uplink_edge_label_shows_hop_limit(db):
    uplink_1 = 0xAAAA0099
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, uplink_1)
    _insert(db, TRACE_1, NODE_A, NODE_B, uplink_1, NODE_B)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink "
            "(trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node, hop_start, hop_limit) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 0, NODE_A, 7, 5),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_A:08x}"][f"!{uplink_1:08x}"][0]
    assert "Uplink: +0s@5" in edge["label"]


def test_trace_graph_uplink_edge_label_reply_only(db):
    uplink_1 = 0xAAAA0099
    _insert(db, TRACE_1, NODE_A, NODE_B, uplink_1, NODE_B, is_reply=1)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 1, NODE_B),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    label = G[f"!{NODE_B:08x}"][f"!{uplink_1:08x}"][0]["label"]
    assert "Uplink (reply): +0s@0" in label
    assert "Uplink: +" not in label.split("Uplink (reply):")[0]


def test_trace_graph_uplink_edge_label_reply_only_ingestion_orientation(db):
    uplink_1 = 0xAAAA0099
    # Ingested reply links are commonly stored as prev_node -> uplink.
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, uplink_1, is_reply=1)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 1, NODE_B),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    label = G[f"!{uplink_1:08x}"][f"!{NODE_B:08x}"][0]["label"]
    assert "Uplink (reply): +0s@0" in label


def test_trace_graph_uplink_edge_label_both_directions(db):
    uplink_1 = 0xAAAA0099
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, uplink_1)
    _insert(db, TRACE_1, NODE_A, NODE_B, uplink_1, NODE_B, is_reply=1)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink "
            "(trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node, hop_limit) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 0, NODE_A, 4),
        )
        db.execute(
            "INSERT INTO traceroute_uplink "
            "(trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node, hop_limit) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW + 2, 1, NODE_B, 3),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    outbound = G[f"!{NODE_A:08x}"][f"!{uplink_1:08x}"][0]["label"]
    inbound = G[f"!{NODE_B:08x}"][f"!{uplink_1:08x}"][0]["label"]
    assert "Uplink: +0s@4" in outbound
    assert "Uplink (reply): +2s@3" in inbound


def test_trace_graph_uplink_node_fallback_label_outbound_origin(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, is_reply=0)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NODE_A, NOW, 0, NODE_A),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    label = G.nodes[f"!{NODE_A:08x}"]["label"]
    assert "Uplink (node): +0s@0" in label
    assert "Uplink (node reply):" not in label


def test_trace_graph_uplink_node_fallback_label_reply_destination(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, is_reply=1)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NODE_B, NOW, 1, NODE_B),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    label = G.nodes[f"!{NODE_B:08x}"]["label"]
    assert "Uplink (node reply): +0s@0" in label
    assert "Uplink (node):" not in label


def test_trace_graph_maps_non_endpoint_self_uplink_to_unique_incoming_edge(db):
    uplink_1 = 0xAAAA0099
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, uplink_1)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 0, uplink_1),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    node_label = G.nodes[f"!{uplink_1:08x}"]["label"]
    edge_label = G[f"!{NODE_A:08x}"][f"!{uplink_1:08x}"][0]["label"]
    assert "Uplink (node):" not in node_label
    assert "Uplink (node reply):" not in node_label
    assert "Uplink: +0s@0" in edge_label


def test_trace_graph_maps_non_endpoint_self_reply_uplink_to_unique_edge(db):
    uplink_1 = 0xAAAA0099
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, uplink_1, is_reply=1)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, uplink_1, NOW, 1, uplink_1),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    node_label = G.nodes[f"!{uplink_1:08x}"]["label"]
    edge_label = G[f"!{uplink_1:08x}"][f"!{NODE_B:08x}"][0]["label"]
    assert "Uplink (node):" not in node_label
    assert "Uplink (node reply):" not in node_label
    assert "Uplink (reply): +0s@0" in edge_label


def test_trace_graph_applies_uplink_line_style_to_endpoints(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NODE_A, NOW, 0, NODE_A),
        )
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NODE_B, NOW + 2, 1, NODE_A),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    source = G.nodes[f"!{NODE_A:08x}"]
    sink = G.nodes[f"!{NODE_B:08x}"]
    assert source["peripheries"] == 2
    assert sink["peripheries"] == 2


def test_trace_graph_direction_line_styles_skip_endpoints(db):
    node_d = 0xAAAA0004
    node_e = 0xAAAA0005
    node_f = 0xAAAA0006
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, node_d, is_reply=0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, node_d, is_reply=1)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, node_e, is_reply=0)
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_B, node_f, is_reply=1)
    G = build_trace_graph(db, trace_id=TRACE_1)
    assert G.nodes[f"!{NODE_A:08x}"]["fillcolor"] == "#ffa9a9"
    assert G.nodes[f"!{NODE_B:08x}"]["fillcolor"] == "#a9a9ff"
    assert G.nodes[f"!{NODE_A:08x}"]["style"] == "filled"
    assert G.nodes[f"!{NODE_B:08x}"]["style"] == "filled"

    both_dir = G.nodes[f"!{node_d:08x}"]
    out_only = G.nodes[f"!{node_e:08x}"]
    in_only = G.nodes[f"!{node_f:08x}"]
    assert both_dir["style"] == "filled,solid"
    assert both_dir["penwidth"] == 2.4
    assert out_only["style"] == "filled,solid"
    assert out_only["penwidth"] == 1.2
    assert in_only["style"] == "filled,dashed"
    assert in_only["penwidth"] == 1.2
    assert both_dir["fillcolor"] == "#ffffff"
    assert out_only["fillcolor"] == "#ffffff"
    assert in_only["fillcolor"] == "#ffffff"


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


def test_node_graph_direction_outbound_only(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_C, NODE_A, NODE_C, NODE_A)
    G = build_node_graph(db, node_id=NODE_A, direction="outbound")
    edges = set(G.edges())
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_C:08x}", f"!{NODE_A:08x}") not in edges


def test_node_graph_direction_inbound_only(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_C, NODE_A, NODE_C, NODE_A)
    G = build_node_graph(db, node_id=NODE_A, direction="inbound")
    edges = set(G.edges())
    assert (f"!{NODE_C:08x}", f"!{NODE_A:08x}") in edges
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") not in edges


def test_node_graph_depth_expands_multiple_hops(db):
    node_d = 0xAAAA0004
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_1, NODE_B, NODE_C, NODE_B, NODE_C)
    _insert(db, TRACE_1, NODE_C, node_d, NODE_C, node_d)
    depth1 = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=1)
    depth2 = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=2)
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") not in set(depth1.edges())
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") in set(depth2.edges())


def test_node_graph_collapses_duplicate_links_and_tracks_snr_range(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B, snr=1.0, is_reply=0)
    _insert(db, TRACE_2, NODE_A, NODE_B, NODE_A, NODE_B, snr=7.0, is_reply=1)
    G = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=1)
    assert G.number_of_edges() == 1
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    expected_color = f"#{((NODE_A ^ NODE_B) & 0xFFFFFF):06x}"
    assert edge["color"] == expected_color
    assert edge["style"] == "solid"
    assert edge["label"] == "1.0..7.0dB"


def test_node_graph_both_splits_overlap_nodes_and_keeps_directional_parts(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_B, NODE_A, NODE_B, NODE_A)
    G = build_node_graph(db, node_id=NODE_A, direction="both", depth=1)
    edges = set(G.edges())
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x} [out]") in edges
    assert (f"!{NODE_B:08x} [in]", f"!{NODE_A:08x}") in edges
    assert (f"!{NODE_B:08x} [out]", f"!{NODE_A:08x}") not in edges
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x} [in]") not in edges
    assert f"!{NODE_A:08x} [out]" not in G.nodes
    assert f"!{NODE_A:08x} [in]" not in G.nodes


def test_node_graph_network_keeps_combined_behavior(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_2, NODE_B, NODE_A, NODE_B, NODE_A)
    G = build_node_graph(db, node_id=NODE_A, direction="network", depth=1)
    edges = set(G.edges())
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_B:08x}", f"!{NODE_A:08x}") in edges
    assert not any("[out]" in n or "[in]" in n for n in G.nodes)


def test_node_graph_outbound_depth_excludes_back_edges(db):
    _insert(db, TRACE_1, NODE_A, NODE_B, NODE_A, NODE_B)
    _insert(db, TRACE_1, NODE_B, NODE_C, NODE_B, NODE_C)
    _insert(db, TRACE_2, NODE_C, NODE_B, NODE_C, NODE_B)  # back toward source
    G = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=3)
    edges = set(G.edges())
    assert (f"!{NODE_C:08x}", f"!{NODE_B:08x}") not in edges
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") in edges


def test_node_graph_inbound_depth_excludes_forward_edges(db):
    _insert(db, TRACE_1, NODE_C, NODE_B, NODE_C, NODE_B)
    _insert(db, TRACE_1, NODE_B, NODE_A, NODE_B, NODE_A)
    _insert(db, TRACE_2, NODE_B, NODE_C, NODE_B, NODE_C)  # away from source
    G = build_node_graph(db, node_id=NODE_A, direction="inbound", depth=3)
    edges = set(G.edges())
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") not in edges
    assert (f"!{NODE_C:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_B:08x}", f"!{NODE_A:08x}") in edges
