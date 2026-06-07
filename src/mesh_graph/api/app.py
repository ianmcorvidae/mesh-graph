from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response

from mesh_graph.api.models import NodeOut, TracerouteOut
from mesh_graph.config import ObservabilityConfig
from mesh_graph.graph.builder import (
    build_node_graph,
    build_simple_network_graph,
    build_trace_graph,
)
from mesh_graph.graph.renderer import render
from mesh_graph.observability import instrument_fastapi, traced_span

_MEDIA_TYPES = {"png": "image/png", "svg": "image/svg+xml"}
_NETWORK_GRAPH_QUERY_PARAMS = {
    "format",
    "start",
    "end",
    "snr_labels",
    "include_unknown_nodes",
    "include_clients",
}
_TRACE_GRAPH_QUERY_PARAMS = {"format", "from", "to", "date", "direction"}
_NODE_GRAPH_QUERY_PARAMS = {"format", "start", "end", "direction", "depth"}


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


def _parse_time_range(
    start: Optional[str], end: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    try:
        return _parse_iso(start), _parse_iso(end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid timestamp: {exc}") from exc


def _reject_unknown_query_params(request: Request, allowed_params: set[str]) -> None:
    unknown = sorted(
        {param for param in request.query_params.keys() if param not in allowed_params}
    )
    if unknown:
        unknown_str = ", ".join(unknown)
        allowed_str = ", ".join(sorted(allowed_params))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown query parameter(s): {unknown_str}. Allowed parameters: {allowed_str}",
        )


def create_app(
    db: sqlite3.Connection, observability_cfg: Optional[ObservabilityConfig] = None
) -> FastAPI:
    app = FastAPI(title="mesh-graph")
    if observability_cfg and observability_cfg.enabled:
        instrument_fastapi(app)

    @app.get("/graph/network")
    def graph_network(
        request: Request,
        format: str = Query(default="svg"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        snr_labels: bool = Query(default=False),
        include_unknown_nodes: bool = Query(default=False),
        include_clients: bool = Query(default=False),
    ):
        with traced_span(
            "api.graph.network",
            warn_ms=5000,
            attributes={
                "format": format,
                "snr_labels": snr_labels,
                "include_unknown_nodes": include_unknown_nodes,
                "include_clients": include_clients,
            },
        ):
            _reject_unknown_query_params(request, _NETWORK_GRAPH_QUERY_PARAMS)
            if format not in _MEDIA_TYPES:
                raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
            with traced_span("parse_time_range", warn_ms=50):
                start_ts, end_ts = _parse_time_range(start, end)
            with traced_span("graph.build_simple_network_graph", warn_ms=2000) as span:
                G = build_simple_network_graph(
                    db,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    include_snr_labels=snr_labels,
                    include_unknown_nodes=include_unknown_nodes,
                    include_clients=include_clients,
                )
                span.set_attribute("graph.node_count", len(G.nodes))
                span.set_attribute("graph.edge_count", len(G.edges))
            with traced_span(
                "renderer.render",
                warn_ms=5000,
                attributes={"format": format, "layout_prog": "sfdp"},
            ) as span:
                content = render(G, format, layout_prog="sfdp")
                span.set_attribute("output.bytes", len(content))
            return Response(content=content, media_type=_MEDIA_TYPES[format])

    @app.get("/graph/trace/{trace_id}")
    def graph_trace(
        trace_id: int,
        request: Request,
        format: str = Query(default="svg"),
        from_node: Optional[str] = Query(default=None, alias="from"),
        to_node: Optional[str] = Query(default=None, alias="to"),
        date: Optional[str] = Query(default=None),
        direction: Literal["both", "out", "in"] = Query(default="both"),
    ):
        with traced_span(
            "api.graph.trace",
            warn_ms=5000,
            attributes={"format": format, "trace_id": trace_id, "direction": direction},
        ):
            _reject_unknown_query_params(request, _TRACE_GRAPH_QUERY_PARAMS)
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

            with traced_span("graph.build_trace_graph", warn_ms=2000) as span:
                G = build_trace_graph(
                    db,
                    trace_id=trace_id,
                    from_id=from_id,
                    to_id=to_id,
                    approx_ts=approx_ts,
                    direction=direction,
                )
                if G is not None:
                    span.set_attribute("graph.node_count", len(G.nodes))
                    span.set_attribute("graph.edge_count", len(G.edges))
            if G is None:
                raise HTTPException(status_code=404, detail="Trace not found")
            with traced_span(
                "renderer.render", warn_ms=5000, attributes={"format": format, "layout_prog": "dot"}
            ) as span:
                content = render(G, format, layout_prog="dot")
                span.set_attribute("output.bytes", len(content))
            return Response(content=content, media_type=_MEDIA_TYPES[format])

    @app.get("/graph/node/{node_id}")
    def graph_node(
        node_id: str,
        request: Request,
        format: str = Query(default="svg"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        direction: Literal["inbound", "outbound", "both", "network"] = Query(default="both"),
        depth: int = Query(default=1, ge=1, le=10),
    ):
        with traced_span(
            "api.graph.node",
            warn_ms=5000,
            attributes={"format": format, "direction": direction, "depth": depth},
        ):
            _reject_unknown_query_params(request, _NODE_GRAPH_QUERY_PARAMS)
            if format not in _MEDIA_TYPES:
                raise HTTPException(status_code=400, detail=f"Unsupported format '{format}'")
            try:
                nid = _parse_node_id(node_id)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid node_id: {node_id!r}")
            with traced_span("parse_time_range", warn_ms=50):
                start_ts, end_ts = _parse_time_range(start, end)
            with traced_span("graph.build_node_graph", warn_ms=2000) as span:
                G = build_node_graph(
                    db,
                    node_id=nid,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    direction=direction,
                    depth=depth,
                )
                span.set_attribute("graph.node_count", len(G.nodes))
                span.set_attribute("graph.edge_count", len(G.edges))
            with traced_span(
                "renderer.render", warn_ms=5000, attributes={"format": format, "layout_prog": "dot"}
            ) as span:
                content = render(G, format, layout_prog="dot")
                span.set_attribute("output.bytes", len(content))
            return Response(content=content, media_type=_MEDIA_TYPES[format])

    @app.get("/nodes", response_model=List[NodeOut])
    def list_nodes(
        after: Optional[int] = Query(
            default=None, description="Return nodes seen at or before this UNIX timestamp"
        ),
        limit: int = Query(
            default=100, ge=1, le=500, description="Maximum number of rows to return"
        ),
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
        after: Optional[int] = Query(
            default=None, description="Return traceroutes seen at or before this UNIX timestamp"
        ),
        limit: int = Query(
            default=100, ge=1, le=500, description="Maximum number of rows to return"
        ),
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
