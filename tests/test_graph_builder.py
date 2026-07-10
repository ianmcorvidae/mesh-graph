import time

from mesh_graph.db import upsert_node
from mesh_graph.graph.builder import (
    build_node_graph,
    build_simple_network_graph,
    build_trace_graph,
)

from .conftest import insert_link

NOW = int(time.time())
PAST = NOW - 7200

# Node IDs used across tests
NODE_A = 0xAAAA0001
NODE_B = 0xAAAA0002
NODE_C = 0xAAAA0003
TRACE_1 = 1001
TRACE_2 = 1002


# ---------------------------------------------------------------------------
# build_simple_network_graph
# ---------------------------------------------------------------------------


def test_simple_network_graph_deduplicates(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    G = build_simple_network_graph(db)
    assert G.number_of_edges() == 1


def test_simple_network_graph_time_range_filters(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        ts=PAST,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_C,
        to_id=NODE_A,
        link_start=NODE_C,
        link_end=NODE_A,
        ts=NOW,
    )
    G = build_simple_network_graph(db, start_ts=NOW - 60)
    assert G.number_of_edges() == 1


def test_simple_network_graph_nodes_use_compact_labels_with_white_fill(db):
    upsert_node(db, NODE_A, long_name="Alpha Long Name", short_name="ALPHA", role="ROUTER")
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    G = build_simple_network_graph(db)
    node_a_name = f"!{NODE_A:08x}"
    assert G.nodes[node_a_name]["label"] == f"{node_a_name}\nALPHA"
    assert G.nodes[node_a_name]["style"] == "filled"
    assert G.nodes[node_a_name]["fillcolor"] == "#ffffff"


def test_simple_network_graph_keeps_one_edge_per_direction(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_A, link_start=NODE_B, link_end=NODE_A
    )
    G = build_simple_network_graph(db)
    assert G.number_of_edges() == 2
    assert G.has_edge(f"!{NODE_A:08x}", f"!{NODE_B:08x}")
    assert G.has_edge(f"!{NODE_B:08x}", f"!{NODE_A:08x}")


def test_simple_network_graph_uses_xor_color_and_snr_range_label(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=2.0,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=6.0,
    )
    G = build_simple_network_graph(db)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    expected_color = f"#{((NODE_A ^ NODE_B) & 0xFFFFFF):06x}"
    assert edge["color"] == expected_color
    assert edge["label"] == "2.0..6.0dB"


def test_simple_network_graph_can_disable_snr_labels(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=2.0,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=6.0,
    )
    G = build_simple_network_graph(db, include_snr_labels=False)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    assert "label" not in edge


def test_simple_network_graph_collapses_bidirectional_edges_when_snr_labels_off(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=2.0,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_B,
        to_id=NODE_A,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=6.0,
    )
    G = build_simple_network_graph(db, include_snr_labels=False)
    assert G.number_of_edges() == 1
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    assert edge["dir"] == "both"


def test_simple_network_graph_can_suppress_unknown_nodes(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end="a038868c-698282d0-1",
    )
    G = build_simple_network_graph(db, include_unknown_nodes=False)
    assert "a038868c-698282d0-1" not in G.nodes


def test_simple_network_graph_can_include_unknown_nodes(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end="a038868c-698282d0-1",
    )
    G = build_simple_network_graph(db, include_unknown_nodes=True)
    assert "a038868c-698282d0-1" in G.nodes


def test_simple_network_graph_core_only_mode_hides_clients(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="CLIENT")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="CLIENT")
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    G = build_simple_network_graph(db, include_clients=False)
    assert G.number_of_edges() == 0


def test_simple_network_graph_core_only_does_not_infer_links_via_clients(db):
    node_d = 0xAAAA0004
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="ROUTER")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="CLIENT")
    upsert_node(db, node_d, long_name="D", short_name="D", role="CLIENT_BASE")
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=node_d, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=node_d, link_start=NODE_B, link_end=node_d
    )
    G = build_simple_network_graph(db, include_clients=False)
    assert not G.has_edge(f"!{NODE_A:08x}", f"!{node_d:08x}")


# ---------------------------------------------------------------------------
# build_trace_graph
# ---------------------------------------------------------------------------


