from __future__ import annotations

import sqlite3
from typing import Optional

import networkx as nx

from mesh_graph.db import get_links_for_network, get_links_for_trace, get_node_attrs, get_uplinks_for_trace
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


def _snr_color(snr: Optional[float]) -> str:
    if snr is None:
        return "#888888"

    red = (0xcc, 0x22, 0x00)
    yellow = (0xcc, 0xcc, 0x00)
    green = (0x00, 0xcc, 0x44)

    if snr <= -20:
        rgb = red
    elif snr < 0:
        t = (snr + 20.0) / 20.0
        rgb = tuple(round(a + (b - a) * t) for a, b in zip(red, yellow, strict=True))
    elif snr < 10:
        t = snr / 10.0
        rgb = tuple(round(a + (b - a) * t) for a, b in zip(yellow, green, strict=True))
    else:
        rgb = green
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


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


def _uplink_label_lines(uplink_rows: list[sqlite3.Row]) -> dict[str, list[str]]:
    """
    Returns {node_name: [label_line, ...]} where each line covers one direction.
    Outbound observations produce "Uplink: +Xs@H,..." and reply observations
    produce "Uplink (reply): +Xs@H,...".  Only the lines that have observations
    are included.  If there are only reply observations the outbound line is
    omitted entirely.
    """
    if not uplink_rows:
        return {}
    base_ts = int(uplink_rows[0]["ts"])
    # node_name -> {"out": [...], "reply": [...]}
    buckets: dict[str, dict[str, list[str]]] = {}
    for row in uplink_rows:
        node_name = _node_str(row["uplink_id"])
        b = buckets.setdefault(node_name, {"out": [], "reply": []})
        rel_secs = int(row["ts"]) - base_ts
        hop_limit = row["hop_limit"]
        hop_str = str(hop_limit if hop_limit is not None else 0)
        part = f"+{rel_secs}s@{hop_str}"
        if row["is_reply"]:
            b["reply"].append(part)
        else:
            b["out"].append(part)
    result: dict[str, list[str]] = {}
    for node_name, b in buckets.items():
        lines: list[str] = []
        if b["out"]:
            lines.append("Uplink: " + ",".join(b["out"]))
        if b["reply"]:
            lines.append("Uplink (reply): " + ",".join(b["reply"]))
        result[node_name] = lines
    return result


def _walk_single_outgoing_chain(
    start: str,
    outgoing: dict[str, list[tuple[str, str]]],
) -> tuple[set[tuple[str, str]], str]:
    marked: set[tuple[str, str]] = set()
    current = start
    visited = {start}
    while True:
        options = outgoing.get(current, [])
        if len(options) != 1:
            return marked, current
        edge = options[0]
        marked.add(edge)
        current = edge[1]
        if current in visited:
            return marked, current
        visited.add(current)


def _fallback_fast_back_reply_edges(rows: list[sqlite3.Row], destination_node: str) -> set[tuple[str, str]]:
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if not row["is_reply"]:
            continue
        # Reply links are rendered with inverted endpoints and dir=back.
        edge = (_node_str(row["link_end"]), _node_str(row["link_start"]))
        outgoing.setdefault(edge[0], []).append(edge)
    marked, _ = _walk_single_outgoing_chain(destination_node, outgoing)
    return marked


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
        else:
            for e0, e1 in list(G.edges()):
                if e0 == e1:
                    continue
                if not G.has_edge(e1, e0):
                    continue
                keep_e0, keep_e1 = sorted((e0, e1))
                drop_e0, drop_e1 = (e0, e1) if (e0, e1) != (keep_e0, keep_e1) else (e1, e0)
                if G.has_edge(drop_e0, drop_e1):
                    G.remove_edge(drop_e0, drop_e1)
                if G.has_edge(keep_e0, keep_e1):
                    G[keep_e0][keep_e1]["dir"] = "both"

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
    uplink_rows = get_uplinks_for_trace(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )

    G = nx.MultiDiGraph()
    from_id = None
    to_id = None
    destination_node = _node_str(rows[0]["to_id"])
    fast_back_edges = {
        (_node_str(row["link_end"]), _node_str(row["link_start"]))
        for row in rows
        if row["is_reply"] and row["is_fast_path"]
    }
    if not fast_back_edges:
        fast_back_edges = _fallback_fast_back_reply_edges(rows, destination_node)

    for row in rows:
        if row["is_reply"]:
            e0 = _node_str(row["link_end"])
            e1 = _node_str(row["link_start"])
        else:
            e0 = _node_str(row["link_start"])
            e1 = _node_str(row["link_end"])
        color = _snr_color(row["snr"])
        edge_is_fast_path = row["is_fast_path"] if not row["is_reply"] else (e0, e1) in fast_back_edges
        attrs = {
            "color": color,
            "fontcolor": color,
            "style": "dashed" if row["is_reply"] else "solid",
            "label": _snr_label(row["snr"]),
        }
        if row["is_reply"]:
            attrs["dir"] = "back"
        if edge_is_fast_path:
            attrs["penwidth"] = 2
            attrs["weight"] = 2
        G.add_edge(e0, e1, **attrs)
        from_id = row["from_id"]
        to_id = row["to_id"]

    from_str = _node_str(from_id)
    to_str = _node_str(to_id)
    for n in (from_str, to_str):
        if not G.has_node(n):
            G.add_node(n)

    nx.set_node_attributes(G, get_node_attrs(conn))
    uplink_label_lines = _uplink_label_lines(uplink_rows)
    if uplink_label_lines:
        uplink_attrs: dict[str, dict[str, str]] = {}
        for node_name, lines in uplink_label_lines.items():
            if not G.has_node(node_name):
                continue
            existing_label = str(G.nodes[node_name].get("label", node_name))
            uplink_attrs[node_name] = {"label": existing_label + "\n" + "\n".join(lines)}
        if uplink_attrs:
            nx.set_node_attributes(G, uplink_attrs)
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
