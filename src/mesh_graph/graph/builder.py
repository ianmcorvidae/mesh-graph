from __future__ import annotations

import sqlite3
from typing import Literal, Optional

import networkx as nx

from mesh_graph.db import (
    get_links_for_network,
    get_links_for_trace,
    get_node_attrs,
    get_trace_for_selector,
    get_uplinks_for_trace,
)
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

    red = (0xCC, 0x22, 0x00)
    yellow = (0xCC, 0xCC, 0x00)
    green = (0x00, 0xCC, 0x44)

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


def _snr_weight(snr: Optional[float]) -> int:
    if snr is None or snr <= -10:
        return 1
    if snr < -5:
        return 2
    if snr < 0:
        return 3
    if snr < 5:
        return 4
    if snr < 10:
        return 5
    return 6


def _xor_link_color(link_start, link_end) -> str:
    if isinstance(link_start, int) and isinstance(link_end, int):
        return _edge_color(link_start ^ link_end)
    if isinstance(link_end, int):
        return _edge_color(link_end)
    if isinstance(link_start, int):
        return _edge_color(link_start)
    return _edge_color(0)


def _uplink_labels(
    uplink_rows: list[sqlite3.Row],
    trace_rows: list[sqlite3.Row],
    *,
    source_node: str,
    destination_node: str,
) -> tuple[dict[tuple[str, str, bool], str], dict[str, list[str]]]:
    """
    Returns:
      1) {(edge_start, edge_end, is_reply): "label"} for regular edge labels.
      2) {node_name: [label_line, ...]} for fallback node labels in the two
         endpoint-only edge-less cases: source outbound and destination reply.

    Outbound uplink observations are attached to the rendered edge
    prev_node -> uplink_id.
    Reply uplink observations are attached to the rendered edge
    prev_node -> uplink_id (with dir=back on reply edges).
    """
    if not uplink_rows:
        return {}, {}
    base_ts = int(uplink_rows[0]["ts"])
    incoming_by_node_dir: dict[tuple[str, bool], set[str]] = {}
    outgoing_by_node_dir: dict[tuple[str, bool], set[str]] = {}
    for row in trace_rows:
        if row["is_reply"]:
            edge_start = _node_str(row["link_end"])
            edge_end = _node_str(row["link_start"])
            is_reply_edge = True
        else:
            edge_start = _node_str(row["link_start"])
            edge_end = _node_str(row["link_end"])
            is_reply_edge = False
        incoming_by_node_dir.setdefault((edge_end, is_reply_edge), set()).add(edge_start)
        outgoing_by_node_dir.setdefault((edge_start, is_reply_edge), set()).add(edge_end)

    buckets: dict[tuple[str, str, bool], dict[str, list[str]]] = {}
    node_buckets: dict[str, dict[str, list[str]]] = {}
    for row in uplink_rows:
        uplink_name = _node_str(row["uplink_id"])
        prev_name = _node_str(row["prev_node"])
        is_reply = bool(row["is_reply"])
        rel_secs = int(row["ts"]) - base_ts
        hop_limit = row["hop_limit"]
        hop_str = str(hop_limit if hop_limit is not None else 0)
        part = f"+{rel_secs}s@{hop_str}"
        is_endpoint_fallback = prev_name == uplink_name and (
            (not is_reply and uplink_name == source_node)
            or (is_reply and uplink_name == destination_node)
        )
        if is_endpoint_fallback:
            node_bucket = node_buckets.setdefault(uplink_name, {"out": [], "reply": []})
            if is_reply:
                node_bucket["reply"].append(part)
            else:
                node_bucket["out"].append(part)
            continue
        if prev_name == uplink_name:
            if is_reply:
                outgoing = outgoing_by_node_dir.get((uplink_name, True), set())
                if len(outgoing) == 1:
                    # For reply rendering, keying the opposite orientation lets
                    # reverse-edge fallback attach to the drawn edge.
                    prev_name = next(iter(outgoing))
            else:
                incoming = incoming_by_node_dir.get((uplink_name, False), set())
                if len(incoming) == 1:
                    prev_name = next(iter(incoming))
        edge_key = (prev_name, uplink_name, is_reply)
        b = buckets.setdefault(edge_key, {"out": [], "reply": []})
        if is_reply:
            b["reply"].append(part)
        else:
            b["out"].append(part)
    edge_result: dict[tuple[str, str, bool], str] = {}
    for edge_key, b in buckets.items():
        lines: list[str] = []
        if b["out"]:
            lines.append("Uplink: " + ",".join(b["out"]))
        if b["reply"]:
            lines.append("Uplink (reply): " + ",".join(b["reply"]))
        edge_result[edge_key] = "\n".join(lines)
    node_result: dict[str, list[str]] = {}
    for node_name, b in node_buckets.items():
        lines: list[str] = []
        if b["out"]:
            lines.append("Uplink (node): " + ",".join(b["out"]))
        if b["reply"]:
            lines.append("Uplink (node reply): " + ",".join(b["reply"]))
        node_result[node_name] = lines
    return edge_result, node_result


