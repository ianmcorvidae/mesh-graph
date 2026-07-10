from __future__ import annotations

import math
import sqlite3
from typing import Optional

from mesh_graph.db import (
    get_link_positions_for_trace,
    get_links_for_network,
    get_links_for_nodes,
    get_node,
)
from mesh_graph.graph.builder import _snr_color
from mesh_graph.observability import traced_span
from mesh_graph.utils import int_to_hex_color, node_id_format


def _pos_to_lonlat(pos) -> tuple[float, float]:
    """Convert raw Meshtastic lat_i/lon_i to degrees as (lon, lat)."""
    if pos is None:
        raise ValueError("pos is None")
    return (
        pos["longitude_i"] * 1e-7,
        pos["latitude_i"] * 1e-7,
    )


def _centroid(positions: list[tuple[float, float]]) -> tuple[float, float]:
    if not positions:
        return (0.0, 0.0)
    lon_sum = sum(p[0] for p in positions)
    lat_sum = sum(p[1] for p in positions)
    return (lon_sum / len(positions), lat_sum / len(positions))


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Bearing from A to B in degrees clockwise from north."""
    lon_a, lat_a = a
    lon_b, lat_b = b
    return math.degrees(math.atan2(lon_b - lon_a, lat_b - lat_a)) % 360


def _arrow_angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Rotation angle for a right-pointing ▶ glyph to align with the A→B bearing."""
    return (_bearing_deg(a, b) - 90) % 360


