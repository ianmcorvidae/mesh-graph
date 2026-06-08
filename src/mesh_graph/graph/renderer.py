from __future__ import annotations

import networkx as nx
import pydot

from mesh_graph.observability import traced_span

_SUPPORTED = {"png", "svg"}


def _to_pydot(G: nx.Graph, *, layout_prog: str) -> pydot.Dot:
    pd = nx.nx_pydot.to_pydot(G)
    for key in ("label", "labelloc"):
        value = G.graph.get(key)
        if isinstance(value, str):
            pd.set(key, value)
    if layout_prog == "sfdp":
        pd.set("overlap", "prism")
        pd.set("sep", "+8")
        pd.set("esep", "+2")
        pd.set("outputorder", "edgesfirst")
        pd.set("K", "1.6")
        pd.set("repulsiveforce", "2")

    rank_source = G.graph.get("rank_source_node")
    if isinstance(rank_source, str) and G.has_node(rank_source):
        sub = pydot.Subgraph(graph_name="rank_source")
        sub.set("rank", "source")
        sub.add_node(pydot.Node(rank_source))
        pd.add_subgraph(sub)

    rank_sink = G.graph.get("rank_sink_node")
    if isinstance(rank_sink, str) and G.has_node(rank_sink):
        sub = pydot.Subgraph(graph_name="rank_sink")
        sub.set("rank", "sink")
        sub.add_node(pydot.Node(rank_sink))
        pd.add_subgraph(sub)

    community_nodes: dict[int, list[str]] = {}
    for pydot_node in pd.get_nodes():
        name = pydot_node.get_name()
        cid = G.nodes[name].get("community_id") if G.has_node(name) else None
        if cid is not None:
            community_nodes.setdefault(cid, []).append(name)

    if community_nodes:
        pd.set("compound", "true")
        _COMMUNITY_COLORS = [
            "#ff6b6b",
            "#4ecdc4",
            "#45b7d1",
            "#96ceb4",
            "#ffeaa7",
            "#dfe6e9",
            "#ff9ff3",
            "#54a0ff",
        ]
        community_labels = G.graph.get("community_labels", {})
        for cid, members in community_nodes.items():
            sub = pydot.Subgraph(graph_name=f"cluster_{cid}")
            sub.set("label", community_labels.get(cid, f"Community {cid}"))
            sub.set("style", "rounded")
            sub.set("color", _COMMUNITY_COLORS[cid % len(_COMMUNITY_COLORS)])
            for name in members:
                sub.add_node(pydot.Node(name))
            pd.add_subgraph(sub)

    return pd


def render(G: nx.Graph, format: str, *, layout_prog: str = "dot") -> bytes:
    fmt = format.lower()
    if fmt not in _SUPPORTED:
        raise ValueError(f"Unsupported format '{format}'. Use one of: {', '.join(_SUPPORTED)}")

    with traced_span(
        "renderer.to_pydot",
        warn_ms=1000,
        attributes={"graph.node_count": len(G.nodes), "graph.edge_count": len(G.edges)},
    ):
        pd = _to_pydot(G, layout_prog=layout_prog)

    with traced_span(
        f"renderer.graphviz.create_{fmt}", warn_ms=5000, attributes={"graphviz.prog": layout_prog}
    ):
        if fmt == "png":
            return pd.create_png(prog=layout_prog)
        return pd.create_svg(prog=layout_prog)
