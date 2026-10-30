"""Turning a run into money: what may be inferred and what may not.

The risk in this module is not a crash, it is a confident wrong number. A rate
guessed from a neighbouring family, or a family key that quietly collapses two
generations of hardware into one, produces a cost report that looks exactly
like a correct one. These tests pin the two halves of that: the name reduction
that decides which rate applies, and the refusal to apply any rate when none
is known.
"""

import json

import pytest

from yeedu_mcp_server.utils import pricing
from yeedu_mcp_server.utils.pricing import (
    PRICE_TABLE_ENV,
    PriceUnavailable,
    family_of,
    price_table,
    rate_for,
    reset_cache,
)
from yeedu_mcp_server.utils.run_metrics import breakdown, cost_estimate, diagnose


@pytest.fixture(autouse=True)
def clean_table(monkeypatch):
    """Every test starts from the seed, with no override leaking in."""
    monkeypatch.delenv(PRICE_TABLE_ENV, raising=False)
    reset_cache()
    yield
    reset_cache()


# ---------------------------------------------------------------------------
# Family reduction. Each provider names sizes differently, so each is wrong
# differently.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("instance,family", [
    ("m5.xlarge", "m5"),
    ("m6i.4xlarge", "m6i"),
    ("r6i.24xlarge", "r6i"),
])
def test_aws_sizes_reduce_to_the_family_before_the_dot(instance, family):
    assert family_of("AWS", instance) == family


@pytest.mark.parametrize("instance,family", [
    ("n2-standard-4", "n2-standard"),
    ("n2-standard-128", "n2-standard"),
    ("n2-highmem-8", "n2-highmem"),
    ("e2-standard-2", "e2-standard"),
])
def test_gcp_keeps_the_shape_and_drops_only_the_size(instance, family):
    # n2-highmem must not reduce to n2: highmem and standard cost different
    # amounts per vCPU, so merging them would misprice every memory-heavy run.
    assert family_of("GCP", instance) == family


@pytest.mark.parametrize("instance,family", [
    ("Standard_D4s_v5", "Ds_v5"),
    ("Standard_D96s_v5", "Ds_v5"),
    ("Standard_E4s_v5", "Es_v5"),
    ("Standard_D4as_v5", "Das_v5"),
    ("D4s_v3", "Ds_v3"),
])
def test_azure_keeps_the_generation_when_stripping_the_vcpu_count(instance, family):
    # The defect this pins: stripping digits without splitting the _v<N> suffix
    # first takes the 5 out of v5, so Ds_v5, Ds_v4 and Ds_v3 all collapse into
    # one family and the newest generation gets priced at the oldest's rate.
    assert family_of("AZURE", instance) == family


def test_an_unknown_provider_keeps_the_name_whole():
    assert family_of("ONPREM", "Onprem-XS-4") == "Onprem-XS-4"


def test_an_empty_instance_type_has_no_family():
    assert family_of("AWS", "") is None
    assert family_of("AWS", None) is None


# ---------------------------------------------------------------------------
# Rates. One rate per family covers every size in it, and nothing else.
# ---------------------------------------------------------------------------
def test_one_rate_covers_sizes_the_table_was_never_told_about():
    # On-demand pricing is linear within a family, so a size absent from the
    # seed is priced correctly rather than refused.
    assert rate_for("AWS", "m6i.32xlarge")["usd_per_vcpu_hour"] == \
        rate_for("AWS", "m6i.large")["usd_per_vcpu_hour"]


def test_a_seeded_rate_carries_its_basis_and_date():
    rate = rate_for("AWS", "m5.2xlarge")
    assert rate["basis_region"] == "us-east-1"
    assert rate["as_of"] == pricing.SEED_AS_OF
    assert rate["currency"] == "USD"
    assert rate["overridden"] is False
    assert rate["source"]


