"""An empty result and a broken query must not render the same.

The messages asserted here are the live wording of the platform's 404s,
captured against a running tenant. They are the only signal separating "your
filters matched nothing" from "the thing you filtered by does not exist", so a
change in how they are classified is a change in whether an SRE is told the
fleet is healthy.
"""

import pytest

from yeedu_mcp_server.utils.errors import APIError
from yeedu_mcp_server.utils.listing import (
    api_detail,
    describe_filters,
    is_empty_listing,
    no_rows,
    validate_enum,
)

CLUSTER_STATUSES = frozenset({
    "INITIATING", "RUNNING", "STOPPING", "STOPPED", "DESTROYING",
    "DESTROYED", "ERROR",
})


def err(detail: str, status: int = 404) -> APIError:
    return APIError(f"Yeedu API error: {detail} (HTTP {status})", status_code=status)


# Verbatim from the platform.
EMPTY_MESSAGES = [
    "No cluster instances found.",
    "No cluster instance matches were found for the provided cluster name: 'x'.",
    "No Spark job run were found within the provided workspace id: 348.",
    "No clusters found.",
]

REAL_ERRORS = [
    "The provided cluster config id: 999999 is not found in cluster configurations.",
    "The provided cluster config name: 'no-such-conf' is not found in cluster "
    "configurations.",
    "You don't have the sufficient permission to perform GET on /api/v1/clusters",
]


@pytest.mark.parametrize("detail", EMPTY_MESSAGES)
def test_an_empty_listing_is_recognised(detail):
    assert is_empty_listing(err(detail))


@pytest.mark.parametrize("detail", REAL_ERRORS)
def test_a_missing_reference_is_not_an_empty_listing(detail):
    """The dangerous direction: a bad filter must never read as all-clear."""
    assert not is_empty_listing(err(detail))


def test_not_found_does_not_match_on_the_word_no():
    """'is not found' must not be mistaken for 'no ... found'."""
    assert not is_empty_listing(
        err("The provided id is not found in cluster configurations."))


def test_only_a_404_is_an_empty_listing():
    for status in (400, 401, 403, 500, 502):
        assert not is_empty_listing(err("No clusters found.", status))


def test_an_unfamiliar_404_stays_an_error():
    """Unrecognised wording falls to the safe side."""
    assert not is_empty_listing(err("Something unexpected happened."))


def test_api_detail_strips_the_wrapper():
    assert api_detail(err("No clusters found.")) == "No clusters found."


def test_filters_are_reported_without_paging():
    rendered = describe_filters({
        "cluster_status": ["INITIATING", "STOPPING"],
        "enable": "true",
        "pageNumber": 1,
        "limit": 20,
        "cluster_conf_id": None,
    })
    assert rendered == "cluster_status=INITIATING, STOPPING; enable=true"
    assert "limit" not in rendered


def test_an_empty_answer_says_it_ran():
    out = no_rows("clusters", {"cluster_status": ["ERROR"], "limit": 20})
    assert "0 rows" in out
    assert "not a failed call" in out
    # The all-clear has to say what it asked, or it claims more than it checked.
    assert "cluster_status=ERROR" in out


def test_a_valid_enum_passes():
    assert validate_enum(["RUNNING", "stopped"], CLUSTER_STATUSES, "x") is None


def test_an_unknown_enum_value_names_the_allowed_set():
    out = validate_enum(["RESIZING_UP"], CLUSTER_STATUSES, "cluster status",
                        "A resizing cluster reports RUNNING.")
    assert out is not None
    assert "RESIZING_UP" in out
    assert "INITIATING" in out and "DESTROYED" in out
    assert "A resizing cluster reports RUNNING." in out


def test_no_tool_advertises_a_status_the_api_rejects():
    """Guards the defect this fixed: a description promising a rejected value.

    `list_clusters` and `get_workspace_clusters` both offered RESIZING_UP /
    RESIZING DOWN in their own parameter descriptions. The API's enum has no
    such value and rejects the request with a 400 that fails the whole call, so
    following the description was guaranteed to break the query.
    """
    import asyncio
    import json

    import yeedu_mcp_server.main  # noqa: F401  — registers every tool
    from yeedu_mcp_server.mcp_server import mcp
    from yeedu_mcp_server.tools import cluster as cluster_tools

    assert cluster_tools.CLUSTER_STATUSES == CLUSTER_STATUSES

    schemas = json.dumps([t.inputSchema for t in asyncio.run(mcp.list_tools())])
    for bogus in ("RESIZING_UP", "RESIZING_DOWN", "RESIZING UP", "RESIZING DOWN"):
        assert bogus not in schemas, f"a tool still advertises {bogus}"
