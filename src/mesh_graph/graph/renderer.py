from __future__ import annotations

import networkx as nx


_SUPPORTED = {"png", "svg"}


def render(G: nx.Graph, format: str) -> bytes:
    fmt = format.lower()
    if fmt not in _SUPPORTED:
        raise ValueError(f"Unsupported format '{format}'. Use one of: {', '.join(_SUPPORTED)}")

    pd = nx.nx_pydot.to_pydot(G)

    if fmt == "png":
        return pd.create_png()
    else:
        return pd.create_svg()