def _arrow_feature(lonlat: tuple[float, float], angle: float) -> dict:
    """A small arrow glyph feature used by the map symbol layer."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lonlat[0], lonlat[1]]},
        "properties": {"layer": "arrow", "arrow_angle": angle},
    }


def _current_node_positions(
    conn: sqlite3.Connection, node_ids: set[int]
) -> dict[int, tuple[float, float]]:
    """Return current lat/lon for nodes from nodes.position_id (fallback)."""
    if not node_ids:
        return {}
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT nodenum, position_id FROM nodes WHERE nodenum IN ({placeholders})",
        list(node_ids),
    ).fetchall()
    pos_ids = {r["nodenum"]: r["position_id"] for r in rows if r["position_id"]}
    if not pos_ids:
        return {}
    placeholders = ",".join("?" * len(pos_ids))
    pos_rows = conn.execute(
        f"SELECT id, latitude_i, longitude_i FROM positions WHERE id IN ({placeholders})",
        list(pos_ids.values()),
    ).fetchall()
    pos_by_id = {r["id"]: r for r in pos_rows}
    result: dict[int, tuple[float, float]] = {}
    for nid, pos_id in pos_ids.items():
        pos = pos_by_id.get(pos_id)
        if pos is not None:
            result[nid] = _pos_to_lonlat(pos)
    return result


def _node_to_feature(
    nodenum: int,
    lonlat: tuple[float, float],
    *,
    role: Optional[str] = None,
    long_name: Optional[str] = None,
    short_name: Optional[str] = None,
    has_position: bool = True,
    is_approximated: bool = False,
    is_center: bool = False,
    is_source: bool = False,
    is_dest: bool = False,
    is_fast_path: bool = False,
) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lonlat[0], lonlat[1]]},
        "properties": {
            "layer": "node",
            "nodenum": nodenum,
            "node_id": node_id_format(nodenum),
            "long_name": long_name or "",
            "short_name": short_name or "",
            "role": role or "CLIENT",
            "has_position": has_position,
            "is_approximated": is_approximated,
            "is_center": is_center,
            "is_source": is_source,
            "is_dest": is_dest,
            "is_fast_path": is_fast_path,
            "color": int_to_hex_color(nodenum),
        },
    }


def _edge_to_feature(
    start_lonlat: tuple[float, float],
    end_lonlat: tuple[float, float],
    props: dict,
) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [start_lonlat[0], start_lonlat[1]],
                [end_lonlat[0], end_lonlat[1]],
            ],
        },
        "properties": {"layer": "edge", **props},
    }


def _approximate_positions(
    node_ids: set[int],
    positioned: dict[int, tuple[float, float]],
    adjacency: dict[int, set[int]],
) -> dict[int, tuple[float, float]]:
    """Approximate lat/lon for positionless nodes based on positioned neighbors.

    Iteratively resolves nodes whose neighbors are already resolved, falling
    back to the centroid of all visible positioned nodes for isolated nodes.
    """
    resolved = dict(positioned)
    unresolved = node_ids - set(positioned)
    if not unresolved:
        return resolved

    changed = True
    while changed and unresolved:
        changed = False
        newly_resolved: dict[int, tuple[float, float]] = {}
        for nid in list(unresolved):
            neighbors = adjacency.get(nid, set())
            resolved_neighbors = [resolved[n] for n in neighbors if n in resolved]
            if not resolved_neighbors:
                continue
            lat_sum = sum(p[1] for p in resolved_neighbors)
            lon_sum = sum(p[0] for p in resolved_neighbors)
            lat = lat_sum / len(resolved_neighbors)
            lon = lon_sum / len(resolved_neighbors)
            if len(resolved_neighbors) == 1:
                # Deterministic ~50m offset based on nodenum to avoid overlap.
                offset_deg = 0.0005
                angle = (nid % 360) * (math.pi / 180)
                lat += offset_deg * math.cos(angle)
                lon += offset_deg * math.sin(angle)
            newly_resolved[nid] = (lon, lat)
            changed = True
        for nid, lonlat in newly_resolved.items():
            resolved[nid] = lonlat
            unresolved.discard(nid)

    if unresolved:
        fallback = _centroid(list(resolved.values()))
        for nid in unresolved:
            resolved[nid] = fallback

    return resolved


def _node_info(conn: sqlite3.Connection, nodenum: int) -> sqlite3.Row:
    row = get_node(conn, nodenum)
    if row is None:

        class _DummyRow:
            def __getitem__(self, key):
                return {"long_name": "", "short_name": "", "role": "CLIENT"}.get(key)

            def __contains__(self, key):
                return key in {"long_name", "short_name", "role"}

        return _DummyRow()
    return row


def build_network_geojson(
    conn: sqlite3.Connection,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    include_clients: bool = False,
    include_unknown: bool = False,
    direction: str = "both",
) -> dict:
    """Build a GeoJSON FeatureCollection for the network view."""
    with traced_span(
        "geo.build_network_geojson",
        warn_ms=2000,
        attributes={"start_ts": start_ts, "end_ts": end_ts, "direction": direction},
    ) as span:
        rows = get_links_for_network(conn, start_ts=start_ts, end_ts=end_ts)

        # Direction filtering
        filtered_rows: list[sqlite3.Row] = []
        for row in rows:
            if direction == "outbound" and row["is_reply"]:
                continue
            if direction == "inbound" and not row["is_reply"]:
                continue
            filtered_rows.append(row)

        # Collect integer node IDs and build adjacency for approximation
        node_ids: set[int] = set()
        adjacency: dict[int, set[int]] = {}
        for row in filtered_rows:
            for val in (row["link_start"], row["link_end"]):
                if isinstance(val, int):
                    node_ids.add(val)
            start = row["link_start"]
            end = row["link_end"]
            if isinstance(start, int) and isinstance(end, int):
                adjacency.setdefault(start, set()).add(end)
                adjacency.setdefault(end, set()).add(start)

        # Determine most recent position per node from materialized link positions.
        # For each node, prefer the position from the link where it appears as
        # link_end (received-at), falling back to link_start.
        latest_pos_id: dict[int, int] = {}
        latest_ts: dict[int, int] = {}
        for row in filtered_rows:
            for endpoint, key in (("link_start", "start"), ("link_end", "end")):
                val = row[endpoint]
                if not isinstance(val, int):
                    continue
                pos_id_col = f"{endpoint}_position_id"
                pos_id = row[pos_id_col]
                if pos_id is None:
                    continue
                ts = row["ts"]
                # link_end gets a tiny priority bump when timestamps tie.
                tie_break = 1 if endpoint == "link_end" else 0
                sort_key = (ts, tie_break)
                if val not in latest_ts or sort_key > latest_ts[val]:
                    latest_ts[val] = sort_key
                    latest_pos_id[val] = pos_id

        positioned: dict[int, tuple[float, float]] = {}
        if latest_pos_id:
            placeholders = ",".join("?" * len(latest_pos_id))
            pos_rows = conn.execute(
                f"SELECT id, latitude_i, longitude_i FROM positions WHERE id IN ({placeholders})",
                list(latest_pos_id.values()),
            ).fetchall()
            pos_by_id = {r["id"]: r for r in pos_rows}
            for nid, pos_id in latest_pos_id.items():
                pos = pos_by_id.get(pos_id)
                if pos is not None:
                    positioned[nid] = _pos_to_lonlat(pos)

        # Fall back to current node positions for nodes without materialized link positions.
        for nid, lonlat in _current_node_positions(conn, node_ids - set(positioned)).items():
            positioned[nid] = lonlat

        resolved = _approximate_positions(node_ids, positioned, adjacency)

        # Determine which nodes participate in fast-path edges.
        fast_path_nodes: set[int] = set()
        for row in filtered_rows:
            if not row["is_fast_path"]:
                continue
            for val in (row["link_start"], row["link_end"]):
                if isinstance(val, int):
                    fast_path_nodes.add(val)

        # Build node features
        node_features: list[dict] = []
        node_info_cache: dict[int, sqlite3.Row] = {}
        for nid in sorted(node_ids):
            info = node_info_cache.get(nid)
            if info is None:
                info = _node_info(conn, nid)
                node_info_cache[nid] = info
            lonlat = resolved[nid]
            node_features.append(
                _node_to_feature(
                    nid,
                    lonlat,
                    role=info["role"],
                    long_name=info["long_name"],
                    short_name=info["short_name"],
                    has_position=nid in positioned,
                    is_approximated=nid not in positioned,
                    is_fast_path=nid in fast_path_nodes,
                )
            )

        # Build edge features, collapsed by unordered pair for integer-to-integer
        # edges, and separately for unknown hop edges.
        edge_groups: dict[tuple[int, int], dict] = {}
        unknown_groups: dict[tuple[object, object], dict] = {}
        for row in filtered_rows:
            start = row["link_start"]
            end = row["link_end"]
            is_unknown = not isinstance(start, int) or not isinstance(end, int)
            if is_unknown:
                key = (str(start), str(end))
                g = unknown_groups.get(key)
                if g is None:
                    g = {
                        "start": start,
                        "end": end,
                        "snr_values": [],
                        "is_reply": bool(row["is_reply"]),
                        "link_count": 0,
                    }
                    unknown_groups[key] = g
                if row["snr"] is not None:
                    g["snr_values"].append(float(row["snr"]))
                g["link_count"] += 1
            else:
                key = (min(start, end), max(start, end))
                g = edge_groups.get(key)
                if g is None:
                    g = {
                        "a": key[0],
                        "b": key[1],
                        "out_snr_values": [],
                        "in_snr_values": [],
                        "out_count": 0,
                        "in_count": 0,
                        "out_fast_path": False,
                        "in_fast_path": False,
                    }
                    edge_groups[key] = g
                is_reply = bool(row["is_reply"])
                if not is_reply:
                    if row["snr"] is not None:
                        g["out_snr_values"].append(float(row["snr"]))
                    g["out_count"] += 1
                    if row["is_fast_path"]:
                        g["out_fast_path"] = True
                else:
                    if row["snr"] is not None:
                        g["in_snr_values"].append(float(row["snr"]))
                    g["in_count"] += 1
                    if row["is_fast_path"]:
                        g["in_fast_path"] = True

        # Unknown hop placement: midpoint of bounding known nodes
        unknown_hop_positions: dict[str, tuple[float, float]] = {}
        if include_unknown:
            for key, g in unknown_groups.items():
                start, end = g["start"], g["end"]
                bounds: list[tuple[float, float]] = []
                for val in (start, end):
                    if isinstance(val, int) and val in positioned:
                        bounds.append(positioned[val])
                if bounds:
                    unknown_hop_positions[str(start) + "-" + str(end)] = _centroid(bounds)

        edge_features: list[dict] = []

        # Integer-to-integer collapsed edges
        for key, g in edge_groups.items():
            a, b = key
            if a not in resolved or b not in resolved:
                continue
            has_out = g["out_count"] > 0
            has_in = g["in_count"] > 0
            if has_out and has_in:
                direction = "both"
                snr_values = g["out_snr_values"] + g["in_snr_values"]
                link_count = g["out_count"] + g["in_count"]
                is_fast_path = g["out_fast_path"] or g["in_fast_path"]
            elif has_out:
                direction = "out"
                snr_values = g["out_snr_values"]
                link_count = g["out_count"]
                is_fast_path = g["out_fast_path"]
            else:
                direction = "in"
                snr_values = g["in_snr_values"]
                link_count = g["in_count"]
                is_fast_path = g["in_fast_path"]
            snr = sum(snr_values) / len(snr_values) if snr_values else None
            a_lonlat = resolved[a]
            b_lonlat = resolved[b]
            edge_features.append(
                _edge_to_feature(
                    a_lonlat,
                    b_lonlat,
                    {
                        "link_start": a,
                        "link_end": b,
                        "link_start_label": node_id_format(a),
                        "link_end_label": node_id_format(b),
                        "snr": snr,
                        "snr_color": _snr_color(snr),
                        "direction": direction,
                        "is_fast_path": is_fast_path,
                        "link_count": link_count,
                    },
                )
            )

        # Unknown hop edges (kept directed, no arrow)
        if include_unknown:
            for key, g in unknown_groups.items():
                start, end = g["start"], g["end"]
                start_lonlat = resolved.get(start) if isinstance(start, int) else None
                end_lonlat = resolved.get(end) if isinstance(end, int) else None
                if start_lonlat is None and isinstance(start, int):
                    continue
                if end_lonlat is None and isinstance(end, int):
                    continue
                if start_lonlat is None:
                    start_lonlat = unknown_hop_positions.get(str(start) + "-" + str(end))
                if end_lonlat is None:
                    end_lonlat = unknown_hop_positions.get(str(start) + "-" + str(end))
                if start_lonlat is None or end_lonlat is None:
                    continue
                snr_values = g["snr_values"]
                snr = sum(snr_values) / len(snr_values) if snr_values else None
                edge_features.append(
                    _edge_to_feature(
                        start_lonlat,
                        end_lonlat,
                        {
                            "link_start": start if isinstance(start, int) else str(start),
                            "link_end": end if isinstance(end, int) else str(end),
                            "link_start_label": node_id_format(start)
                            if isinstance(start, int)
                            else str(start),
                            "link_end_label": node_id_format(end)
                            if isinstance(end, int)
                            else str(end),
                            "snr": snr,
                            "snr_color": _snr_color(snr),
                            "direction": "out" if not g["is_reply"] else "in",
                            "is_fast_path": False,
                            "link_count": g["link_count"],
                            "is_unknown_hop": True,
                        },
                    )
                )

        span.set_attribute("geo.node_count", len(node_features))
        span.set_attribute("geo.edge_count", len(edge_features))
        return {
            "type": "FeatureCollection",
            "features": node_features + edge_features,
        }


def build_trace_geojson(
    conn: sqlite3.Connection,
    trace_id: int,
    from_id: Optional[int] = None,
    to_id: Optional[int] = None,
    approx_ts: Optional[int] = None,
    direction: str = "both",
) -> Optional[dict]:
    """Build a GeoJSON FeatureCollection for a single traceroute."""
    with traced_span(
        "geo.build_trace_geojson",
        warn_ms=2000,
        attributes={"trace_id": trace_id, "direction": direction},
    ) as span:
        rows = get_link_positions_for_trace(conn, trace_id=trace_id, from_id=from_id, to_id=to_id)
        if not rows:
            return None

        # Filter by direction only (fast_path is labeled client-side)
        filtered_rows = list(rows)
        if direction == "out":
            filtered_rows = [r for r in filtered_rows if not r["is_reply"]]
        elif direction == "in":
            filtered_rows = [r for r in filtered_rows if r["is_reply"]]

        # Determine trace source/dest
        first = filtered_rows[0]
        trace_from_id = first["from_id"]
        trace_to_id = first["to_id"]

        # Collect integer node IDs and build adjacency for approximation
        node_ids: set[int] = set()
        adjacency: dict[int, set[int]] = {}
        for row in filtered_rows:
            for val in (row["link_start"], row["link_end"]):
                if isinstance(val, int):
                    node_ids.add(val)
            start = row["link_start"]
            end = row["link_end"]
            if isinstance(start, int) and isinstance(end, int):
                adjacency.setdefault(start, set()).add(end)
                adjacency.setdefault(end, set()).add(start)

        # Collect node positions. Prefer link_end position when a node appears in
        # multiple links; fall back to link_start.
        node_pos_by_id: dict[int, tuple[float, float]] = {}
        for row in filtered_rows:
            for endpoint, prefix in (("link_start", "start"), ("link_end", "end")):
                val = row[endpoint]
                if not isinstance(val, int):
                    continue
                lat_col = f"{prefix}_lat_i"
                lon_col = f"{prefix}_lon_i"
                lat_i = row[lat_col]
                lon_i = row[lon_col]
                if lat_i is None or lon_i is None:
                    continue
                existing = node_pos_by_id.get(val)
                if existing is None or endpoint == "link_end":
                    node_pos_by_id[val] = _pos_to_lonlat(
                        {"latitude_i": lat_i, "longitude_i": lon_i}
                    )

        # Fall back to current node positions for nodes without materialized link positions.
        for nid, lonlat in _current_node_positions(conn, node_ids - set(node_pos_by_id)).items():
            node_pos_by_id[nid] = lonlat

        resolved = _approximate_positions(node_ids, node_pos_by_id, adjacency)

        # Nodes that touch a fast-path edge in this filtered view.
        fast_path_nodes: set[int] = set()
        for row in filtered_rows:
            if not row["is_fast_path"]:
                continue
            for val in (row["link_start"], row["link_end"]):
                if isinstance(val, int):
                    fast_path_nodes.add(val)

        node_features: list[dict] = []
        node_info_cache: dict[int, sqlite3.Row] = {}
        for nid in sorted(node_ids):
            info = node_info_cache.get(nid)
            if info is None:
                info = _node_info(conn, nid)
                node_info_cache[nid] = info
            lonlat = resolved[nid]
            node_features.append(
                _node_to_feature(
                    nid,
                    lonlat,
                    role=info["role"],
                    long_name=info["long_name"],
                    short_name=info["short_name"],
                    has_position=nid in node_pos_by_id,
                    is_approximated=nid not in node_pos_by_id,
                    is_source=nid == trace_from_id,
                    is_dest=nid == trace_to_id,
                    is_fast_path=nid in fast_path_nodes,
                )
            )

        # Collapse integer-to-integer edges by unordered pair
        edge_groups: dict[tuple[int, int], dict] = {}
        for row in filtered_rows:
            start = row["link_start"]
            end = row["link_end"]
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            key = (min(start, end), max(start, end))
            g = edge_groups.get(key)
            if g is None:
                g = {
                    "a": key[0],
                    "b": key[1],
                    "out_snr_values": [],
                    "in_snr_values": [],
                    "out_count": 0,
                    "in_count": 0,
                    "out_fast_path": False,
                    "in_fast_path": False,
                    "max_route_len": None,
                    "is_overflow": False,
                }
                edge_groups[key] = g
            is_reply = bool(row["is_reply"])
            snr = float(row["snr"]) if row["snr"] is not None else None
            route_len = row["route_len"]
            is_overflow = route_len is not None and route_len >= 8
            if is_overflow:
                g["is_overflow"] = True
            if route_len is not None and (
                g["max_route_len"] is None or route_len > g["max_route_len"]
            ):
                g["max_route_len"] = route_len
            if not is_reply:
                if snr is not None:
                    g["out_snr_values"].append(snr)
                g["out_count"] += 1
                if row["is_fast_path"]:
                    g["out_fast_path"] = True
            else:
                if snr is not None:
                    g["in_snr_values"].append(snr)
                g["in_count"] += 1
                if row["is_fast_path"]:
                    g["in_fast_path"] = True

        edge_features: list[dict] = []
        for key, g in edge_groups.items():
            a, b = key
            if a not in resolved or b not in resolved:
                continue
            has_out = g["out_count"] > 0
            has_in = g["in_count"] > 0
            if has_out and has_in:
                direction = "both"
                snr_values = g["out_snr_values"] + g["in_snr_values"]
                link_count = g["out_count"] + g["in_count"]
                is_fast_path = g["out_fast_path"] or g["in_fast_path"]
            elif has_out:
                direction = "out"
                snr_values = g["out_snr_values"]
                link_count = g["out_count"]
                is_fast_path = g["out_fast_path"]
            else:
                direction = "in"
                snr_values = g["in_snr_values"]
                link_count = g["in_count"]
                is_fast_path = g["in_fast_path"]
            snr = sum(snr_values) / len(snr_values) if snr_values else None
            a_lonlat = resolved[a]
            b_lonlat = resolved[b]
            edge_features.append(
                _edge_to_feature(
                    a_lonlat,
                    b_lonlat,
                    {
                        "link_start": a,
                        "link_end": b,
                        "link_start_label": node_id_format(a),
                        "link_end_label": node_id_format(b),
                        "snr": snr,
                        "snr_color": _snr_color(snr),
                        "direction": direction,
                        "is_fast_path": is_fast_path,
                        "route_len": g["max_route_len"],
                        "is_overflow": g["is_overflow"],
                        "link_count": link_count,
                    },
                )
            )

        # Unknown hop edges (grouped by string pair, kept directed)
        unknown_groups: dict[tuple[str, str], dict] = {}
        for row in filtered_rows:
            start = row["link_start"]
            end = row["link_end"]
            if isinstance(start, int) and isinstance(end, int):
                continue
            key = (str(start), str(end))
            g = unknown_groups.get(key)
            if g is None:
                g = {
                    "start": start,
                    "end": end,
                    "out_snr_values": [],
                    "in_snr_values": [],
                    "out_count": 0,
                    "in_count": 0,
                }
                unknown_groups[key] = g
            snr = float(row["snr"]) if row["snr"] is not None else None
            if not row["is_reply"]:
                if snr is not None:
                    g["out_snr_values"].append(snr)
                g["out_count"] += 1
            else:
                if snr is not None:
                    g["in_snr_values"].append(snr)
                g["in_count"] += 1

        for g in unknown_groups.values():
            start, end = g["start"], g["end"]
            bounds: list[tuple[float, float]] = []
            for val in (start, end):
                if isinstance(val, int) and val in node_pos_by_id:
                    bounds.append(node_pos_by_id[val])
            if not bounds:
                continue
            midpoint = _centroid(bounds)
            has_out = g["out_count"] > 0
            has_in = g["in_count"] > 0
            if has_out and has_in:
                direction = "both"
                snr_values = g["out_snr_values"] + g["in_snr_values"]
            elif has_out:
                direction = "out"
                snr_values = g["out_snr_values"]
            else:
                direction = "in"
                snr_values = g["in_snr_values"]
            snr = sum(snr_values) / len(snr_values) if snr_values else None
            edge_features.append(
                _edge_to_feature(
                    midpoint,
                    midpoint,
                    {
                        "link_start": start if isinstance(start, int) else str(start),
                        "link_end": end if isinstance(end, int) else str(end),
                        "link_start_label": str(start),
                        "link_end_label": str(end),
                        "snr": snr,
                        "snr_color": _snr_color(snr),
                        "direction": direction,
                        "is_fast_path": False,
                        "is_unknown_hop": True,
                    },
                )
            )

        span.set_attribute("geo.node_count", len(node_features))
        span.set_attribute("geo.edge_count", len(edge_features))
        return {
            "type": "FeatureCollection",
            "features": node_features + edge_features,
        }


def build_node_geojson(
    conn: sqlite3.Connection,
    node_id: int,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    direction: str = "both",
    depth: int = 1,
) -> dict:
    """Build a GeoJSON FeatureCollection for a node neighborhood."""
    with traced_span(
        "geo.build_node_geojson",
        warn_ms=2000,
        attributes={"node_id": node_id, "direction": direction, "depth": depth},
    ) as span:
        if direction not in {"inbound", "outbound", "both", "network"}:
            raise ValueError(f"Unsupported direction '{direction}'")

        # Iterative SQL BFS mirroring build_node_graph
        all_rows: list[sqlite3.Row] = []
        seen_nodes: set[int] = {node_id}
        frontier: list[int] = [node_id]

        if direction in ("both", "network"):
            sql_direction = "both"
        else:
            sql_direction = direction

        for _ in range(depth):
            if not frontier:
                break
            new_rows = get_links_for_nodes(
                conn,
                frontier,
                start_ts=start_ts,
                end_ts=end_ts,
                direction=sql_direction,
                exclude_string_nodes=True,
            )
            all_rows.extend(new_rows)
            next_frontier: set[int] = set()
            for row in new_rows:
                for val in (row["link_start"], row["link_end"]):
                    if isinstance(val, int) and val not in seen_nodes:
                        next_frontier.add(val)
                        seen_nodes.add(val)
            frontier = list(next_frontier)

        # Deduplicate rows
        seen_keys: set[tuple] = set()
        rows: list[sqlite3.Row] = []
        for row in all_rows:
            key = (
                row["trace_id"],
                row["from_id"],
                row["to_id"],
                row["link_start"],
                row["link_end"],
                row["is_reply"],
            )
            if key not in seen_keys:
                seen_keys.add(key)
                rows.append(row)

        # Build adjacency and collect positions from materialized link columns
        node_ids = set(seen_nodes)
        adjacency: dict[int, set[int]] = {}
        node_pos_id_by_node: dict[int, int] = {}
        for row in rows:
            start = row["link_start"]
            end = row["link_end"]
            if isinstance(start, int) and isinstance(end, int):
                adjacency.setdefault(start, set()).add(end)
                adjacency.setdefault(end, set()).add(start)
            for endpoint in ("link_start", "link_end"):
                val = row[endpoint]
                if not isinstance(val, int):
                    continue
                pos_id_col = f"{endpoint}_position_id"
                pos_id = row[pos_id_col]
                if pos_id is None:
                    continue
                # Prefer link_end position on ties.
                existing = node_pos_id_by_node.get(val)
                if existing is None or endpoint == "link_end":
                    node_pos_id_by_node[val] = pos_id

        node_pos_by_id: dict[int, tuple[float, float]] = {}
        if node_pos_id_by_node:
            placeholders = ",".join("?" * len(node_pos_id_by_node))
            pos_rows = conn.execute(
                f"SELECT id, latitude_i, longitude_i FROM positions WHERE id IN ({placeholders})",
                list(node_pos_id_by_node.values()),
            ).fetchall()
            pos_by_id = {r["id"]: r for r in pos_rows}
            for nid, pos_id in node_pos_id_by_node.items():
                pos = pos_by_id.get(pos_id)
                if pos is not None:
                    node_pos_by_id[nid] = _pos_to_lonlat(pos)

        # Fall back to current node positions for nodes without materialized link positions.
        for nid, lonlat in _current_node_positions(conn, node_ids - set(node_pos_by_id)).items():
            node_pos_by_id[nid] = lonlat

        resolved = _approximate_positions(node_ids, node_pos_by_id, adjacency)

        node_features: list[dict] = []
        node_info_cache: dict[int, sqlite3.Row] = {}
        for nid in sorted(node_ids):
            info = node_info_cache.get(nid)
            if info is None:
                info = _node_info(conn, nid)
                node_info_cache[nid] = info
            lonlat = resolved[nid]
            node_features.append(
                _node_to_feature(
                    nid,
                    lonlat,
                    role=info["role"],
                    long_name=info["long_name"],
                    short_name=info["short_name"],
                    has_position=nid in node_pos_by_id,
                    is_approximated=nid not in node_pos_by_id,
                    is_center=nid == node_id,
                )
            )

        # Collapse edges per unordered pair and determine directionality:
        # "out" = only A->B, "in" = only B->A, "both" = both directions observed.
        edge_groups: dict[tuple[int, int], dict] = {}
        for row in rows:
            start = row["link_start"]
            end = row["link_end"]
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            key = (min(start, end), max(start, end))
            g = edge_groups.get(key)
            if g is None:
                g = {
                    "a": key[0],
                    "b": key[1],
                    "out_snr_values": [],
                    "in_snr_values": [],
                    "out_count": 0,
                    "in_count": 0,
                    "out_fast_path": False,
                    "in_fast_path": False,
                }
                edge_groups[key] = g
            if not row["is_reply"]:
                if row["snr"] is not None:
                    g["out_snr_values"].append(float(row["snr"]))
                g["out_count"] += 1
                if row["is_fast_path"]:
                    g["out_fast_path"] = True
            else:
                if row["snr"] is not None:
                    g["in_snr_values"].append(float(row["snr"]))
                g["in_count"] += 1
                if row["is_fast_path"]:
                    g["in_fast_path"] = True

        edge_features: list[dict] = []
        for key, g in edge_groups.items():
            a, b = key
            if a not in resolved or b not in resolved:
                continue
            has_out = g["out_count"] > 0
            has_in = g["in_count"] > 0
            if has_out and has_in:
                direction = "both"
                snr_values = g["out_snr_values"] + g["in_snr_values"]
                link_count = g["out_count"] + g["in_count"]
                is_fast_path = g["out_fast_path"] or g["in_fast_path"]
            elif has_out:
                direction = "out"
                snr_values = g["out_snr_values"]
                link_count = g["out_count"]
                is_fast_path = g["out_fast_path"]
            else:
                direction = "in"
                snr_values = g["in_snr_values"]
                link_count = g["in_count"]
                is_fast_path = g["in_fast_path"]
            snr = sum(snr_values) / len(snr_values) if snr_values else None
            a_lonlat = resolved[a]
            b_lonlat = resolved[b]
            edge_features.append(
                _edge_to_feature(
                    a_lonlat,
                    b_lonlat,
                    {
                        "link_start": a,
                        "link_end": b,
                        "link_start_label": node_id_format(a),
                        "link_end_label": node_id_format(b),
                        "snr": snr,
                        "snr_color": _snr_color(snr),
                        "direction": direction,
                        "is_fast_path": is_fast_path,
                        "link_count": link_count,
                    },
                )
            )

        span.set_attribute("geo.node_count", len(node_features))
        span.set_attribute("geo.edge_count", len(edge_features))
        return {
            "type": "FeatureCollection",
            "features": node_features + edge_features,
        }
