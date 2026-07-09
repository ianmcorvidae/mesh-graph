from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from mesh_graph.db import (
    get_dashboard_stats,
    get_links_for_trace,
    get_node,
    get_nodes,
    get_recent_nodes,
    get_recent_traceroutes,
    get_traceroutes,
    get_traceroutes_for_node,
    parse_node_id,
)
from mesh_graph.utils import node_id_format, node_id_str

router = APIRouter()
TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES)


def _default_time_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_ts(ts: Optional[int]) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


templates.env.filters["format_ts"] = format_ts


def _get_db(request: Request):
    return request.app.state.db


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
    return templates.TemplateResponse(
        request,
        "network.html",
        {"default_start": default_start, "default_end": default_end},
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

    return templates.TemplateResponse(
        request,
        "node_detail.html",
        {
            "node_id": node_id_str(nid),
            "node_id_int": nid,
            "node_info": dict(node_info) if node_info else None,
            "recent_traces": recent_traces,
            "default_start": default_start,
            "default_end": default_end,
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

    return templates.TemplateResponse(
        request,
        "traceroute_detail.html",
        {
            "trace_id": trace_id,
            "trace_info": dict(trace_info),
            "trace_links": trace_links,
        },
    )