def test_trace_graph_isolates_single_trace(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_C, to_id=NODE_A, link_start=NODE_C, link_end=NODE_A
    )
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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        is_fast_path=1,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    assert edge["style"] == "solid"
    assert edge["penwidth"] == 2
    assert edge["weight"] == 20


def test_trace_graph_reply_edge_style(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        is_reply=1,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    assert edge["style"] == "dashed"
    assert edge["dir"] == "back"
    assert edge["label"] == "?dB"
    assert edge["color"] == "#888888"


def test_trace_graph_keeps_outbound_and_reply_edges_separate(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=4.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=7.0,
        is_reply=1,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    outbound = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][0]
    reply = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"][1]
    assert outbound["style"] == "solid"
    assert outbound["label"] == "4.0dB"
    assert reply["style"] == "dashed"
    assert reply["label"] == "7.0dB"
    assert G.number_of_edges() == 2


def test_trace_graph_direction_filters_edges_by_is_reply(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=4.0,
        is_reply=0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=7.0,
        is_reply=1,
    )

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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=-20.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_C,
        snr=0.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_C,
        link_end=NODE_A,
        snr=10.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_C,
        snr=None,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge_by_label = {d["label"]: d for _, _, d in G.edges(data=True)}
    assert edge_by_label["-20.0dB"]["color"] == "#cc2200"
    assert edge_by_label["0.0dB"]["color"] == "#cccc00"
    assert edge_by_label["10.0dB"]["color"] == "#00cc44"
    assert edge_by_label["?dB"]["color"] == "#888888"
    assert edge_by_label["0.0dB"]["fontcolor"] == "#cccc00"


def test_trace_graph_marks_full_route_terminal_outbound_edge(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=3.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_C,
        snr=5.0,
        route_len=8,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_B:08x}"][f"!{NODE_C:08x}"][0]
    assert edge["style"] == "dotted"
    assert edge["color"] == "#ee5500"
    assert edge["fontcolor"] == "#ee5500"
    assert edge["label"] == "5.0dB (>=8 hops)"


def test_trace_graph_marks_full_route_terminal_reply_edge(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_C,
        snr=7.0,
        is_reply=1,
        route_len=8,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    edge = G[f"!{NODE_C:08x}"][f"!{NODE_B:08x}"][0]
    assert edge["style"] == "dotted"
    assert edge["color"] == "#ee5500"
    assert edge["label"] == "7.0dB (>=8 hops)"
    assert edge["dir"] == "back"


def test_trace_graph_marks_ingested_reply_fast_path_with_penwidth(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=1.0,
        is_reply=1,
        is_fast_path=1,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_C,
        link_end=NODE_B,
        snr=2.0,
        is_reply=1,
        is_fast_path=1,
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    reply_edges = [d for _, _, d in G.edges(data=True) if d.get("dir") == "back"]
    assert len(reply_edges) == 2
    assert all(d.get("style") == "dashed" for d in reply_edges)
    assert all(d.get("penwidth") == 2 for d in reply_edges)
    assert all(d.get("weight") == 20 for d in reply_edges)


def test_trace_graph_fallback_marks_unique_chain_from_destination(db):
    node_d = 0xAAAA0004
    node_e = 0xAAAA0005
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_A,
        snr=1.0,
        is_reply=1,
    )  # B->A
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=node_d,
        snr=2.0,
        is_reply=1,
    )  # A->D
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=node_e,
        link_end=NODE_A,
        snr=3.0,
        is_reply=1,
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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=-11.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=NODE_C,
        snr=-7.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_C,
        link_end=NODE_A,
        snr=-2.0,
    )
    node_d = 0xAAAA0004
    node_e = 0xAAAA0005
    node_f = 0xAAAA0006
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=node_d,
        snr=3.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=node_d,
        link_end=node_e,
        snr=7.0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=node_e,
        link_end=node_f,
        snr=12.0,
    )

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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=1.0,
        is_reply=1,
    )  # B->A
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=node_d,
        link_end=NODE_B,
        snr=2.0,
        is_reply=1,
    )  # B->D
    G = build_trace_graph(db, trace_id=TRACE_1)
    reply_edges = [d for _, _, d in G.edges(data=True) if d.get("style") == "dashed"]
    assert len(reply_edges) == 2
    assert all("penwidth" not in d for d in reply_edges)


