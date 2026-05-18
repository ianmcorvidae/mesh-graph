from __future__ import annotations

import sqlite3
from typing import Optional

import networkx as nx

from mesh_graph.db import get_links_for_network, get_links_for_node, get_links_for_trace, get_node_attrs


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


def build_network_graph(
    conn: sqlite3.Connection,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for row in get_links_for_network(conn, start_ts=start_ts, end_ts=end_ts):
        color = _edge_color(row["trace_id"])
        e0 = _node_str(row["link_start"])
        e1 = _node_str(row["link_end"])
        style = "dashed" if row["is_reply"] else "solid"
        G.add_edge(e0, e1, color=color, fontcolor=color, style=style, label=_snr_label(row["snr"]))
    nx.set_node_attributes(G, get_node_attrs(conn))
    return G


def build_simple_network_graph(
    conn: sqlite3.Connection,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> nx.DiGraph:
    G = nx.DiGraph()
    seen = set()
    for row in get_links_for_network(conn, start_ts=start_ts, end_ts=end_ts):
        e0 = _node_str(row["link_start"])
        e1 = _node_str(row["link_end"])
        if (e0, e1) in seen:
            continue
        seen.add((e0, e1))
        ls = row["link_start"]
        le = row["link_end"]
        if isinstance(ls, int) and isinstance(le, int):
            color = _edge_color(ls ^ le)
        elif isinstance(le, int):
            color = _edge_color(le)
        elif isinstance(ls, int):
            color = _edge_color(ls)
        else:
            color = _edge_color(0)
        G.add_edge(e0, e1, color=color, fontcolor=color, style="solid")
    nx.set_node_attributes(G, get_node_attrs(conn))
    return G


def build_trace_graph(conn: sqlite3.Connection, trace_id: int):
    rows = get_links_for_trace(conn, trace_id=trace_id)
    if not rows:
        return None

    import pydot

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

    return G


def build_node_graph(
    conn: sqlite3.Connection,
    node_id: int,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for row in get_links_for_node(conn, node_id=node_id, start_ts=start_ts, end_ts=end_ts):
        color = _edge_color(row["trace_id"])
        e0 = _node_str(row["link_start"])
        e1 = _node_str(row["link_end"])
        style = "dashed" if row["is_reply"] else "solid"
        G.add_edge(e0, e1, color=color, fontcolor=color, style=style, label=_snr_label(row["snr"]))
    nx.set_node_attributes(G, get_node_attrs(conn))
    node_str = _node_str(node_id)
    if not G.has_node(node_str):
        G.add_node(node_str)
    nx.set_node_attributes(G, {node_str: {"style": "filled", "fillcolor": "#ffffa9"}})
    return G
