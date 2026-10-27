"""Parameter normalisation shared by the management tools.

FastMCP fills a tool's unset arguments from its Field defaults, so a real call
delivers None for anything the model left out. A direct Python call — the
offline check used to verify a tool without a live platform — skips that step
and delivers the bare `FieldInfo` instead, which is truthy and is not None, so
every `if x is None` guard silently inverts.

`opt` collapses that difference: it maps a bare FieldInfo to None and leaves
every real value alone, so a tool behaves the same whether it is called over
MCP or straight from Python.
"""

from typing import Any

from pydantic.fields import FieldInfo


def opt(value: Any) -> Any:
    """Return the value, or None when nothing was supplied."""
    return None if isinstance(value, FieldInfo) else value


def given(value: Any) -> bool:
    """Whether a value counts as supplied: not None, not unset, not empty."""
    value = opt(value)
    if value is None:
        return False
    if isinstance(value, (list, dict, str)) and len(value) == 0:
        return False
    return True