def test_trace_graph_highlights_from_to_nodes(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
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
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=uplink_1
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=uplink_1, link_end=uplink_2
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=uplink_2, link_end=NODE_B
    )
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
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=uplink_1
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=uplink_1, link_end=NODE_B
    )
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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=uplink_1,
        link_end=NODE_B,
        is_reply=1,
    )
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink (trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node) "
            "VALUES (?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NODE_B, NOW, 1, uplink_1),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    label = G[f"!{NODE_B:08x}"][f"!{uplink_1:08x}"][0]["label"]
    assert "Uplink (reply): +0s@0" in label
    assert "Uplink: +" not in label.split("Uplink (reply):")[0]


def test_trace_graph_uplink_edge_label_reply_only_ingestion_orientation(db):
    uplink_1 = 0xAAAA0099
    # Ingested reply links are commonly stored as prev_node -> uplink.
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=uplink_1,
        is_reply=1,
    )
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
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=uplink_1
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=uplink_1,
        link_end=NODE_B,
        is_reply=1,
    )
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
            (TRACE_1, NODE_A, NODE_B, NODE_B, NOW + 2, 1, uplink_1, 3),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    outbound = G[f"!{NODE_A:08x}"][f"!{uplink_1:08x}"][0]["label"]
    inbound = G[f"!{NODE_B:08x}"][f"!{uplink_1:08x}"][0]["label"]
    assert "Uplink: +0s@4" in outbound
    assert "Uplink (reply): +2s@3" in inbound


def test_trace_graph_uplink_edge_label_self_reply_stays_on_destination_node(db):
    neighbor_1 = 0xAAAA00AA
    neighbor_2 = 0xAAAA00AB
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=neighbor_1,
        link_end=NODE_B,
        is_reply=1,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=neighbor_2,
        link_end=NODE_B,
        is_reply=1,
    )
    with db:
        db.execute(
            "INSERT INTO traceroute_uplink "
            "(trace_id, from_id, to_id, uplink_id, ts, is_reply, prev_node, hop_limit) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (TRACE_1, NODE_A, NODE_B, NODE_B, NOW, 1, NODE_B, 5),
        )
    G = build_trace_graph(db, trace_id=TRACE_1)
    node_label = G.nodes[f"!{NODE_B:08x}"]["label"]
    edge_1 = G[f"!{NODE_B:08x}"][f"!{neighbor_1:08x}"][0]["label"]
    edge_2 = G[f"!{NODE_B:08x}"][f"!{neighbor_2:08x}"][0]["label"]
    assert "Uplink (node reply): +0s@5" in node_label
    assert "Uplink (reply):" not in edge_1
    assert "Uplink (reply):" not in edge_2


def test_trace_graph_uplink_node_fallback_label_outbound_origin(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        is_reply=0,
    )
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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        is_reply=1,
    )
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
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=uplink_1
    )
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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=uplink_1,
        is_reply=1,
    )
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
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
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
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=node_d,
        is_reply=0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=node_d,
        is_reply=1,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=node_e,
        is_reply=0,
    )
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_B,
        link_end=node_f,
        is_reply=1,
    )
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
# build_trace_graph — community detection
# ---------------------------------------------------------------------------


def test_trace_graph_no_community_when_resolution_none(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_C, link_start=NODE_B, link_end=NODE_C
    )
    G = build_trace_graph(db, trace_id=TRACE_1)
    for _, data in G.nodes(data=True):
        assert "community_id" not in data
    assert "community_labels" not in G.graph


def test_trace_graph_community_assigns_ids_and_labels(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_C, link_start=NODE_B, link_end=NODE_C
    )
    G = build_trace_graph(db, trace_id=TRACE_1, resolution=1.0)
    assert G is not None
    for _, data in G.nodes(data=True):
        assert "community_id" in data
        assert isinstance(data["community_id"], int)
    assert "community_labels" in G.graph
    labels = G.graph["community_labels"]
    assert isinstance(labels, dict)
    for cid, label in labels.items():
        assert "nodes)" in label


