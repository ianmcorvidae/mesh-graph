from __future__ import annotations

import sqlite3
from typing import Optional

import networkx as nx

from mesh_graph.db import get_links_for_network, get_links_for_trace, get_node_attrs
from mesh_graph.observability import traced_span


def _node_str(val) -> str:
    if isinstance(val, int):
        return f"!{val:08x}"
    return str(val)


def _edge_color(trace_id) -> str:
    n = int(trace_id) if not isinstance(trace_id, int) else trace_id
    r = (n & 0xFF0000) >> 16
    g = (n & 0x00FF00) >> 8
    b = n & 0x0000FF
    return f"#{r:02x}{g:02x}{b:02x}"


def _snr_label(snr) -> str:
    return f"{'?' if snr is None else snr}dB"


def _snr_range_label(snrs: list[float]) -> str:
    if not snrs:
        return "?dB"
    lo = min(snrs)
    hi = max(snrs)
    if lo == hi:
        return f"{lo}dB"
    return f"{lo}..{hi}dB"


def _xor_link_color(link_start, link_end) -> str:
    if isinstance(link_start, int) and isinstance(link_end, int):
        return _edge_color(link_start ^ link_end)
    if isinstance(link_end, int):
        return _edge_color(link_end)
    if isinstance(link_start, int):
        return _edge_color(link_start)
    return _edge_color(0)


def _build_depth_map(
    outgoing: dict[object, list[sqlite3.Row]],
    incoming: dict[object, list[sqlite3.Row]],
    node_id: int,
    depth: int,
    traversal: str,
) -> dict[object, int]:
    node_depth: dict[object, int] = {node_id: 0}
    frontier: set[object] = {node_id}
    for current_depth in range(depth):
        next_frontier: set[object] = set()
        for current in frontier:
            if traversal in {"outbound", "both"}:
                for row in outgoing.get(current, []):
                    neighbor = row["link_end"]
                    if neighbor not in node_depth:
                        node_depth[neighbor] = current_depth + 1
                        next_frontier.add(neighbor)
            if traversal in {"inbound", "both"}:
                for row in incoming.get(current, []):
                    neighbor = row["link_start"]
                    if neighbor not in node_depth:
                        node_depth[neighbor] = current_depth + 1
                        next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    return node_depth


def _add_collapsed_edges(
    G: nx.DiGraph,
    rows: list[sqlite3.Row],
    node_mapper,
) -> None:
    edge_snrs: dict[tuple[str, str], list[float]] = {}
    edge_colors: dict[tuple[str, str], str] = {}
    for row in rows:
        start_name = node_mapper(row["link_start"])
        end_name = node_mapper(row["link_end"])
        key = (start_name, end_name)
        if key not in edge_snrs:
            edge_snrs[key] = []
        if row["snr"] is not None:
            edge_snrs[key].append(float(row["snr"]))
        if key not in edge_colors:
            edge_colors[key] = _xor_link_color(row["link_start"], row["link_end"])
    for (start_name, end_name), snrs in edge_snrs.items():
        color = edge_colors[(start_name, end_name)]
        G.add_edge(
            start_name,
            end_name,
            color=color,
            fontcolor=color,
            style="solid",
            label=_snr_range_label(snrs),
        )


def build_simple_network_graph(
    conn: sqlite3.Connection,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    include_snr_labels: bool = True,
    include_unknown_nodes: bool = True,
    include_clients: bool = True,
) -> nx.DiGraph:
    with traced_span(
        "graph.build_simple_network_graph",
        warn_ms=2000,
        attributes={"graph.start_ts": start_ts, "graph.end_ts": end_ts},
    ) as span:
        G = nx.DiGraph()
        edge_snrs: dict[tuple[str, str], list[float]] = {}
        rows = get_links_for_network(conn, start_ts=start_ts, end_ts=end_ts)
        span.set_attribute("graph.input_rows", len(rows))
        role_rows = conn.execute("SELECT nodenum, role FROM nodes").fetchall()
        role_by_node = {row["nodenum"]: row["role"] for row in role_rows}
        core_roles = {"ROUTER", "ROUTER_LATE", "CLIENT_BASE"}

        def is_core_node(val: object) -> bool:
            return isinstance(val, int) and role_by_node.get(val) in core_roles

        effective_include_unknown = include_clients and include_unknown_nodes

        def is_visible_node(val: object) -> bool:
            if is_core_node(val):
                return True
            if not include_clients:
                return False
            if not isinstance(val, int):
                return effective_include_unknown
            return True

        filtered_rows: list[sqlite3.Row] = []
        for row in rows:
            start = row["link_start"]
            end = row["link_end"]
            if not effective_include_unknown and (not isinstance(start, int) or not isinstance(end, int)):
                continue
            filtered_rows.append(row)

        for row in filtered_rows:
            start = row["link_start"]
            end = row["link_end"]
            if not (is_visible_node(start) and is_visible_node(end)):
                continue
            e0 = _node_str(start)
            e1 = _node_str(end)
            key = (e0, e1)
            if key not in edge_snrs:
                edge_snrs[key] = []
            if row["snr"] is not None:
                edge_snrs[key].append(float(row["snr"]))

            if not G.has_edge(e0, e1):
                color = _xor_link_color(start, end)
                G.add_edge(e0, e1, color=color, fontcolor=color, style="solid")

        if include_snr_labels:
            for (e0, e1), snrs in edge_snrs.items():
                G[e0][e1]["label"] = _snr_range_label(snrs)

        nx.set_node_attributes(G, get_node_attrs(conn, label_mode="compact"))
        nx.set_node_attributes(G, {n: {"style": "filled", "fillcolor": "#ffffff"} for n in G.nodes})
        span.set_attribute("graph.node_count", len(G.nodes))
        span.set_attribute("graph.edge_count", len(G.edges))
        return G


