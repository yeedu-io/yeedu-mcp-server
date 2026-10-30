"""Reading a run's duration and a run's cost as answers rather than numbers.

`get_job_run_details` already returns the three fields these tools need. What
it cannot do is interpret them: it reports `total_run_time_sec` beside
`execution_time_sec` and leaves the subtraction, and the conclusion drawn from
it, to whoever is reading. `diagnose_run` does that subtraction and names the
bottleneck. `job_cost_report` converts wall-clock into cloud spend, which the
platform cannot do at all, because nothing in it knows what an instance-hour
costs.
"""

import logging
from typing import Optional

from pydantic import Field

from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.utils.params import opt
from yeedu_mcp_server.utils.pricing import PRICE_TABLE_ENV
from yeedu_mcp_server.utils.run_metrics import (
    RunMetricsError,
    breakdown,
    cost_estimate,
    diagnose,
    fetch_cluster_shape,
    fetch_run,
    list_job_runs,
)

logger = logging.getLogger(__name__)

#: Clusters are looked up once each while costing a job's run history, so the
#: report stays to a handful of calls however many runs it covers.
_MAX_RUNS = 100


def _sec(value: Optional[float]) -> str:
    return f"{value:.2f}" if value is not None else "N/A"


@mcp.tool(
    description=(
        "Explains where a run's wall-clock went: how much was Spark executing "
        "and how much was spent waiting for compute (cluster start, queueing "
        "behind other runs, Spark session setup). Use this when asked why a run "
        "was slow, before tuning any code — a run that spent most of its time "
        "waiting has no code problem, and the platform reports the two numbers "
        "side by side without ever drawing that conclusion. Returns the "
        "breakdown, a verdict naming the bottleneck, and the next tool to call."
    ),
    annotations={"readOnlyHint": True},
)
async def diagnose_run(
    workspace_id: int = Field(description="Workspace ID containing the run"),
    run_id: int = Field(description="Run ID to diagnose (not a job_id)"),
) -> str:
    try:
        run = await fetch_run(workspace_id, run_id)
    except RunMetricsError as exc:
        return f"Error: {exc}"

    split = breakdown(run)
    verdict = diagnose(run, split)

    lines = [
        "field,value",
        f"run_id,{run['run_id']}",
        f"job_name,{run.get('job_name') or 'N/A'}",
        f"job_id,{run.get('job_id') or 'N/A'}",
        f"run_status,{run['status']}",
        f"cluster,{run.get('cluster_id')} ({run.get('cluster_name') or 'N/A'})",
        f"total_run_time_sec,{_sec(split['total_sec'])}",
        f"execution_time_sec,{_sec(split['execution_sec'])}",
        f"waiting_sec,{_sec(split['wait_sec'])}",
        f"waiting_pct,"
        + (f"{split['wait_pct']:.1f}" if split["wait_pct"] is not None else "N/A"),
        f"approx_ycu,{run['approx_ycu'] if run['approx_ycu'] is not None else 'N/A'}",
    ]

    return (
        "\n".join(lines)
        + f"\n\nVerdict: {verdict['verdict']}"
        + f"\nNext: {verdict['next']}"
        + "\n\nwaiting_sec is total_run_time_sec minus execution_time_sec: time "
          "the run existed without computing. approx_ycu measures the machine's "
          "size, not its price — use job_cost_report for cloud spend."
    )