def test_trace_graph_community_hub_uses_long_name(db):
    upsert_node(db, NODE_A, long_name="Alpha Long", short_name="ALPHA", role="ROUTER")
    upsert_node(db, NODE_B, long_name="Beta Long", short_name="BETA", role="ROUTER")
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    G = build_trace_graph(db, trace_id=TRACE_1, resolution=1.0)
    assert G is not None
    labels = G.graph["community_labels"]
    for label in labels.values():
        assert "Alpha Long" in label or "Beta Long" in label


def test_trace_graph_community_single_edge(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    G = build_trace_graph(db, trace_id=TRACE_1, resolution=1.0)
    assert G is not None
    assert all("community_id" in d for _, d in G.nodes(data=True))
    assert len(G.graph["community_labels"]) == 1
    assert "(2 nodes)" in list(G.graph["community_labels"].values())[0]


def test_trace_graph_community_with_custom_resolution(db):
    node_d = 0xAAAA0004
    node_e = 0xAAAA0005
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=node_d
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=node_d, link_end=node_e
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=node_e, link_end=NODE_B
    )
    G = build_trace_graph(db, trace_id=TRACE_1, resolution=0.5)
    assert G is not None
    communities = set()
    for _, d in G.nodes(data=True):
        cid = d.get("community_id")
        if cid is not None:
            communities.add(cid)
    assert len(communities) >= 1


def test_trace_graph_community_fallback_on_zero_resolution(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_C, link_start=NODE_B, link_end=NODE_C
    )
    G = build_trace_graph(db, trace_id=TRACE_1, resolution=0.0)
    assert G is not None
    for _, d in G.nodes(data=True):
        assert "community_id" in d


# ---------------------------------------------------------------------------
# build_node_graph
# ---------------------------------------------------------------------------


def test_node_graph_includes_links_as_start(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_C, to_id=NODE_B, link_start=NODE_C, link_end=NODE_B
    )
    G = build_node_graph(db, node_id=NODE_A)
    assert G.number_of_edges() == 1


def test_node_graph_includes_links_as_end(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    G = build_node_graph(db, node_id=NODE_B)
    assert G.number_of_edges() == 1


def test_node_graph_time_range(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        ts=PAST,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_C,
        link_start=NODE_A,
        link_end=NODE_C,
        ts=NOW,
    )
    G = build_node_graph(db, node_id=NODE_A, start_ts=NOW - 60)
    assert G.number_of_edges() == 1


def test_node_graph_direction_outbound_only(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_C, to_id=NODE_A, link_start=NODE_C, link_end=NODE_A
    )
    G = build_node_graph(db, node_id=NODE_A, direction="outbound")
    edges = set(G.edges())
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_C:08x}", f"!{NODE_A:08x}") not in edges


def test_node_graph_direction_inbound_only(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_C, to_id=NODE_A, link_start=NODE_C, link_end=NODE_A
    )
    G = build_node_graph(db, node_id=NODE_A, direction="inbound")
    edges = set(G.edges())
    assert (f"!{NODE_C:08x}", f"!{NODE_A:08x}") in edges
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") not in edges


def test_node_graph_depth_expands_multiple_hops(db):
    node_d = 0xAAAA0004
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_C, link_start=NODE_B, link_end=NODE_C
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_C, to_id=node_d, link_start=NODE_C, link_end=node_d
    )
    depth1 = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=1)
    depth2 = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=2)
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") not in set(depth1.edges())
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") in set(depth2.edges())


def test_node_graph_collapses_duplicate_links_and_tracks_snr_range(db):
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=1.0,
        is_reply=0,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=7.0,
        is_reply=1,
    )
    G = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=1)
    assert G.number_of_edges() == 1
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    expected_color = f"#{((NODE_A ^ NODE_B) & 0xFFFFFF):06x}"
    assert edge["color"] == expected_color
    assert edge["style"] == "solid"
    assert edge["label"] == "1.0..7.0dB"


