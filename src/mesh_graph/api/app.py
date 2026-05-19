from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Response

from mesh_graph.api.models import NodeOut, TracerouteOut
from mesh_graph.db import get_connection, get_links_for_network, init_db
from mesh_graph.graph.builder import (
    build_network_graph,
    build_node_graph,
    build_simple_network_graph,
    build_trace_graph,
)
from mesh_graph.graph.renderer import render

_MEDIA_TYPES = {"png": "image/png", "svg": "image/svg+xml"}


def _parse_iso(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    # '+' in a query string is decoded as a space; restore it for ISO 8601 offsets
    dt = datetime.fromisoformat(value.replace(" ", "+"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_node_id(node_id: str) -> int:
    """Accept '!aabbccdd', 0x-prefixed hex, plain hex, or decimal."""
    s = node_id.strip()
    if s.startswith("!"):
        return int(s[1:], 16)
    if s.lower().startswith("0x"):
        return int(s, 16)
    if s.isdigit():
        return int(s, 10)
    try:
        return int(s, 16)
    except ValueError:
        return int(s)


def _parse_time_range(start: Optional[str], end: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    try:
        return _parse_iso(start), _parse_iso(end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid timestamp: {exc}") from exc


def create_app(db: sqlite3.Connection) -> FastAPI:
    app = FastAPI(title="mesh-graph")

    @app.get("/graph/network")
    def graph_network(
        format: str = Query(default="png"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
    ):
        if format not in _MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
        start_ts, end_ts = _parse_time_range(start, end)
        G = build_network_graph(db, start_ts=start_ts, end_ts=end_ts)
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/graph/network/simple")
    def graph_network_simple(
        format: str = Query(default="png"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
    ):
        if format not in _MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
        start_ts, end_ts = _parse_time_range(start, end)
        G = build_simple_network_graph(db, start_ts=start_ts, end_ts=end_ts)
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/graph/trace/{trace_id}")
    def graph_trace(
        trace_id: int,
        format: str = Query(default="png"),
        from_node: Optional[str] = Query(default=None, alias="from"),
        to_node: Optional[str] = Query(default=None, alias="to"),
        date: Optional[str] = Query(default=None),
    ):
        if format not in _MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
        try:
            from_id = _parse_node_id(from_node) if from_node is not None else None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid from node_id: {from_node!r}")
        try:
            to_id = _parse_node_id(to_node) if to_node is not None else None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid to node_id: {to_node!r}")
        try:
            approx_ts = _parse_iso(date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid timestamp: {exc}") from exc

        G = build_trace_graph(
            db,
            trace_id=trace_id,
            from_id=from_id,
            to_id=to_id,
            approx_ts=approx_ts,
        )
        if G is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/graph/node/{node_id}")
    def graph_node(
        node_id: str,
        format: str = Query(default="png"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        direction: Literal["inbound", "outbound", "both", "network"] = Query(default="both"),
        depth: int = Query(default=1, ge=1, le=10),
    ):
        if format not in _MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
        try:
            nid = _parse_node_id(node_id)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid node_id: {node_id!r}")
        start_ts, end_ts = _parse_time_range(start, end)
        G = build_node_graph(
            db,
            node_id=nid,
            start_ts=start_ts,
            end_ts=end_ts,
            direction=direction,
            depth=depth,
        )
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/nodes", response_model=List[NodeOut])
    def list_nodes(
        after: Optional[int] = Query(default=None, description="Return nodes seen at or before this UNIX timestamp"),
        limit: int = Query(default=100, ge=1, le=500, description="Maximum number of rows to return"),
    ):
        cursor = int(time.time()) if after is None else after
        rows = db.execute(
            "SELECT nodenum, long_name, short_name, role, last_seen_ts FROM nodes "
            "WHERE last_seen_ts <= ? "
            "ORDER BY last_seen_ts DESC, nodenum DESC "
            "LIMIT ?",
            (cursor, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/traceroutes", response_model=List[TracerouteOut])
    def list_traceroutes(
        after: Optional[int] = Query(default=None, description="Return traceroutes seen at or before this UNIX timestamp"),
        limit: int = Query(default=100, ge=1, le=500, description="Maximum number of rows to return"),
        from_node: Optional[str] = Query(default=None, alias="from"),
        to_node: Optional[str] = Query(default=None, alias="to"),
    ):
        try:
            from_id = _parse_node_id(from_node) if from_node is not None else None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid from node_id: {from_node!r}")
        try:
            to_id = _parse_node_id(to_node) if to_node is not None else None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid to node_id: {to_node!r}")

        cursor = int(time.time()) if after is None else after
        query = (
            "SELECT trace_id, from_id, to_id, first_seen_ts FROM traceroute "
            "WHERE first_seen_ts <= ?"
        )
        params: list = [cursor]
        if from_id is not None:
            query += " AND from_id = ?"
            params.append(from_id)
        if to_id is not None:
            query += " AND to_id = ?"
            params.append(to_id)
        query += " ORDER BY first_seen_ts DESC, trace_id DESC, from_id DESC, to_id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(
            query,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    return app