def _trace_node_style_attrs(
    rows: list[sqlite3.Row], uplink_rows: list[sqlite3.Row], from_str: str, to_str: str
) -> dict[str, dict[str, object]]:
    outbound_nodes: set[str] = set()
    inbound_nodes: set[str] = set()
    for row in rows:
        start = _node_str(row["link_start"])
        end = _node_str(row["link_end"])
        if row["is_reply"]:
            inbound_nodes.update((start, end))
        else:
            outbound_nodes.update((start, end))

    uplink_nodes = {_node_str(row["uplink_id"]) for row in uplink_rows}
    endpoint_nodes = {from_str, to_str}
    attrs: dict[str, dict[str, object]] = {}
    for node_name in outbound_nodes | inbound_nodes | endpoint_nodes | uplink_nodes:
        in_outbound = node_name in outbound_nodes
        in_inbound = node_name in inbound_nodes
        entry: dict[str, object] = {
            "style": "filled",
            "fillcolor": "#ffffff",
            "penwidth": 1.2,
            "peripheries": 1,
        }
        if node_name not in endpoint_nodes:
            if in_outbound and in_inbound:
                entry["style"] = "filled,solid"
                entry["penwidth"] = 2.4
            elif in_outbound:
                entry["style"] = "filled,solid"
            elif in_inbound:
                entry["style"] = "filled,dashed"
        if node_name in uplink_nodes:
            entry["peripheries"] = 2
        attrs[node_name] = entry
    return attrs


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


