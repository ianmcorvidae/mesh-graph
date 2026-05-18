from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from pydantic import BaseModel


class NodeOut(BaseModel):
    nodenum: int
    long_name: Optional[str]
    short_name: Optional[str]
    role: Optional[str]
    last_seen_ts: Optional[int]


class TracerouteOut(BaseModel):
    trace_id: int
    from_id: int
    to_id: int
    first_seen_ts: Optional[int]
