from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from mesh_graph.api.models import NodeOut, TracerouteOut
from mesh_graph.db import get_connection, get_links_for_network, init_db
from mesh_graph.graph.builder import (
    build_network_graph,
    build_node_graph,
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
    """Accept '!aabbccdd' or plain hex or decimal."""
    s = node_id.strip()
    if s.startswith("!"):
        return int(s[1:], 16)
    try:
        return int(s, 16)
    except ValueError:
        return int(s)


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
        G = build_network_graph(db, start_ts=_parse_iso(start), end_ts=_parse_iso(end))
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/graph/trace/{trace_id}")
    def graph_trace(trace_id: int, format: str = Query(default="png")):
        if format not in _MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
        G = build_trace_graph(db, trace_id=trace_id)
        if G is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/graph/node/{node_id}")
    def graph_node(
        node_id: str,
        format: str = Query(default="png"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
    ):
        if format not in _MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
        try:
            nid = _parse_node_id(node_id)
        except ValueError:
            from fastapi import status
            raise HTTPException(status_code=422, detail=f"Invalid node_id: {node_id!r}")
        G = build_node_graph(db, node_id=nid, start_ts=_parse_iso(start), end_ts=_parse_iso(end))
        return Response(content=render(G, format), media_type=_MEDIA_TYPES[format])

    @app.get("/nodes", response_model=List[NodeOut])
    def list_nodes():
        rows = db.execute("SELECT nodenum, long_name, short_name, role, last_seen_ts FROM nodes").fetchall()
        return [dict(r) for r in rows]

    @app.get("/traceroutes", response_model=List[TracerouteOut])
    def list_traceroutes():
        rows = db.execute("SELECT trace_id, from_id, to_id, first_seen_ts FROM traceroute ORDER BY first_seen_ts DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]

    return app
