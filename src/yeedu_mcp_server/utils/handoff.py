"""Hand secret-bearing operations back to the user.

Some Yeedu operations require the secret itself as a request field: a cloud
service-account key, a Databricks token, a keytab, a git PAT. A tool parameter
is the wrong place for those — an MCP tool argument is produced by the model,
so the value would sit in the model's context and in the client's transcript on
its way to the platform. The API token this server uses never does that; it
lives only in `client.py`, and credentials entered through tool parameters
would be the one exception.

So the tools do not accept them. They return the exact CLI command and the UI
screen instead, and the user performs that one step themselves. Everything
around it — listing, getting, renaming, deleting, and referencing the result by
id — stays available through MCP.
"""

from typing import Optional

from yeedu_mcp_server.config import settings


#: Why the operation is not available, in one sentence the agent can relay.
_REASON = (
    "This server deliberately does not accept credential or secret values, "
    "because a tool parameter would carry the value through the AI model's "
    "context and the chat transcript"
)


def ui_link(path: str) -> Optional[str]:
    """Build a UI URL, but only if the operator configured a UI base URL.

    Returning None is the normal case. The server knows the API host, not the
    UI host, and inventing one from the API URL would both be wrong and leak
    infrastructure the system prompt says to keep quiet about.
    """
    base = settings.yeedu_ui_base_url
    if not base:
        return None
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def secret_handoff(
    what: str,
    cli_command: str,
    ui_nav: str,
    ui_path: Optional[str] = None,
    next_step: Optional[str] = None,
    role_note: Optional[str] = None,
) -> str:
    """Return instructions for the user to perform a secret-bearing step.

    Args:
        what: The thing being created, e.g. "a credential configuration".
        cli_command: The full `yeedu ...` command, with placeholders.
        ui_nav: The UI navigation path as the UI labels it.
        ui_path: URL path fragment under /tenant/<tenant-id>/. Rendered as a full
            link when a UI base URL is configured, and as the bare fragment
            otherwise, so the user can recognise the screen either way.
        next_step: The MCP tool to call once the user has finished.
        role_note: Any role restriction on the UI screen.
    """
    lines = [
        f"Creating {what} has to be done by you, in the Yeedu UI or CLI.",
        "",
        f"Reason: {_REASON}. Everything else about {what} — listing, viewing, "
        "renaming and deleting it, and referencing it by id — works through this "
        "server as normal.",
        "",
        "CLI:",
        f"  {cli_command}",
        "",
        f"UI: {ui_nav}",
    ]

    if ui_path:
        link = ui_link(f"tenant/<tenant-id>/{ui_path}")
        lines.append(f"     {link}" if link else f"     screen URL: .../tenant/<tenant-id>/{ui_path}")

    if role_note:
        lines.append(f"     ({role_note})")

    if next_step:
        lines += ["", f"Once it exists, continue here with {next_step}."]

    return "\n".join(lines)
