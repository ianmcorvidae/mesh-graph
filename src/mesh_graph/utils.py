from __future__ import annotations


def node_id_str(nodenum: int) -> str:
    return f"!{nodenum:08x}"


def node_id_format(val: int | str) -> str:
    if isinstance(val, int):
        return f"!{val:08x}"
    return str(val)


def int_to_hex_color(n: int | float) -> str:
    n = int(n) if not isinstance(n, int) else n
    r = (n & 0xFF0000) >> 16
    g = (n & 0x00FF00) >> 8
    b = n & 0x0000FF
    return f"#{r:02x}{g:02x}{b:02x}"
