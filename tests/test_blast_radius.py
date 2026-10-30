"""What a refusal has to show before someone approves it.

The gate in utils/confirm asks twice. These tests cover the part that makes
the second ask meaningful for a lifecycle action: the thing being named is not
the thing being lost. "Stop cluster onprem-s" is a sentence someone approves
without hesitating; "stop cluster onprem-s, killing 3 runs in flight across 2
workspaces" is not, and the difference is the whole point of the module.
"""

import pytest

from yeedu_mcp_server.utils.blast_radius import cluster_impact
from yeedu_mcp_server.utils.confirm import _past_tense, confirmation_required


# ---------------------------------------------------------------------------
# The refusal has to read like English, because a refusal that looks broken
# gets dismissed as a bug and retried past.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action,past", [
    ("delete", "deleted"),
    ("unassign", "unassigned"),
    ("dissociate", "dissociated"),
    ("stop", "stopped"),
    ("stop_all_jobs", "stopped"),
    ("destroy", "destroyed"),
    ("disable", "disabled"),
    ("restart", "restarted"),
])
def test_the_past_tense_of_every_gated_verb(action, past):
    assert _past_tense(action) == past


def test_an_api_verb_that_is_not_english_still_reads_as_a_sentence():
    message = confirmation_required(
        "Cluster", "cluster_id=357", "cluster_action", action="stop_all_jobs",
        retry="cluster_id=357", action_label="stop all jobs on",
    )
    assert message.startswith("Not stopped — confirmation required.")
    assert "This would stop all jobs on cluster cluster_id=357." in message
    # The retry line has to carry the value the API accepts, not the prose.
    assert "action='stop_all_jobs'" in message
    assert "action='stop all jobs on'" not in message


def test_the_retry_line_prefers_the_resolved_id_over_the_name():
    # cluster_action accepts a name, and a name is the input most likely to be
    # stale or to match the wrong environment. Confirming resolves it once.
    message = confirmation_required(
        "Cluster", "cluster_id=357", "cluster_action", action="destroy",
        retry="cluster_id=357",
    )
    assert "cluster_action(action='destroy', cluster_id=357, confirm=True)" in message


# ---------------------------------------------------------------------------
# The impact sentence is written from what is actually running, not from the
# action alone.
# ---------------------------------------------------------------------------
def test_an_idle_cluster_says_no_work_is_lost():
    impact = cluster_impact("stop", {"active_runs": 0, "workspaces": ["mcp"]})
    assert "No runs are in flight" in impact


def test_a_busy_cluster_counts_the_runs_that_would_be_killed():
    impact = cluster_impact("stop", {"active_runs": 3, "workspaces": ["mcp"]})
    assert "3 run(s) are in flight" in impact
    assert "killed mid-execution" in impact


def test_an_unreadable_count_errs_towards_assuming_loss():
    # The failure mode to avoid is a refusal that reads as "nothing to lose"
    # because a stats call timed out.
    impact = cluster_impact("stop", {"active_runs": None, "workspaces": []})
    assert "could not be read" in impact
    assert "assume" in impact


def test_destroy_says_it_cannot_be_undone():
    impact = cluster_impact("destroy", {"active_runs": 0, "workspaces": []})
    assert "irreversible" in impact
    assert "cannot be started again" in impact


def test_stop_says_the_cluster_survives_so_the_two_are_not_confused():
    impact = cluster_impact("stop", {"active_runs": 0, "workspaces": []})
    assert "can be started again" in impact
    assert "irreversible" not in impact


def test_disable_names_the_consequence_for_schedules():
    impact = cluster_impact("disable", {"active_runs": 0, "workspaces": []})
    assert "cannot be selected for new runs" in impact


def test_stop_all_jobs_says_the_cluster_keeps_running():
    impact = cluster_impact("stop_all_jobs", {"active_runs": 2, "workspaces": []})
    assert "2 run(s) are in flight" in impact
    assert "keeps running" in impact


def test_a_shared_cluster_says_whose_work_is_affected():
    # One team stopping a cluster another team is using is the incident this
    # sentence exists to prevent.
    impact = cluster_impact(
        "stop", {"active_runs": 1, "workspaces": ["mcp", "analytics", "etl"]})
    assert "attached to 3 workspaces" in impact
    assert "more than one team" in impact


def test_a_single_workspace_cluster_does_not_claim_to_be_shared():
    impact = cluster_impact("stop", {"active_runs": 1, "workspaces": ["mcp"]})
    assert "more than one team" not in impact
