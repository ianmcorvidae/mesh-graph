from __future__ import annotations

import networkx as nx
import pydot

from mesh_graph.observability import traced_span

_SUPPORTED = {"png", "svg", "dot"}


def _to_pydot(G: nx.Graph, *, layout_prog: str) -> pydot.Dot:
    pd = nx.nx_pydot.to_pydot(G)
    for key in ("label", "labelloc"):
        value = G.graph.get(key)
        if isinstance(value, str):
            pd.set(key, value)
    if layout_prog == "sfdp":
        pd.set("overlap", "prism")
        pd.set("sep", "+8")
        pd.set("esep", "+2")
        pd.set("outputorder", "edgesfirst")
        pd.set("K", "1.6")
        pd.set("repulsiveforce", "2")

    rank_source = G.graph.get("rank_source_node")
    if isinstance(rank_source, str) and G.has_node(rank_source):
        sub = pydot.Subgraph(graph_name="rank_source")
        sub.set("rank", "source")
        sub.add_node(pydot.Node(rank_source))
        pd.add_subgraph(sub)

    rank_sink = G.graph.get("rank_sink_node")
    if isinstance(rank_sink, str) and G.has_node(rank_sink):
        sub = pydot.Subgraph(graph_name="rank_sink")
        sub.set("rank", "sink")
        sub.add_node(pydot.Node(rank_sink))
        pd.add_subgraph(sub)

    community_nodes: dict[int, list[str]] = {}
    for pydot_node in pd.get_nodes():
        name = pydot_node.get_name()
        cid = G.nodes[name].get("community_id") if G.has_node(name) else None
        if cid is not None:
            community_nodes.setdefault(cid, []).append(name)

    if community_nodes:
        pd.set("compound", "true")
        _COMMUNITY_COLORS = [
            "#ff6b6b",
            "#4ecdc4",
            "#45b7d1",
            "#96ceb4",
            "#ffeaa7",
            "#dfe6e9",
            "#ff9ff3",
            "#54a0ff",
        ]
        community_labels = G.graph.get("community_labels", {})
        for cid, members in community_nodes.items():
            sub = pydot.Subgraph(graph_name=f"cluster_{cid}")
            sub.set("label", community_labels.get(cid, f"Community {cid}"))
            sub.set("style", "rounded")
            sub.set("color", _COMMUNITY_COLORS[cid % len(_COMMUNITY_COLORS)])
            for name in members:
                sub.add_node(pydot.Node(name))
            pd.add_subgraph(sub)

    return pd


_SVG_INJECTION = b"""
  <script xlink:href="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js" />
  <script>
    <![CDATA[
      if (window === window.top) {
        window.addEventListener('load', function() {
          var svgEl = document.documentElement;
          // Force the standalone SVG to act as a fullscreen viewport
          svgEl.style.width = '100vw';
          svgEl.style.height = '100vh';
          svgEl.style.margin = '0';
          svgEl.style.overflow = 'hidden';
          svgEl.style.touchAction = 'none';
          if (document.body) document.body.style.margin = '0';
          
          var pz = svgPanZoom(svgEl, {
            zoomEnabled: true,
            controlIconsEnabled: true,
            fit: true,
            center: true,
            contain: true,
            minZoom: 0.01,
            maxZoom: 10
          });
          
          var initialScale = 1;
          var initialPinchDistance = 0;
          var lastPanX = 0;
          var lastPanY = 0;
          
          svgEl.addEventListener('touchstart', function(e) {
            if (e.touches.length === 1) {
              lastPanX = e.touches[0].clientX;
              lastPanY = e.touches[0].clientY;
            } else if (e.touches.length === 2) {
              e.preventDefault();
              var dx = e.touches[0].clientX - e.touches[1].clientX;
              var dy = e.touches[0].clientY - e.touches[1].clientY;
              initialPinchDistance = Math.sqrt(dx*dx + dy*dy);
              if (initialPinchDistance < 10) initialPinchDistance = 10;
              initialScale = pz.getZoom();
            }
          }, { passive: false });
          
          svgEl.addEventListener('touchmove', function(e) {
            if (e.touches.length === 1) {
              e.preventDefault();
              var dx = e.touches[0].clientX - lastPanX;
              var dy = e.touches[0].clientY - lastPanY;
              lastPanX = e.touches[0].clientX;
              lastPanY = e.touches[0].clientY;
              pz.panBy({x: dx, y: dy});
            } else if (e.touches.length === 2 && initialPinchDistance > 0) {
              e.preventDefault();
              var dx = e.touches[0].clientX - e.touches[1].clientX;
              var dy = e.touches[0].clientY - e.touches[1].clientY;
              var dist = Math.sqrt(dx*dx + dy*dy);
              var centerX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
              var centerY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
              pz.zoomAtPoint(initialScale * (dist / initialPinchDistance), {x: centerX, y: centerY});
            }
          }, { passive: false });
          
          svgEl.addEventListener('touchend', function(e) {
            if (e.touches.length < 2) {
              initialPinchDistance = 0;
            }
            if (e.touches.length === 1) {
              lastPanX = e.touches[0].clientX;
              lastPanY = e.touches[0].clientY;
            }
          }, { passive: false });
          
          var preventScroll = function(e) { e.preventDefault(); };
          svgEl.addEventListener('wheel', preventScroll, { passive: false });
          svgEl.addEventListener('mousewheel', preventScroll, { passive: false });
          svgEl.addEventListener('DOMMouseScroll', preventScroll, { passive: false });
        });
      }
    ]]>
  </script>
</svg>"""

def render(G: nx.Graph, format: str, *, layout_prog: str = "dot") -> bytes:
    fmt = format.lower()
    if fmt not in _SUPPORTED:
        raise ValueError(f"Unsupported format '{format}'. Use one of: {', '.join(_SUPPORTED)}")

    with traced_span(
        "renderer.to_pydot",
        warn_ms=1000,
        attributes={"graph.node_count": len(G.nodes), "graph.edge_count": len(G.edges)},
    ):
        pd = _to_pydot(G, layout_prog=layout_prog)

    if fmt == "dot":
        return pd.to_string().encode()

    with traced_span(
        f"renderer.graphviz.create_{fmt}", warn_ms=5000, attributes={"graphviz.prog": layout_prog}
    ):
        if fmt == "png":
            return pd.create_png(prog=layout_prog)
        svg_bytes = pd.create_svg(prog=layout_prog)
        return svg_bytes.replace(b"</svg>", _SVG_INJECTION)