def test_an_unseeded_family_refuses_rather_than_borrowing_a_neighbour():
    # n2d-highmem is real hardware with no seeded rate. The wrong answer is
    # n2-highmem's rate, or n2d-standard's; the right answer is "I don't know".
    with pytest.raises(PriceUnavailable) as exc:
        rate_for("GCP", "n2d-highmem-16")
    assert "n2d-highmem" in str(exc.value)
    assert PRICE_TABLE_ENV in str(exc.value)


def test_a_non_linear_family_is_absent_on_purpose():
    with pytest.raises(PriceUnavailable):
        rate_for("AWS", "u-6tb1.56xlarge")


def test_on_prem_explains_why_no_public_price_exists():
    with pytest.raises(PriceUnavailable) as exc:
        rate_for("ONPREM", "Onprem-XS-4")
    message = str(exc.value)
    assert "on-prem" in message
    assert PRICE_TABLE_ENV in message
    assert "chargeback" in message


# ---------------------------------------------------------------------------
# The override. An org on committed-use pricing does not pay list.
# ---------------------------------------------------------------------------
def _write_table(tmp_path, payload):
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_an_override_replaces_a_seeded_rate_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(PRICE_TABLE_ENV, _write_table(tmp_path, {
        "as_of": "2026-01-01",
        "providers": {"AWS": {"m6i": {"usd_per_vcpu_hour": 0.031,
                                      "basis_region": "eu-west-1",
                                      "source": "our rate card"}}},
    }))
    reset_cache()
    rate = rate_for("AWS", "m6i.xlarge")
    assert rate["usd_per_vcpu_hour"] == 0.031
    assert rate["basis_region"] == "eu-west-1"
    assert rate["as_of"] == "2026-01-01"
    assert rate["overridden"] is True
    # Families the override did not mention still come from the seed.
    assert rate_for("AWS", "c6i.xlarge")["usd_per_vcpu_hour"] == 0.0425


def test_a_wildcard_override_prices_on_prem(tmp_path, monkeypatch):
    monkeypatch.setenv(PRICE_TABLE_ENV, _write_table(tmp_path, {
        "providers": {"onprem": {"*": {"usd_per_vcpu_hour": 0.01,
                                       "basis_region": "dc1"}}},
    }))
    reset_cache()
    rate = rate_for("OnPrem", "Onprem-XS-4")
    assert rate["usd_per_vcpu_hour"] == 0.01
    # Reported as the wildcard it is, so the answer cannot read as though this
    # instance type had a price of its own.
    assert rate["family"] == "*"


def test_a_broken_override_falls_back_to_the_seed_without_raising(
        tmp_path, monkeypatch):
    path = tmp_path / "rates.json"
    path.write_text("{not json")
    monkeypatch.setenv(PRICE_TABLE_ENV, str(path))
    reset_cache()
    assert rate_for("AWS", "m5.xlarge")["usd_per_vcpu_hour"] == 0.0480
    assert price_table()["overridden"] is False


def test_an_override_with_no_providers_object_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(PRICE_TABLE_ENV, _write_table(tmp_path, {"as_of": "2020"}))
    reset_cache()
    assert price_table()["as_of"] == pricing.SEED_AS_OF


def test_a_missing_override_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(PRICE_TABLE_ENV, str(tmp_path / "absent.json"))
    reset_cache()
    assert price_table()["overridden"] is False


# ---------------------------------------------------------------------------
# Wall-clock into cost, and wall-clock into a verdict.
# ---------------------------------------------------------------------------
_SHAPE = {"cluster_id": 357, "instance_type": "m6i.xlarge", "vcpus": 4.0,
          "provider": "AWS", "min_instances": 2.0, "max_instances": 2.0}


def test_cost_is_rate_times_vcpus_times_nodes_times_hours():
    estimate = cost_estimate(_SHAPE, 3600.0)
    assert estimate["per_node_hour"] == pytest.approx(0.192)
    assert estimate["low"] == pytest.approx(0.384)
    assert estimate["high"] == pytest.approx(0.384)