def _fallback_fast_back_reply_edges(
    rows: list[sqlite3.Row], destination_node: str
) -> set[tuple[str, str]]:
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if not row["is_reply"]:
            continue
        # Fallback traces stored reply links from the trace destination outward.
        edge = (_node_str(row["link_start"]), _node_str(row["link_end"]))
        outgoing.setdefault(edge[0], []).append(edge)
    marked_stored, _ = _walk_single_outgoing_chain(destination_node, outgoing)
    # Reply links are rendered with inverted endpoints and dir=back.
    return {(end, start) for start, end in marked_stored}


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
            if not effective_include_unknown and (
                not isinstance(start, int) or not isinstance(end, int)
            ):
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
    direction: Literal["both", "out", "in"] = "both",
):
    trace = get_trace_for_selector(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )
    if trace is None:
        return None

    rows = get_links_for_trace(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )
    uplink_rows = get_uplinks_for_trace(
        conn,
        trace_id=trace_id,
        from_id=from_id,
        to_id=to_id,
        approx_ts=approx_ts,
    )

    G = nx.MultiDiGraph()
    trace_from_id = trace["from_id"]
    trace_to_id = trace["to_id"]
    destination_node = _node_str(trace_to_id)
    from_str = _node_str(trace_from_id)
    to_str = _node_str(trace_to_id)
    if direction == "out":
        filtered_uplink_rows = [row for row in uplink_rows if not row["is_reply"]]
    elif direction == "in":
        filtered_uplink_rows = [row for row in uplink_rows if row["is_reply"]]
    else:
        filtered_uplink_rows = uplink_rows
    filtered_rows = [
        row
        for row in rows
        if (direction != "out" or not row["is_reply"]) and (direction != "in" or row["is_reply"])
    ]
    uplink_edge_labels, uplink_node_labels = _uplink_labels(
        filtered_uplink_rows,
        filtered_rows,
        source_node=from_str,
        destination_node=to_str,
    )
    fast_back_edges = {
        (_node_str(row["link_end"]), _node_str(row["link_start"]))
        for row in rows
        if row["is_reply"] and row["is_fast_path"]
    }
    if not fast_back_edges:
        fast_back_edges = _fallback_fast_back_reply_edges(rows, destination_node)

    for row in filtered_rows:
        if direction == "out" and row["is_reply"]:
            continue
        if direction == "in" and not row["is_reply"]:
            continue
        if row["is_reply"]:
            e0 = _node_str(row["link_end"])
            e1 = _node_str(row["link_start"])
        else:
            e0 = _node_str(row["link_start"])
            e1 = _node_str(row["link_end"])
        color = _snr_color(row["snr"])
        edge_is_fast_path = (
            row["is_fast_path"] if not row["is_reply"] else (e0, e1) in fast_back_edges
        )
        attrs = {
            "color": color,
            "fontcolor": color,
            "style": "dashed" if row["is_reply"] else "solid",
            "label": _snr_label(row["snr"]),
            "weight": _snr_weight(row["snr"]),
        }
        is_reply_edge = bool(row["is_reply"])
        uplink_label = uplink_edge_labels.get((e0, e1, is_reply_edge))
        if uplink_label is None and is_reply_edge:
            # Some traces store reply link endpoints opposite to test fixtures.
            # Accept either orientation for matching reply uplink labels.
            uplink_label = uplink_edge_labels.get((e1, e0, True))
        if uplink_label:
            attrs["label"] = f"{attrs['label']}\n{uplink_label}"
        if row["is_reply"]:
            attrs["dir"] = "back"
        if edge_is_fast_path:
            attrs["penwidth"] = 2
            attrs["weight"] = 20
        G.add_edge(e0, e1, **attrs)
    for n in (from_str, to_str):
        if not G.has_node(n):
            G.add_node(n)

    nx.set_node_attributes(G, get_node_attrs(conn))
    if uplink_node_labels:
        fallback_attrs: dict[str, dict[str, str]] = {}
        for node_name, lines in uplink_node_labels.items():
            if not G.has_node(node_name):
                continue
            existing_label = str(G.nodes[node_name].get("label", node_name))
            fallback_attrs[node_name] = {"label": existing_label + "\n" + "\n".join(lines)}
        if fallback_attrs:
            nx.set_node_attributes(G, fallback_attrs)
    node_style_attrs = _trace_node_style_attrs(
        rows, filtered_uplink_rows, from_str=from_str, to_str=to_str
    )
    if node_style_attrs:
        nx.set_node_attributes(G, node_style_attrs)
    nx.set_node_attributes(
        G,
        {
            from_str: {"style": "filled", "fillcolor": "#ffa9a9"},
            to_str: {"style": "filled", "fillcolor": "#a9a9ff"},
        },
    )
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
        out_depth = _build_depth_map(
            outgoing, incoming, node_id=node_id, depth=depth, traversal="outbound"
        )
        in_depth = _build_depth_map(
            outgoing, incoming, node_id=node_id, depth=depth, traversal="inbound"
        )
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
        node_depth = _build_depth_map(
            outgoing, incoming, node_id=node_id, depth=depth, traversal=traversal
        )
        mode = "both" if direction == "network" else direction
        _add_collapsed_edges(G, filter_rows_for(node_depth, mode), _node_str)

    nx.set_node_attributes(G, all_attrs)
    node_str = _node_str(node_id)
    target_nodes = [node_str]
    for target in target_nodes:
        if not G.has_node(target):
            G.add_node(target)
    nx.set_node_attributes(
        G, {target: {"style": "filled", "fillcolor": "#ffffa9"} for target in target_nodes}
    )
    return G
