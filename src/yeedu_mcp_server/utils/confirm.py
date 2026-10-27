"""Second confirmation for destructive operations.

`SYSTEM_PROMPT` already tells an agent to confirm destructive operations with
the user, but that is advice an agent can skip. This module makes it a
mechanism: a destructive action refuses to run unless the caller passes
`confirm=True`, and the refusal shows the record that would be affected, read
back from the platform first. So the user is asked twice — once by the agent
reading this text, and once by the agent having to re-issue the call.

The read-back matters as much as the gate. A delete by name or by a stale id
is the case where an agent destroys the wrong record, and seeing the row
before approving is what catches it.
"""

from typing import Any, Optional


#: The only values that count as confirmation.
_TRUE = {"true", "yes", "y", "1", "on"}


def confirmed(value: Any) -> bool:
    """Whether a confirm flag really says yes.

    Deliberately strict, and never `bool(value)`. MCP clients coerce argument
    types loosely, so a client can hand this a string: `bool("false")` is True,
    which would turn a refusal into a deletion. A bare Pydantic default (a
    FieldInfo, when a tool is called directly rather than through FastMCP) is
    truthy for the same reason. Anything that is not a real yes is a no.
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    return False


def confirmation_required(
    label: str,
    target: str,
    tool: str,
    action: str = "delete",
    *,
    record: Optional[str] = None,
    impact: Optional[str] = None,
    retry: Optional[str] = None,
) -> str:
    """Return the message that stands in for performing a destructive action.

    Args:
        label: Human name of the resource type, e.g. "Network configuration".
        target: The identifier or name the caller passed.
        tool: The tool to retry, for a copyable retry line.
        action: The destructive verb, e.g. "delete" or "dissociate".
        record: The row read back from the platform, when it could be read.
        impact: What else breaks, in one sentence.
        retry: The exact arguments to retry with, minus confirm.
    """
    # "delete" → "deleted", "unassign" → "unassigned".
    past = action + ("d" if action.endswith("e") else "ed")
    lines = [
        f"Not {past} — confirmation required.",
        "",
        f"This would {action} {label.lower()} {target}.",
    ]

    if record:
        lines += ["", record]

    if impact:
        lines += ["", f"Impact: {impact}"]

    args = f"{retry}, confirm=True" if retry else "confirm=True"
    lines += [
        "",
        f"Ask the user to confirm, then repeat the call with confirm=True: "
        f"{tool}(action='{action}', {args})",
    ]
    return "\n".join(lines)
