"""The confirmation gate: what counts as a yes, and what a refusal must say."""

import pytest
from pydantic import Field

from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed


@pytest.mark.parametrize("value", [True, "true", "True", " yes ", "y", "1", "on", 1])
def test_a_real_yes_is_accepted(value):
    assert confirmed(value) is True


@pytest.mark.parametrize(
    "value",
    [
        False, None, 0, "", "   ", "no", "n", "0", "off",
        # The cases that make bool(value) the wrong test: MCP clients coerce
        # argument types loosely, and bool("false") is True.
        "false", "False", "none", "null", "maybe",
        # A bare Pydantic default, which is what a tool sees when it is called
        # directly rather than through FastMCP. Also truthy.
        Field(default=False),
        [], {}, 2, -1, 1.0,
    ],
)
def test_anything_that_is_not_a_yes_is_a_no(value):
    assert confirmed(value) is False


def test_refusal_names_the_action_and_the_target():
    message = confirmation_required(
        "Network configuration", "67", "manage_network_config")
    assert message.startswith("Not deleted — confirmation required.")
    assert "This would delete network configuration 67." in message


def test_refusal_shows_the_record_the_impact_and_the_retry_line():
    message = confirmation_required(
        "Token", "7156", "manage_token",
        record="token_id,description\n7156,mcp",
        impact="Any client using this token stops authenticating.",
        retry="token_id=7156",
    )
    assert "7156,mcp" in message
    assert "Impact: Any client using this token stops authenticating." in message
    assert (
        "manage_token(action='delete', token_id=7156, confirm=True)" in message
    )


def test_refusal_without_a_readable_record_still_gives_a_retry_line():
    message = confirmation_required("Mount", "5", "manage_cluster_mount")
    assert "manage_cluster_mount(action='delete', confirm=True)" in message


@pytest.mark.parametrize(
    "action,past",
    [("delete", "deleted"), ("dissociate", "dissociated"),
     ("unassign", "unassigned"), ("revoke", "revoked"), ("disable", "disabled")],
)
def test_the_verb_reads_correctly_for_every_destructive_action(action, past):
    message = confirmation_required("Thing", "1", "a_tool", action=action)
    assert message.startswith(f"Not {past} — confirmation required.")
    assert f"action='{action}'" in message
