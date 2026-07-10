from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from mesh_graph.db import (
    get_dashboard_stats,
    get_links_for_nodes,
    get_links_for_trace,
    get_node,
    get_nodes,
    get_recent_nodes,
    get_recent_traceroutes,
    get_traceroutes,
    get_traceroutes_for_node,
    parse_node_id,
)
from mesh_graph.utils import node_id_format, node_id_str, parse_time_bounds

router = APIRouter()
TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES)


def _default_time_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def _geojson_url(request: Request, endpoint: str, defaults: dict) -> str:
    """Build a GeoJSON API URL from current query params, falling back to defaults."""
    params = dict(request.query_params)
    q = []
    for key, default in defaults.items():
        value = params.get(key, default)
        if value:
            q.append(f"{key}={value}")
    if endpoint.startswith("/api/geojson/network"):
        if params.get("include_clients") == "true":
            q.append("include_clients=true")
        if params.get("include_unknown_nodes") == "true":
            q.append("include_unknown_nodes=true")
    return f"{endpoint}?{'&'.join(q)}"


def format_ts(ts: Optional[int]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_ts_fmt(ts: Optional[int]) -> str:
    if ts is None:
        return ""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%b %d, %Y, %I:%M %p UTC")


templates.env.filters["format_ts"] = format_ts


def _get_db(request: Request):
    return request.app.state.db


def _group_node_connections(
    db, nid: int, start_ts: Optional[int], end_ts: Optional[int]
) -> list[dict]:
    rows = get_links_for_nodes(db, [nid], start_ts=start_ts, end_ts=end_ts)
    groups: dict[tuple[int, str], dict] = {}
    for row in rows:
        if isinstance(row["link_start"], int) and row["link_start"] == nid:
            neighbor = row["link_end"]
            direction = "out"
        elif isinstance(row["link_end"], int) and row["link_end"] == nid:
            neighbor = row["link_start"]
            direction = "in"
        else:
            continue
        if not isinstance(neighbor, int):
            continue
        key = (neighbor, direction)
        g = groups.get(key)
        if g is None:
            g = {
                "node_id": neighbor,
                "node_hex": node_id_format(neighbor),
                "direction": direction,
                "link_count": 0,
                "snr_min": None,
                "snr_max": None,
                "last_seen": 0,
                "role": None,
                "short_name": None,
            }
            groups[key] = g
        g["link_count"] += 1
        snr = row["snr"]
        if snr is not None:
            if g["snr_min"] is None or snr < g["snr_min"]:
                g["snr_min"] = snr
            if g["snr_max"] is None or snr > g["snr_max"]:
                g["snr_max"] = snr
        ts = row["ts"]
        if ts is not None and ts > g["last_seen"]:
            g["last_seen"] = ts

    if groups:
        from mesh_graph.db import get_node_attrs

        all_attrs = get_node_attrs(
            db,
            relevant_nodenums={k[0] for k in groups},
        )
        for (neighbor, _), g in groups.items():
            name_str = node_id_format(neighbor)
            attrs = all_attrs.get(name_str, {})
            label = attrs.get("label", "")
            lines = label.split("\n")
            g["short_name"] = lines[1] if len(lines) > 1 else None
            g["role"] = lines[2] if len(lines) > 2 else None
            g["last_seen_iso"] = format_ts(g["last_seen"])
            g["last_seen_fmt"] = format_ts_fmt(g["last_seen"])

    result = sorted(groups.values(), key=lambda g: g["link_count"], reverse=True)
    return result


def _enrich_trace_links(links: list[dict]) -> list[dict]:
    for link in links:
        link["_id"] = f"{link['link_start']}:{link['link_end']}:{link['is_reply']}:{link['ts']}"
        link["link_start_hex"] = node_id_format(link["link_start"])
        link["link_end_hex"] = node_id_format(link["link_end"])
        link["link_start_is_id"] = isinstance(link["link_start"], int)
        link["link_end_is_id"] = isinstance(link["link_end"], int)
        link["direction"] = "In" if link["is_reply"] else "Out"
        link["ts_iso"] = format_ts(link["ts"])
    return links


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db = _get_db(request)
    stats = get_dashboard_stats(db)
    recent_nodes = [dict(r) for r in get_recent_nodes(db)]
    recent_traces = [dict(r) for r in get_recent_traceroutes(db)]
    default_start, default_end = _default_time_bounds()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "node_count": stats["node_count"],
            "trace_count": stats["trace_count"],
            "recent_nodes": recent_nodes,
            "recent_traces": recent_traces,
            "default_start": default_start,
            "default_end": default_end,
        },
    )


@router.get("/network", response_class=HTMLResponse)
def network_page(request: Request):
    default_start, default_end = _default_time_bounds()
    geojson_url = _geojson_url(
        request,
        "/api/geojson/network",
        {"start": default_start, "end": default_end},
    )
    return templates.TemplateResponse(
        request,
        "network.html",
        {
            "default_start": default_start,
            "default_end": default_end,
            "geojson_url": geojson_url,
        },
    )


