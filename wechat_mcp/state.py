"""Process-wide mutable state shared by the server and tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class State:
    runtime: Optional[Any] = None
    public_base: str = ""
    login_url: str = ""
    extra: dict = field(default_factory=dict)


STATE = State()