def build_trace_graph(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
    approx_ts: Optional[int] = None,
):
    rows = get_links_for_trace(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )
    if not rows:
        return None

    G = nx.MultiDiGraph()
    from_id = None
    to_id = None

    for row in rows:
        color = _edge_color(row["trace_id"])
        e0 = _node_str(row["link_start"])
        e1 = _node_str(row["link_end"])
        if row["is_fast_path"]:
            style = "bold"
        elif row["is_reply"]:
            style = "dashed"
        else:
            style = "solid"
        G.add_edge(e0, e1, color=color, fontcolor=color, style=style, label=_snr_label(row["snr"]))
        from_id = row["from_id"]
        to_id = row["to_id"]

    from_str = _node_str(from_id)
    to_str = _node_str(to_id)
    for n in (from_str, to_str):
        if not G.has_node(n):
            G.add_node(n)

    nx.set_node_attributes(G, get_node_attrs(conn))
    nx.set_node_attributes(G, {
        from_str: {"style": "filled", "fillcolor": "#ffa9a9"},
        to_str: {"style": "filled", "fillcolor": "#a9a9ff"},
    })
    G.graph["rank_source_node"] = from_str
    G.graph["rank_sink_node"] = to_str

    return G


def build_node_graph(
    conn: sqlite3.Connection,
    node_id: int,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    direction: str = "both",
    depth: int = 1,
) -> nx.DiGraph:
    if direction not in {"inbound", "outbound", "both", "network"}:
        raise ValueError(f"Unsupported direction '{direction}'")
    if depth < 1:
        raise ValueError("depth must be >= 1")

    rows = get_links_for_network(conn, start_ts=start_ts, end_ts=end_ts)
    outgoing: dict[object, list[sqlite3.Row]] = {}
    incoming: dict[object, list[sqlite3.Row]] = {}
    for row in rows:
        start = row["link_start"]
        end = row["link_end"]
        outgoing.setdefault(start, []).append(row)
        incoming.setdefault(end, []).append(row)

    G = nx.DiGraph()

    def filter_rows_for(node_depth: dict[object, int], mode: str) -> list[sqlite3.Row]:
        selected: list[sqlite3.Row] = []
        for row in rows:
            start = row["link_start"]
            end = row["link_end"]
            if start not in node_depth or end not in node_depth:
                continue
            start_depth = node_depth[start]
            end_depth = node_depth[end]
            if mode == "outbound" and not (start_depth < end_depth):
                continue
            if mode == "inbound" and not (start_depth > end_depth):
                continue
            selected.append(row)
        return selected

    all_attrs = get_node_attrs(conn)
    if direction == "both":
        out_depth = _build_depth_map(outgoing, incoming, node_id=node_id, depth=depth, traversal="outbound")
        in_depth = _build_depth_map(outgoing, incoming, node_id=node_id, depth=depth, traversal="inbound")
        overlap = (set(out_depth.keys()) & set(in_depth.keys())) - {node_id}

        def map_out(val) -> str:
            base = _node_str(val)
            return f"{base} [out]" if val in overlap else base

        def map_in(val) -> str:
            base = _node_str(val)
            return f"{base} [in]" if val in overlap else base

        _add_collapsed_edges(G, filter_rows_for(out_depth, "outbound"), map_out)
        _add_collapsed_edges(G, filter_rows_for(in_depth, "inbound"), map_in)

        extra_attrs: dict[str, dict] = {}
        for n in G.nodes:
            if n.endswith(" [out]"):
                base = n[:-6]
                attrs = dict(all_attrs.get(base, {"label": base, "color": _edge_color(0)}))
                attrs["label"] = f"{attrs.get('label', base)}"
                extra_attrs[n] = attrs
            elif n.endswith(" [in]"):
                base = n[:-5]
                attrs = dict(all_attrs.get(base, {"label": base, "color": _edge_color(0)}))
                attrs["label"] = f"{attrs.get('label', base)}"
                extra_attrs[n] = attrs
        if extra_attrs:
            nx.set_node_attributes(G, extra_attrs)
    else:
        traversal = "both" if direction == "network" else direction
        node_depth = _build_depth_map(outgoing, incoming, node_id=node_id, depth=depth, traversal=traversal)
        mode = "both" if direction == "network" else direction
        _add_collapsed_edges(G, filter_rows_for(node_depth, mode), _node_str)

    nx.set_node_attributes(G, all_attrs)
    node_str = _node_str(node_id)
    target_nodes = [node_str]
    for target in target_nodes:
        if not G.has_node(target):
            G.add_node(target)
    nx.set_node_attributes(G, {target: {"style": "filled", "fillcolor": "#ffffa9"} for target in target_nodes})
    return G