@router.get("/nodes", response_class=HTMLResponse)
def nodes_page(
    request: Request,
    q: Optional[str] = Query(default=None),
    after: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    db = _get_db(request)
    rows, next_cursor = get_nodes(db, cursor=after, limit=limit, search=q)
    return templates.TemplateResponse(
        request,
        "nodes_list.html",
        {
            "nodes": [dict(r) for r in rows],
            "next_cursor": next_cursor,
            "query": q,
        },
    )


@router.get("/nodes/partial", response_class=HTMLResponse)
def nodes_partial(
    request: Request,
    q: Optional[str] = Query(default=None),
    after: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    db = _get_db(request)
    rows, next_cursor = get_nodes(db, cursor=after, limit=limit, search=q)
    return templates.TemplateResponse(
        request,
        "partials/node_table_rows.html",
        {
            "nodes": [dict(r) for r in rows],
            "next_cursor": next_cursor,
            "query": q,
        },
    )


@router.get("/nodes/{node_id}", response_class=HTMLResponse)
def node_detail(
    request: Request,
    node_id: str,
):
    db = _get_db(request)
    try:
        nid = parse_node_id(node_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid node ID: {node_id!r}")

    node_info = get_node(db, nid)
    recent_traces = [dict(r) for r in get_traceroutes_for_node(db, nid)]
    default_start, default_end = _default_time_bounds()
    start_ts, end_ts = parse_time_bounds(default_start, default_end)
    connections = _group_node_connections(db, nid, start_ts=start_ts, end_ts=end_ts)
    geojson_url = _geojson_url(
        request,
        f"/api/geojson/node/{node_id_str(nid)}",
        {"start": default_start, "end": default_end, "direction": "both", "depth": "1"},
    )

    return templates.TemplateResponse(
        request,
        "node_detail.html",
        {
            "node_id": node_id_str(nid),
            "node_id_int": nid,
            "node_info": dict(node_info) if node_info else None,
            "recent_traces": recent_traces,
            "connections": connections,
            "default_start": default_start,
            "default_end": default_end,
            "geojson_url": geojson_url,
        },
    )


@router.get("/traceroutes", response_class=HTMLResponse)
def traceroutes_page(
    request: Request,
    from_node: Optional[str] = Query(default=None, alias="from"),
    to_node: Optional[str] = Query(default=None, alias="to"),
    after: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    db = _get_db(request)
    try:
        from_id = parse_node_id(from_node) if from_node else None
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid from node: {from_node!r}")
    try:
        to_id = parse_node_id(to_node) if to_node else None
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid to node: {to_node!r}")

    rows, next_cursor = get_traceroutes(db, cursor=after, limit=limit, from_id=from_id, to_id=to_id)

    return templates.TemplateResponse(
        request,
        "traceroutes_list.html",
        {
            "traceroutes": [dict(r) for r in rows],
            "next_cursor": next_cursor,
            "from_id": from_node,
            "to_id": to_node,
        },
    )


@router.get("/traceroutes/partial", response_class=HTMLResponse)
def traceroutes_partial(
    request: Request,
    from_node: Optional[str] = Query(default=None, alias="from"),
    to_node: Optional[str] = Query(default=None, alias="to"),
    after: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    db = _get_db(request)
    try:
        from_id = parse_node_id(from_node) if from_node else None
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid from node: {from_node!r}")
    try:
        to_id = parse_node_id(to_node) if to_node else None
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid to node: {to_node!r}")

    rows, next_cursor = get_traceroutes(db, cursor=after, limit=limit, from_id=from_id, to_id=to_id)

    return templates.TemplateResponse(
        request,
        "partials/trace_table_rows.html",
        {
            "traceroutes": [dict(r) for r in rows],
            "next_cursor": next_cursor,
            "from_id": from_node,
            "to_id": to_node,
        },
    )


@router.get("/traceroutes/{trace_id}", response_class=HTMLResponse)
def traceroute_detail(
    request: Request,
    trace_id: int,
):
    db = _get_db(request)
    trace_info = db.execute(
        "SELECT trace_id, from_id, to_id, first_seen_ts FROM traceroute WHERE trace_id = ? ORDER BY first_seen_ts DESC LIMIT 1",
        (trace_id,),
    ).fetchone()

    if trace_info is None:
        raise HTTPException(status_code=404, detail=f"Traceroute {trace_id} not found")

    trace_links = _enrich_trace_links(
        [dict(r) for r in get_links_for_trace(db, trace_id=trace_id, limit=500)]
    )
    geojson_url = _geojson_url(
        request,
        f"/api/geojson/trace/{trace_id}",
        {"direction": "both"},
    )

    return templates.TemplateResponse(
        request,
        "traceroute_detail.html",
        {
            "trace_id": trace_id,
            "trace_info": dict(trace_info),
            "trace_links": trace_links,
            "geojson_url": geojson_url,
        },
    )