@mcp.tool(
    description=(
        "Estimates what a run, or a job's recent runs, cost the cloud account, "
        "in currency. Pass run_id for one run or job_id for that job's history. "
        "YCU cannot answer this: approx_ycu is a function of the instance's "
        "shape (vCPUs/4 + memory_GB/10), so it says how big the machine was, "
        "not what the provider charged. This multiplies the run's wall-clock by "
        "the on-demand list rate for the cluster's instance type, and reports a "
        "low and a high figure because node count is an autoscaling range. "
        "Figures are compute-only list prices for a basis region and exclude "
        "storage, egress and any committed-use discount — relay the caveats "
        "with the number, never the number alone. On-prem clusters have no "
        "public price and report that instead of a guess."
    ),
    annotations={"readOnlyHint": True},
)
async def job_cost_report(
    workspace_id: int = Field(description="Workspace ID to report on"),
    run_id: Optional[int] = Field(
        default=None, description="Cost a single run. Give this or job_id"),
    job_id: Optional[int] = Field(
        default=None,
        description="Cost this job's recent runs and total them. Give this or run_id"),
    limit: int = Field(
        default=25, ge=1, le=_MAX_RUNS,
        description="How many of the job's most recent runs to include"),
) -> str:
    run_id = opt(run_id)
    job_id = opt(job_id)
    limit = opt(limit) or 25

    if run_id is None and job_id is None:
        return ("Error: give either run_id (one run) or job_id (that job's "
                "recent runs).\nHint: workspace_job_runs(workspace_id="
                f"{workspace_id}) lists both ids.")
    if run_id is not None and job_id is not None:
        return ("Error: run_id and job_id select different reports; pass one. "
                "run_id costs that single run, job_id totals the job's history.")

    try:
        if run_id is not None:
            run = await fetch_run(workspace_id, run_id)
            runs = [run]
            heading = f"Run {run['run_id']} ({run.get('job_name') or 'unnamed'})"
        else:
            runs = await list_job_runs(workspace_id, job_id, limit)
            if not runs:
                return (f"Job {job_id} has no runs in workspace {workspace_id}, "
                        f"so there is nothing to cost.")
            name = next((r.get("job_name") for r in runs if r.get("job_name")), None)
            heading = (f"Job {job_id} ({name or 'unnamed'}) — "
                       f"{len(runs)} most recent run(s)")

        # One cluster lookup per distinct cluster, not per run.
        shapes = {}
        for record in runs:
            cluster_id = record.get("cluster_id")
            if cluster_id is not None and cluster_id not in shapes:
                shapes[cluster_id] = await fetch_cluster_shape(cluster_id)
    except RunMetricsError as exc:
        return f"Error: {exc}"

    rows = []
    low_total = 0.0
    high_total = 0.0
    ycu_total = 0.0
    currency = None
    unpriced = {}
    notes = set()

    for record in runs:
        shape = shapes.get(record.get("cluster_id")) or {}
        seconds = record.get("total_sec")
        estimate = cost_estimate(shape, seconds) if shape else {
            "unavailable": "the run records no cluster."}

        if record.get("approx_ycu") is not None:
            ycu_total += record["approx_ycu"]

        if "unavailable" in estimate:
            cost_cell = "unpriced"
            unpriced.setdefault(estimate["unavailable"], []).append(
                str(record.get("run_id")))
        else:
            currency = estimate["currency"]
            low_total += estimate["low"]
            high_total += estimate["high"]
            if estimate["low_nodes"] == estimate["high_nodes"]:
                cost_cell = f"{estimate['low']:.4f}"
            else:
                cost_cell = f"{estimate['low']:.4f}–{estimate['high']:.4f}"
            family = estimate["family"]
            notes.add(
                f"{shape.get('instance_type')}"
                + (" (provider-wide rate, not priced per family)"
                   if family == "*" else
                   f" ({family})" if family != shape.get("instance_type") else "")
                + f" at "
                f"{estimate['usd_per_vcpu_hour']:.5f} {estimate['currency']}"
                f"/vCPU-hour × {shape.get('vcpus'):.0f} vCPU = "
                f"{estimate['per_node_hour']:.4f}/node-hour; "
                f"nodes {estimate['low_nodes']:.0f}–{estimate['high_nodes']:.0f}; "
                f"list price for {estimate['basis_region']} as of "
                f"{estimate['as_of']}"
                + (" (overridden rate table)" if estimate["overridden"] else "")
                + f"; source: {estimate['source']}")

        rows.append([
            str(record.get("run_id")),
            record.get("status") or "N/A",
            _sec(seconds),
            str(shape.get("instance_type") or "N/A"),
            (f"{record['approx_ycu']}" if record.get("approx_ycu") is not None
             else "N/A"),
            cost_cell,
        ])

    out = [heading, ""]
    out.append(serialize_table(rows, headers=[
        "run_id", "status", "wall_clock_sec", "instance_type", "approx_ycu",
        f"est_cost_{currency.lower()}" if currency else "est_cost"]))

    if currency:
        total = (f"{low_total:.4f}" if abs(high_total - low_total) < 1e-9
                 else f"{low_total:.4f}–{high_total:.4f}")
        out.append(f"\nEstimated cloud compute: {total} {currency}"
                   f" across {len(rows)} run(s).")
    if ycu_total:
        out.append(f"Platform usage: {ycu_total:.2f} YCU"
                   f" (machine size, not money).")

    for reason, ids in unpriced.items():
        shown = ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")
        out.append(f"\nNot priced (run {shown}): {reason}")

    if notes:
        out.append("\nRate basis:")
        out.extend(f"  - {note}" for note in sorted(notes))

    out.append(
        "\nThese are estimates. They cover cluster compute for the run's "
        "wall-clock at on-demand list price and exclude storage, network "
        "egress, and any committed-use, reserved or negotiated discount, so an "
        "organisation with a rate card pays less. Node count comes from the "
        "cluster's autoscaling range, so a run that scaled sits between the low "
        "and high figures. A cluster shared by concurrent runs is charged once "
        "by the provider but counted against each run here, so per-run figures "
        "do not sum to a bill. Set " + PRICE_TABLE_ENV + " to a JSON rate table "
        "to report the rates actually paid.")

    return "\n".join(out)
