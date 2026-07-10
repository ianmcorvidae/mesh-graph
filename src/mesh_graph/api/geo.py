from __future__ import annotations

import json
import sqlite3
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response

from mesh_graph.api.cache import TTLCache, cache_key
from mesh_graph.db import (
    get_max_link_ts,
    get_max_link_ts_for_node,
    get_max_ts_for_trace,
    parse_node_id,
)
from mesh_graph.geo.builder import (
    build_network_geojson,
    build_node_geojson,
    build_trace_geojson,
)
from mesh_graph.observability import traced_span
from mesh_graph.utils import parse_iso

router = APIRouter()

_MEDIA_TYPE = "application/geo+json"

_NETWORK_GEOJSON_QUERY_PARAMS = {
    "start",
    "end",
    "include_clients",
    "include_unknown_nodes",
    "direction",
}
_TRACE_GEOJSON_QUERY_PARAMS = {"from", "to", "date", "direction"}
_NODE_GEOJSON_QUERY_PARAMS = {"start", "end", "direction", "depth"}


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


def _parse_time_range(
    start: Optional[str], end: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    try:
        return parse_iso(start), parse_iso(end)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid timestamp: {exc}") from exc


def _build_geojson(
    cache: TTLCache,
    build_fn,
    key: str,
    ttl: int,
) -> Response:
    with traced_span("cache.lookup", warn_ms=5) as span:
        cached = cache.get(key)
        span.set_attribute("cache.hit", cached is not None)
    if cached is not None:
        return Response(content=cached, media_type=_MEDIA_TYPE)

    with traced_span("geo.build", warn_ms=2000):
        data = build_fn()
    if data is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    content = json.dumps(data, separators=(",", ":")).encode("utf-8")
    cache.set(key, content, ttl=ttl)
    return Response(content=content, media_type=_MEDIA_TYPE)


@router.get("/api/geojson/network")
def geojson_network(
    request: Request,
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    include_clients: bool = Query(default=False),
    include_unknown_nodes: bool = Query(default=False),
    direction: Literal["both", "outbound", "inbound"] = Query(default="both"),
):
    with traced_span(
        "api.geojson.network",
        warn_ms=5000,
        attributes={
            "include_clients": include_clients,
            "include_unknown_nodes": include_unknown_nodes,
            "direction": direction,
        },
    ):
        _reject_unknown_query_params(request, _NETWORK_GEOJSON_QUERY_PARAMS)
        db: sqlite3.Connection = request.app.state.db
        start_ts, end_ts = _parse_time_range(start, end)

        now_ts = int(time.time())
        end_is_past = end_ts is not None and end_ts <= now_ts
        cache_ttl = 3600 if end_is_past else 60

        with traced_span("cache.version_query", warn_ms=10):
            max_ts = get_max_link_ts(db, start_ts=start_ts, end_ts=end_ts)

        key = cache_key(
            endpoint="geojson-network",
            start_ts=start_ts,
            end_ts=end_ts,
            end_is_past=end_is_past,
            max_ts=max_ts,
            include_clients=include_clients,
            include_unknown_nodes=include_unknown_nodes,
            direction=direction,
        )
        cache: TTLCache = request.app.state._geojson_cache
        return _build_geojson(
            cache,
            lambda: build_network_geojson(
                db,
                start_ts=start_ts,
                end_ts=end_ts,
                include_clients=include_clients,
                include_unknown=include_unknown_nodes,
                direction=direction,
            ),
            key,
            cache_ttl,
        )


@router.get("/api/geojson/trace/{trace_id}")
def geojson_trace(
    trace_id: int,
    request: Request,
    from_node: Optional[str] = Query(default=None, alias="from"),
    to_node: Optional[str] = Query(default=None, alias="to"),
    date: Optional[str] = Query(default=None),
    direction: Literal["both", "out", "in"] = Query(default="both"),
):
    with traced_span(
        "api.geojson.trace",
        warn_ms=5000,
        attributes={"trace_id": trace_id, "direction": direction},
    ):
        _reject_unknown_query_params(request, _TRACE_GEOJSON_QUERY_PARAMS)
        db: sqlite3.Connection = request.app.state.db
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
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid timestamp: {exc}") from exc

        with traced_span("cache.version_query", warn_ms=10):
            max_ts = get_max_ts_for_trace(db, trace_id)

        key = cache_key(
            endpoint="geojson-trace",
            trace_id=trace_id,
            from_id=from_id,
            to_id=to_id,
            approx_ts=approx_ts,
            direction=direction,
            max_ts=max_ts,
        )
        cache: TTLCache = request.app.state._geojson_cache
        return _build_geojson(
            cache,
            lambda: build_trace_geojson(
                db,
                trace_id=trace_id,
                from_id=from_id,
                to_id=to_id,
                approx_ts=approx_ts,
                direction=direction,
            ),
            key,
            3600,
        )


@router.get("/api/geojson/node/{node_id}")
def geojson_node(
    node_id: str,
    request: Request,
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    direction: Literal["inbound", "outbound", "both", "network"] = Query(default="both"),
    depth: int = Query(default=1, ge=1, le=10),
):
    with traced_span(
        "api.geojson.node",
        warn_ms=5000,
        attributes={"node_id": node_id, "direction": direction, "depth": depth},
    ):
        _reject_unknown_query_params(request, _NODE_GEOJSON_QUERY_PARAMS)
        db: sqlite3.Connection = request.app.state.db
        try:
            nid = parse_node_id(node_id)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid node_id: {node_id!r}")
        start_ts, end_ts = _parse_time_range(start, end)

        now_ts = int(time.time())
        end_is_past = end_ts is not None and end_ts <= now_ts
        use_version_key = depth == 1 or end_is_past

        if use_version_key:
            with traced_span("cache.version_query", warn_ms=10):
                max_ts = get_max_link_ts_for_node(db, nid, start_ts=start_ts, end_ts=end_ts)
            key = cache_key(
                endpoint="geojson-node",
                nid=nid,
                start_ts=start_ts,
                end_ts=end_ts,
                end_is_past=end_is_past,
                direction=direction,
                depth=depth,
                max_ts=max_ts,
            )
            cache_ttl = 3600
        else:
            key = cache_key(
                endpoint="geojson-node",
                nid=nid,
                start_ts=start_ts,
                end_ts=end_ts,
                direction=direction,
                depth=depth,
            )
            cache_ttl = 60

        cache: TTLCache = request.app.state._geojson_cache
        return _build_geojson(
            cache,
            lambda: build_node_geojson(
                db,
                node_id=nid,
                start_ts=start_ts,
                end_ts=end_ts,
                direction=direction,
                depth=depth,
            ),
            key,
            cache_ttl,
        )
