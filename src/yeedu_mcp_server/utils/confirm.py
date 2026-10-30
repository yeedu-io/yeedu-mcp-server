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

#: Verbs whose past tense is not formed by appending "d" or "ed". Appending
#: blindly produces "stoped" and "stop_all_jobsed", which reads as a bug in the
#: refusal and undermines the one message that has to be taken seriously.
_IRREGULAR_PAST = {
    "stop": "stopped",
    "stop_all_jobs": "stopped",
    "kill": "killed",
    "reset": "reset",
    "run": "run",
}


def _past_tense(action: str) -> str:
    if action in _IRREGULAR_PAST:
        return _IRREGULAR_PAST[action]
    return action + ("d" if action.endswith("e") else "ed")


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
    include_action: bool = True,
    action_label: Optional[str] = None,
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
        include_action: Whether the tool takes an `action` parameter. The
            `manage_*` tools do, so their retry line needs it. Tools that name
            their verb in the tool name itself do not, and printing one there
            would hand back a call signature that does not exist.
        action_label: How to say the verb in the sentence, when the API's own
            value does not read as English. `stop_all_jobs` has to stay the
            value in the retry line, but the sentence should say "stop all jobs
            on". Defaults to `action`.
    """
    # "delete" → "deleted", "unassign" → "unassigned", "stop" → "stopped".
    past = _past_tense(action)
    lines = [
        f"Not {past} — confirmation required.",
        "",
        f"This would {action_label or action} {label.lower()} {target}.",
    ]

    if record:
        lines += ["", record]

    if impact:
        lines += ["", f"Impact: {impact}"]

    args = f"{retry}, confirm=True" if retry else "confirm=True"
    signature = f"{tool}(action='{action}', {args})" if include_action else f"{tool}({args})"
    lines += [
        "",
        f"Ask the user to confirm, then repeat the call with confirm=True: "
        f"{signature}",
    ]
    return "\n".join(lines)
