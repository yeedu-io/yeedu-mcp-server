"""Run timing, its diagnosis, and what a run cost the cloud account.

Two things the platform reports per run but never interprets:

`total_run_time_sec` and `execution_time_sec`. Their difference is time the run
existed without computing -- queueing behind other runs, waiting for the
cluster to allocate a driver, starting a Spark session. The UI shows both
numbers side by side and leaves the subtraction to the reader, which is the
whole diagnosis: a run that spent nine minutes of its ten waiting does not have
slow code, and tuning the query is wasted effort.

`approx_ycu`, which is not a cost. See utils/pricing for why.
"""

import logging
from typing import Any, Dict, List, Optional

from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.errors import APIError
from yeedu_mcp_server.utils.pricing import PriceUnavailable, rate_for

logger = logging.getLogger(__name__)

#: A run is called startup-bound when waiting dominates by this share of
#: wall-clock. The absolute floor stops a 2-second run whose session took 1.2s
#: from being reported as a scheduling problem: the ratio is meaningless at
#: that scale, because every run pays a fixed session cost.
_WAIT_DOMINANT_PCT = 50.0
_WAIT_NOTABLE_PCT = 25.0
_WAIT_FLOOR_SEC = 30.0

_FAILED_STATUSES = {"ERROR", "TERMINATED", "STOPPED", "KILLED"}
_PENDING_STATUSES = {"SUBMITTED", "RUNNING", "STOPPING"}


class RunMetricsError(Exception):
    """A run or cluster could not be read, with the reason in the message."""


def _is_missing(exc: APIError) -> bool:
    return getattr(exc, "status_code", None) == 404


def _num(value: Any) -> Optional[float]:
    """Coerce an API number, treating absent and unparseable alike."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_run(workspace_id: int, run_id: int) -> Dict[str, Any]:
    """Read one run's timing record."""
    async with YeeduClient() as client:
        try:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/spark/job/run/{run_id}")
        except APIError as exc:
            if _is_missing(exc):
                raise RunMetricsError(
                    f"No run {run_id} in workspace {workspace_id}. A run_id is "
                    f"not a job_id -- use workspace_job_runs(workspace_id="
                    f"{workspace_id}) to list run ids."
                ) from exc
            raise

    if not data or (isinstance(data, dict) and data.get("error_message")):
        detail = (data or {}).get("error_message") if isinstance(data, dict) else None
        raise RunMetricsError(
            f"Run {run_id} could not be read in workspace {workspace_id}"
            + (f": {detail}" if detail else ".")
        )

    return _normalise(data, fallback_run_id=run_id)


def _normalise(row: Dict[str, Any], fallback_run_id: Any = None) -> Dict[str, Any]:
    """Flatten a run row from either run endpoint.

    Timing and status sit at the top level, but a run's identity does not: the
    job it ran and the cluster it ran on are nested under `job_conf`, and the
    single-run endpoint names the job `name` where the list endpoint names it
    `job_name`. Reading the top level alone yields a run with no cluster, which
    silently makes it uncostable.
    """
    conf = row.get("job_conf") or {}
    cluster_info = conf.get("cluster_info") or {}
    spark_type = conf.get("spark_job_type") or {}

    return {
        "run_id": row.get("run_id") or fallback_run_id,
        "job_id": conf.get("job_id") or conf.get("notebook_id"),
        "job_name": conf.get("name") or conf.get("job_name")
        or conf.get("notebook_name"),
        "job_type": spark_type.get("job_type"),
        "language": spark_type.get("language"),
        "application_id": row.get("application_id"),
        "status": (row.get("run_status") or row.get("status") or "UNKNOWN").upper(),
        "execution_sec": _num(row.get("execution_time_sec")),
        "total_sec": _num(row.get("total_run_time_sec")),
        "approx_ycu": _num(row.get("approx_ycu")),
        "cluster_id": cluster_info.get("cluster_id"),
        "cluster_name": cluster_info.get("name"),
        "cluster_status": cluster_info.get("cluster_status"),
    }


async def fetch_cluster_shape(cluster_id: int) -> Dict[str, Any]:
    """Read the compute shape a cluster bills for: instance type and count."""
    async with YeeduClient() as client:
        try:
            data = await client.get(
                "/api/v1/cluster", params={"cluster_id": cluster_id})
        except APIError as exc:
            if _is_missing(exc):
                raise RunMetricsError(
                    f"Cluster {cluster_id} does not exist."
                ) from exc
            raise

    if not data:
        raise RunMetricsError(
            f"Cluster {cluster_id} could not be read, so its instance type is "
            f"unknown and the run cannot be costed."
        )

    conf = data.get("cluster_conf") or {}
    machine = conf.get("machine_type") or {}
    cloud_env = data.get("cloud_env") or {}
    provider = (cloud_env.get("cloud_provider") or {}).get("name")
    zone = cloud_env.get("availability_zone") or {}

    return {
        "cluster_id": cluster_id,
        "name": data.get("name"),
        "status": data.get("cluster_status"),
        "instance_type": machine.get("name"),
        "vcpus": _num(machine.get("vCPUs")),
        "memory_gb": _num(machine.get("memory")),
        "provider": provider,
        "region": zone.get("region") or zone.get("name"),
        "min_instances": _num(data.get("min_instances")),
        "max_instances": _num(data.get("max_instances")),
    }


async def list_job_runs(
    workspace_id: int, job_id: int, limit: int
) -> List[Dict[str, Any]]:
    """List a job's runs, newest first, for aggregate reporting."""
    async with YeeduClient() as client:
        try:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/spark/job/runs",
                params={"job_ids": job_id, "limit": limit, "pageNumber": 1},
            )
        except APIError as exc:
            if _is_missing(exc):
                raise RunMetricsError(
                    f"No job {job_id} in workspace {workspace_id}. A job_id is "
                    f"not a run_id -- use list_job_configurations(workspace_id="
                    f"{workspace_id}) to list job ids."
                ) from exc
            raise

    rows = (data or {}).get("data") or []
    return [_normalise(row) for row in rows]


