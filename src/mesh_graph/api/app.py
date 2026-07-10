from __future__ import annotations

import os
import sqlite3
import time
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from starlette.staticfiles import StaticFiles
from starlette_compress import CompressMiddleware

from mesh_graph.api.cache import TTLCache, cache_key
from mesh_graph.api.models import NodeOut, TracerouteOut
from mesh_graph.config import ObservabilityConfig
from mesh_graph.db import (
    get_max_link_ts,
    get_max_link_ts_for_node,
    get_max_ts_for_trace,
    get_nodes,
    get_traceroutes,
    parse_node_id,
)
from mesh_graph.graph.builder import (
    build_node_graph,
    build_simple_network_graph,
    build_trace_graph,
)
from mesh_graph.graph.renderer import render
from mesh_graph.observability import instrument_fastapi, traced_span
from mesh_graph.utils import parse_iso, parse_time_bounds

_MEDIA_TYPES = {"png": "image/png", "svg": "image/svg+xml", "dot": "text/vnd.graphviz"}
_NETWORK_GRAPH_QUERY_PARAMS = {
    "format",
    "start",
    "end",
    "snr_labels",
    "include_unknown_nodes",
    "include_clients",
    "clickable",
}
_TRACE_GRAPH_QUERY_PARAMS = {
    "format",
    "from",
    "to",
    "date",
    "direction",
    "communities",
    "clickable",
}
_NODE_GRAPH_QUERY_PARAMS = {"format", "start", "end", "direction", "depth", "clickable"}