def test_an_autoscaling_range_gives_a_range_not_a_point():
    estimate = cost_estimate({**_SHAPE, "max_instances": 10.0}, 3600.0)
    assert estimate["low"] == pytest.approx(0.384)
    assert estimate["high"] == pytest.approx(1.92)


def test_an_unpriced_cluster_returns_the_reason_not_a_zero():
    estimate = cost_estimate({**_SHAPE, "provider": "OnPrem"}, 3600.0)
    assert "on-prem" in estimate["unavailable"]
    assert "low" not in estimate


def test_a_run_with_no_duration_cannot_be_costed():
    assert "unavailable" in cost_estimate(_SHAPE, None)


def test_a_cluster_with_no_vcpu_count_cannot_be_costed():
    estimate = cost_estimate({**_SHAPE, "vcpus": None}, 3600.0)
    assert "no vCPU count" in estimate["unavailable"]


def test_waiting_is_the_difference_between_the_two_timings():
    split = breakdown({"total_sec": 100.0, "execution_sec": 40.0})
    assert split["wait_sec"] == 60.0
    assert split["wait_pct"] == pytest.approx(60.0)


def test_negative_waiting_is_sampling_noise_not_a_finding():
    # The two fields are sampled separately, so a just-finished run can report
    # execution a hair above total.
    assert breakdown({"total_sec": 15.0, "execution_sec": 15.2})["wait_sec"] == 0.0


def _verdict(total, execution, status="DONE", run_id=1, cluster_id=357):
    run = {"status": status, "run_id": run_id, "cluster_id": cluster_id,
           "total_sec": total, "execution_sec": execution}
    return diagnose(run, breakdown(run))["verdict"]


def test_a_run_that_mostly_waited_is_named_startup_bound():
    assert "Startup-bound" in _verdict(600.0, 60.0)


def test_a_short_run_is_not_called_a_scheduling_problem():
    # 60% waiting, but 1.2s of it: every run pays a fixed session cost, so the
    # ratio is meaningless at this scale.
    assert "Compute-bound" in _verdict(2.0, 0.8)


def test_a_run_that_mostly_computed_is_named_compute_bound():
    assert "Compute-bound" in _verdict(15.68, 15.28)


def test_a_middling_gap_is_named_as_worth_removing_first():
    assert "Mixed" in _verdict(120.0, 85.0)


def test_a_running_run_is_reported_as_partial_rather_than_diagnosed():
    assert "still RUNNING" in _verdict(10.0, 1.0, status="RUNNING")


def test_a_failed_run_still_says_where_its_time_went():
    assert "waiting for compute" in _verdict(600.0, 60.0, status="ERROR")
    assert "executing" in _verdict(600.0, 590.0, status="ERROR")


def test_a_run_with_no_timing_pair_says_so_instead_of_guessing():
    assert "no usable timing pair" in _verdict(None, None)


def test_a_stopped_run_is_not_reported_as_a_failure():
    # STOPPED means a person ended the run, or a notebook session was shut
    # down. Calling it a failure sends the reader looking for a stack trace
    # that was never written -- seen against a notebook run stopped after 18
    # minutes of healthy execution.
    verdict = _verdict(1089.69, 1089.29, status="STOPPED")
    assert "was stopped" in verdict
    assert "Run ended STOPPED" not in verdict


def test_a_stopped_run_is_not_sent_looking_for_a_stack_trace():
    run = {"status": "STOPPED", "run_id": 1810356, "cluster_id": 357,
           "total_sec": 1089.69, "execution_sec": 1089.29}
    nxt = diagnose(run, breakdown(run))["next"]
    assert "stack trace" not in nxt
    assert "analyze_job_issue" not in nxt
    assert "not evidence of a defect" in nxt


def test_a_genuine_failure_still_points_at_the_diagnosis_tools():
    run = {"status": "ERROR", "run_id": 5, "cluster_id": 357,
           "total_sec": 600.0, "execution_sec": 590.0}
    nxt = diagnose(run, breakdown(run))["next"]
    assert "analyze_job_issue" in nxt and "stack trace" in nxt