def breakdown(run: Dict[str, Any]) -> Dict[str, Any]:
    """Split a run's wall-clock into computing and waiting."""
    total = run.get("total_sec")
    execution = run.get("execution_sec")

    wait = None
    wait_pct = None
    if total is not None and execution is not None:
        # Clamp: the two fields are sampled separately, so a just-finished run
        # can report execution a hair above total. A negative wait is noise,
        # not a finding.
        wait = max(0.0, total - execution)
        if total > 0:
            wait_pct = wait / total * 100.0

    return {"total_sec": total, "execution_sec": execution,
            "wait_sec": wait, "wait_pct": wait_pct}


def diagnose(run: Dict[str, Any], split: Dict[str, Any]) -> Dict[str, str]:
    """Say which of the two halves of a run's wall-clock is the problem."""
    status = run["status"]
    wait = split["wait_sec"]
    wait_pct = split["wait_pct"]

    if status in _PENDING_STATUSES:
        return {
            "verdict": f"Run is still {status}; timings are partial.",
            "next": (f"Re-read it once it reaches a terminal status: "
                     f"get_job_run_details(run_id={run['run_id']})."),
        }

    if wait is None or wait_pct is None:
        return {
            "verdict": ("The platform reported no usable timing pair for this "
                        "run, so computing and waiting cannot be separated."),
            "next": (f"get_job_run_logs(run_id={run['run_id']}) has the "
                     f"wall-clock the driver itself recorded."),
        }

    if status in _FAILED_STATUSES:
        # Where a failed run spent its time still matters -- a failure after
        # ten minutes of waiting is usually an infrastructure failure, and one
        # after ten minutes of execution is usually the code.
        where = ("waiting for compute" if wait_pct >= _WAIT_DOMINANT_PCT
                 else "executing")
        return {
            "verdict": (f"Run ended {status} after {split['total_sec']:.1f}s, "
                        f"most of it spent {where}."),
            "next": (f"analyze_job_issue(run_id={run['run_id']}) classifies the "
                     f"failure; get_job_run_logs(run_id={run['run_id']}) has "
                     f"the stack trace."),
        }

    if wait >= _WAIT_FLOOR_SEC and wait_pct >= _WAIT_DOMINANT_PCT:
        return {
            "verdict": (f"Startup-bound: {wait:.1f}s of {split['total_sec']:.1f}s "
                        f"({wait_pct:.0f}%) was spent not computing. The code is "
                        f"not the bottleneck -- tuning the query would change "
                        f"the smaller half of this run."),
            "next": ("That gap is cluster start, queueing behind other runs, or "
                     "Spark session setup. Keep the cluster warm between runs, "
                     "or check whether runs were competing: "
                     f"workspace_job_runs(cluster_ids=[{run['cluster_id']}])."),
        }

    if wait >= _WAIT_FLOOR_SEC and wait_pct >= _WAIT_NOTABLE_PCT:
        return {
            "verdict": (f"Mixed: {wait:.1f}s of {split['total_sec']:.1f}s "
                        f"({wait_pct:.0f}%) went on startup rather than compute, "
                        f"which is worth removing before tuning the code."),
            "next": ("Reuse a warm cluster to recover that share; then profile "
                     "the execution half in the run's App Info (Spark UI)."),
        }

    return {
        "verdict": (f"Compute-bound: {split['execution_sec']:.1f}s of "
                    f"{split['total_sec']:.1f}s was actual execution, so the "
                    f"run's duration is the work it did."),
        "next": ("Speed-ups have to come from the job itself -- partitioning, "
                 "file layout, shuffle -- or from a larger machine type. The "
                 "run's App Info (Spark UI) has the per-stage breakdown."),
    }


def cost_estimate(shape: Dict[str, Any], seconds: Optional[float]) -> Dict[str, Any]:
    """Estimate what the cloud account was charged for a run's wall-clock.

    Returns a record carrying a low and a high figure, because a cluster's node
    count is reported as an autoscaling range and the platform does not record
    how many nodes a given run actually held. `unavailable` carries the reason
    when no rate applies, which is a normal answer, not an error.
    """
    if seconds is None:
        return {"unavailable": "the run reports no wall-clock duration."}

    vcpus = shape.get("vcpus")
    if not vcpus:
        return {"unavailable": (
            f"cluster {shape['cluster_id']} reports no vCPU count for instance "
            f"type {shape.get('instance_type') or 'unknown'}.")}

    try:
        rate = rate_for(shape.get("provider") or "", shape.get("instance_type") or "")
    except PriceUnavailable as exc:
        return {"unavailable": str(exc)}

    hours = seconds / 3600.0
    per_node_hour = rate["usd_per_vcpu_hour"] * vcpus

    low_nodes = shape.get("min_instances") or 1.0
    high_nodes = shape.get("max_instances") or low_nodes

    return {
        "currency": rate["currency"],
        "low": per_node_hour * low_nodes * hours,
        "high": per_node_hour * high_nodes * hours,
        "low_nodes": low_nodes,
        "high_nodes": high_nodes,
        "per_node_hour": per_node_hour,
        "usd_per_vcpu_hour": rate["usd_per_vcpu_hour"],
        "family": rate["family"],
        "basis_region": rate["basis_region"],
        "source": rate["source"],
        "as_of": rate["as_of"],
        "overridden": rate["overridden"],
        "hours": hours,
    }
