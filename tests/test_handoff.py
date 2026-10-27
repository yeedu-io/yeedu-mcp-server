"""Credential handoff: the text a secret-bearing operation returns instead.

The thing these tests protect is that the handoff explains itself. An agent
that reads "not supported" looks for another endpoint; an agent that reads why
the value cannot cross the interface, and is handed the command, relays it.
"""

import pytest

from yeedu_mcp_server.config import settings
from yeedu_mcp_server.utils.handoff import secret_handoff, ui_link


@pytest.fixture
def no_ui_base_url(monkeypatch):
    monkeypatch.setattr(settings, "yeedu_ui_base_url", None)


@pytest.fixture
def ui_base_url(monkeypatch):
    monkeypatch.setattr(settings, "yeedu_ui_base_url", "https://ui.example.com/")


def test_no_link_is_invented_when_no_ui_host_is_configured(no_ui_base_url):
    # The server knows the API host, not the UI host. Deriving one would be
    # both wrong and a leak of infrastructure detail.
    assert ui_link("tenant/<tenant-id>/secrets") is None


def test_a_link_is_built_when_the_ui_host_is_configured(ui_base_url):
    assert ui_link("/tenant/<tenant-id>/secrets") == (
        "https://ui.example.com/tenant/<tenant-id>/secrets"
    )


def test_handoff_gives_the_reason_the_command_and_the_screen(no_ui_base_url):
    message = secret_handoff(
        "a tenant secret",
        'yeedu secret create --scope tenant --name "<name>" --value "<value>"',
        "Settings → Secrets → Add Secret",
        ui_path="secrets",
        next_step="manage_secret(action='list', scope='tenant')",
    )
    assert "has to be done by you" in message
    assert "through the AI model's context and the chat transcript" in message
    assert 'yeedu secret create --scope tenant --name "<name>"' in message
    assert "UI: Settings → Secrets → Add Secret" in message
    # Without a UI base URL the screen is named, not linked, and no host leaks.
    assert "screen URL: .../tenant/<tenant-id>/secrets" in message
    assert "https://" not in message.split("CLI:")[0]
    assert "manage_secret(action='list', scope='tenant')" in message


def test_handoff_says_what_still_works_here(no_ui_base_url):
    # The agent must not conclude the whole resource is out of reach.
    message = secret_handoff(
        "a git credential",
        "yeedu git-credential create --username <user> --token <pat>",
        "Settings → Git Credentials",
    )
    for still_works in ("listing", "viewing", "renaming", "deleting",
                        "referencing it by id"):
        assert still_works in message


def test_handoff_links_the_screen_when_a_ui_host_is_configured(ui_base_url):
    message = secret_handoff(
        "an API token", "yeedu token create --description <text>",
        "Profile → Tokens", ui_path="tokens")
    assert "https://ui.example.com/tenant/<tenant-id>/tokens" in message


def test_handoff_never_contains_a_placeholder_that_looks_like_a_real_value(
        no_ui_base_url):
    message = secret_handoff(
        "a tenant secret",
        'yeedu secret create --name "<name>" --value "<value>"',
        "Settings → Secrets")
    # The command is a template for the user to fill in, so every secret-ish
    # argument must still be a placeholder.
    assert "--value \"<value>\"" in message