def test_node_graph_both_splits_overlap_nodes_and_keeps_directional_parts(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_B, to_id=NODE_A, link_start=NODE_B, link_end=NODE_A
    )
    G = build_node_graph(db, node_id=NODE_A, direction="both", depth=1)
    edges = set(G.edges())
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x} [out]") in edges
    assert (f"!{NODE_B:08x} [in]", f"!{NODE_A:08x}") in edges
    assert (f"!{NODE_B:08x} [out]", f"!{NODE_A:08x}") not in edges
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x} [in]") not in edges
    assert f"!{NODE_A:08x} [out]" not in G.nodes
    assert f"!{NODE_A:08x} [in]" not in G.nodes


def test_node_graph_network_keeps_combined_behavior(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_B, to_id=NODE_A, link_start=NODE_B, link_end=NODE_A
    )
    G = build_node_graph(db, node_id=NODE_A, direction="network", depth=1)
    edges = set(G.edges())
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_B:08x}", f"!{NODE_A:08x}") in edges
    assert not any("[out]" in n or "[in]" in n for n in G.nodes)


def test_node_graph_outbound_depth_excludes_back_edges(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_A, to_id=NODE_B, link_start=NODE_A, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_C, link_start=NODE_B, link_end=NODE_C
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_C, to_id=NODE_B, link_start=NODE_C, link_end=NODE_B
    )  # back toward source
    G = build_node_graph(db, node_id=NODE_A, direction="outbound", depth=3)
    edges = set(G.edges())
    assert (f"!{NODE_C:08x}", f"!{NODE_B:08x}") not in edges
    assert (f"!{NODE_A:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") in edges


def test_node_graph_inbound_depth_excludes_forward_edges(db):
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_C, to_id=NODE_B, link_start=NODE_C, link_end=NODE_B
    )
    insert_link(
        db, trace_id=TRACE_1, from_id=NODE_B, to_id=NODE_A, link_start=NODE_B, link_end=NODE_A
    )
    insert_link(
        db, trace_id=TRACE_2, from_id=NODE_B, to_id=NODE_C, link_start=NODE_B, link_end=NODE_C
    )  # away from source
    G = build_node_graph(db, node_id=NODE_A, direction="inbound", depth=3)
    edges = set(G.edges())
    assert (f"!{NODE_B:08x}", f"!{NODE_C:08x}") not in edges
    assert (f"!{NODE_C:08x}", f"!{NODE_B:08x}") in edges
    assert (f"!{NODE_B:08x}", f"!{NODE_A:08x}") in edges


# ---------------------------------------------------------------------------
# Overflow exclusion
# ---------------------------------------------------------------------------


def test_network_graph_excludes_overflow_rows(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="ROUTER")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="ROUTER")
    # Non-overflow link
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=5.0,
    )
    # Overflow link
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=2.0,
        route_len=8,
    )
    G = build_simple_network_graph(db)
    assert G.has_edge(f"!{NODE_A:08x}", f"!{NODE_B:08x}")
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    # Label should only reflect non-overflow SNR
    assert "2.0" not in edge.get("label", "")


def test_network_graph_drops_edge_when_all_overflow(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="ROUTER")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="ROUTER")
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=5.0,
        route_len=8,
    )
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=2.0,
        route_len=8,
    )
    G = build_simple_network_graph(db)
    assert not G.has_edge(f"!{NODE_A:08x}", f"!{NODE_B:08x}")


def test_node_graph_excludes_overflow_rows(db):
    upsert_node(db, NODE_A, long_name="A", short_name="A", role="ROUTER")
    upsert_node(db, NODE_B, long_name="B", short_name="B", role="ROUTER")
    # Non-overflow link
    insert_link(
        db,
        trace_id=TRACE_1,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=5.0,
    )
    # Overflow link
    insert_link(
        db,
        trace_id=TRACE_2,
        from_id=NODE_A,
        to_id=NODE_B,
        link_start=NODE_A,
        link_end=NODE_B,
        snr=2.0,
        route_len=8,
    )
    G = build_node_graph(db, node_id=NODE_A, depth=1)
    assert G.has_edge(f"!{NODE_A:08x}", f"!{NODE_B:08x}")
    edge = G[f"!{NODE_A:08x}"][f"!{NODE_B:08x}"]
    assert "2.0" not in edge.get("label", "")