def _parse_time_range(
    start: Optional[str], end: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    try:
        return parse_time_bounds(start, end)
    except (ValueError, TypeError) as exc:
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


def _build_and_render(
    db: sqlite3.Connection,
    cache: TTLCache,
    build_fn,
    key: str,
    cache_ttl: int,
    format: str,
    layout_prog: str,
    span_name: str,
    span_attributes: Optional[dict] = None,
):
    """Check cache, build graph, render, store, and return Response.

    Returns ``Response`` on success.  Raises ``HTTPException(404)`` when the
    build function returns ``None`` (e.g. trace not found).
    """
    with traced_span("cache.lookup", warn_ms=5) as span:
        cached = cache.get(key)
        span.set_attribute("cache.hit", cached is not None)
    if cached is not None:
        return Response(content=cached, media_type=_MEDIA_TYPES[format])

    with traced_span(span_name, warn_ms=2000) as span:
        G = build_fn()
        if G is not None:
            span.set_attribute("graph.node_count", len(G.nodes))
            span.set_attribute("graph.edge_count", len(G.edges))
    if G is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    with traced_span(
        "renderer.render", warn_ms=5000, attributes={"format": format, "layout_prog": layout_prog}
    ) as span:
        content = render(G, format, layout_prog=layout_prog)
        span.set_attribute("output.bytes", len(content))
    cache.set(key, content, ttl=cache_ttl)
    return Response(content=content, media_type=_MEDIA_TYPES[format])


def create_app(
    db: sqlite3.Connection, observability_cfg: Optional[ObservabilityConfig] = None
) -> FastAPI:
    app = FastAPI(title="mesh-graph")
    app.add_middleware(CompressMiddleware, minimum_size=500)
    if observability_cfg and observability_cfg.enabled:
        instrument_fastapi(app)
    _cache = TTLCache()
    app.state.db = db
    app.state._graph_cache = _cache
    app.state._geojson_cache = TTLCache()

    from mesh_graph.api.geo import router as geo_router

    app.include_router(geo_router)

    @app.get("/graph/network")
    def graph_network(
        request: Request,
        format: str = Query(default="svg"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        snr_labels: bool = Query(default=False),
        include_unknown_nodes: bool = Query(default=False),
        include_clients: bool = Query(default=False),
        clickable: bool = Query(default=False),
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

            now_ts = int(time.time())
            end_is_past = end_ts is not None and end_ts <= now_ts
            cache_ttl = 3600 if end_is_past else 60

            with traced_span("cache.version_query", warn_ms=10):
                max_ts = get_max_link_ts(db, start_ts=start_ts, end_ts=end_ts)

            ck = cache_key(
                endpoint="network",
                start_ts=start_ts,
                end_is_past=end_is_past,
                max_ts=max_ts,
                snr_labels=snr_labels,
                include_unknown_nodes=include_unknown_nodes,
                include_clients=include_clients,
                clickable=clickable,
                format=format,
                layout="sfdp",
            )
            return _build_and_render(
                db,
                _cache,
                lambda: build_simple_network_graph(
                    db,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    include_snr_labels=snr_labels,
                    include_unknown_nodes=include_unknown_nodes,
                    include_clients=include_clients,
                    clickable=clickable,
                ),
                ck,
                cache_ttl,
                format,
                layout_prog="sfdp",
                span_name="graph.build_simple_network_graph",
            )

    @app.get("/graph/trace/{trace_id}")
    def graph_trace(
        trace_id: int,
        request: Request,
        format: str = Query(default="svg"),
        from_node: Optional[str] = Query(default=None, alias="from"),
        to_node: Optional[str] = Query(default=None, alias="to"),
        date: Optional[str] = Query(default=None),
        direction: Literal["both", "out", "in"] = Query(default="both"),
        communities: str = Query(default="false"),
        clickable: bool = Query(default=False),
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
                from_id = parse_node_id(from_node) if from_node is not None else None
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid from node_id: {from_node!r}")
            try:
                to_id = parse_node_id(to_node) if to_node is not None else None
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid to node_id: {to_node!r}")
            try:
                approx_ts = parse_iso(date)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Invalid timestamp: {exc}") from exc

            communities_lower = communities.strip().lower()
            if communities_lower in ("", "false", "0"):
                resolution: Optional[float] = None
            elif communities_lower == "true":
                resolution = 1.0
            else:
                try:
                    resolution = float(communities)
                except ValueError:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid communities value {communities!r}. Use 'true', 'false', or a number.",
                    )

            with traced_span("cache.version_query", warn_ms=10):
                max_ts = get_max_ts_for_trace(db, trace_id)

            ck = cache_key(
                endpoint="trace",
                trace_id=trace_id,
                from_id=from_id,
                to_id=to_id,
                approx_ts=approx_ts,
                direction=direction,
                resolution=resolution,
                clickable=clickable,
                format=format,
                layout="dot",
                max_ts=max_ts,
            )
            return _build_and_render(
                db,
                _cache,
                lambda: build_trace_graph(
                    db,
                    trace_id=trace_id,
                    from_id=from_id,
                    to_id=to_id,
                    approx_ts=approx_ts,
                    direction=direction,
                    resolution=resolution,
                    clickable=clickable,
                ),
                ck,
                3600,
                format,
                layout_prog="dot",
                span_name="graph.build_trace_graph",
            )

    @app.get("/graph/node/{node_id}")
    def graph_node(
        node_id: str,
        request: Request,
        format: str = Query(default="svg"),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        direction: Literal["inbound", "outbound", "both", "network"] = Query(default="both"),
        depth: int = Query(default=1, ge=1, le=10),
        clickable: bool = Query(default=False),
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
                nid = parse_node_id(node_id)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid node_id: {node_id!r}")
            with traced_span("parse_time_range", warn_ms=50):
                start_ts, end_ts = _parse_time_range(start, end)

            now_ts = int(time.time())
            end_is_past = end_ts is not None and end_ts <= now_ts
            use_version_key = depth == 1 or end_is_past

            if use_version_key:
                with traced_span("cache.version_query", warn_ms=10):
                    max_ts = get_max_link_ts_for_node(db, nid, start_ts=start_ts, end_ts=end_ts)
                ck = cache_key(
                    endpoint="node",
                    nid=nid,
                    start_ts=start_ts,
                    end_is_past=end_is_past,
                    direction=direction,
                    depth=depth,
                    clickable=clickable,
                    format=format,
                    layout="dot",
                    max_ts=max_ts,
                )
                cache_ttl = 3600
            else:
                ck = cache_key(
                    endpoint="node",
                    nid=nid,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    direction=direction,
                    depth=depth,
                    clickable=clickable,
                    format=format,
                    layout="dot",
                )
                cache_ttl = 60

            return _build_and_render(
                db,
                _cache,
                lambda: build_node_graph(
                    db,
                    node_id=nid,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    direction=direction,
                    depth=depth,
                    clickable=clickable,
                ),
                ck,
                cache_ttl,
                format,
                layout_prog="dot",
                span_name="graph.build_node_graph",
            )

    @app.get("/api/nodes", response_model=List[NodeOut])
    def list_nodes(
        after: Optional[int] = Query(
            default=None, description="Return nodes seen at or before this UNIX timestamp"
        ),
        limit: int = Query(
            default=100, ge=1, le=500, description="Maximum number of rows to return"
        ),
    ):
        rows, _next = get_nodes(db, cursor=after, limit=limit)
        return [dict(r) for r in rows]

    @app.get("/api/traceroutes", response_model=List[TracerouteOut])
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
            from_id = parse_node_id(from_node) if from_node is not None else None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid from node_id: {from_node!r}")
        try:
            to_id = parse_node_id(to_node) if to_node is not None else None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid to node_id: {to_node!r}")

        rows, _next = get_traceroutes(db, cursor=after, limit=limit, from_id=from_id, to_id=to_id)
        return [dict(r) for r in rows]

    _static = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(_static):
        app.mount("/static", StaticFiles(directory=_static), name="static")

    from mesh_graph.api.ui import router as ui_router

    app.include_router(ui_router)

    return app
