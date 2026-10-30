"""What a destructive action would take down, read back before it runs.

`utils/confirm` makes a destructive action ask twice. That is enough for a
record that stands alone -- deleting a network config affects the config. It is
not enough for the lifecycle operations, because the thing being named is not
the thing being lost: stopping a cluster kills every run on it, and whoever
typed the cluster's name cannot see those runs from the name. A gate that
echoes back only "cluster onprem-s" asks the user to approve a blast radius it
declined to measure.

So these helpers read the surroundings, not just the target: how much work is
in flight on a cluster, which workspaces point at it, how long a run has been
executing before it is killed. They are best-effort by construction -- a
preview that fails must still produce a refusal, because the alternative is an
unreadable platform turning a refusal into an error an agent retries its way
past.

They also resolve a name to an id and say which id they resolved to.
`cluster_action` accepts `cluster_name`, and a name is the input most likely to
be stale, ambiguous or simply the wrong environment.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from yeedu_mcp_server.client import YeeduClient

logger = logging.getLogger(__name__)

#: Run states that represent work a stop would actually destroy. STOPPING is
#: already on its way down and is counted separately from live work.
_ACTIVE_RUN_STATUSES = ("RUNNING", "SUBMITTED")

#: Cluster states in which there is nothing running to lose.
_INERT_CLUSTER_STATUSES = ("DESTROYED", "STOPPED", "ERROR")


class AmbiguousTarget(Exception):
    """A name matched more than one cluster, so no action may be taken on it."""


def _rows(data: Any) -> List[Dict[str, Any]]:
    """Pull the row list out of either listing shape."""
    if isinstance(data, dict):
        rows = data.get("data")
        return rows if isinstance(rows, list) else []
    return data if isinstance(data, list) else []


async def _resolve_cluster(
    client: YeeduClient, cluster_id: Optional[int], cluster_name: Optional[str]
) -> Dict[str, Any]:
    """Read a cluster by id, or find exactly one by name.

    A name that matches several clusters raises rather than picking one. The
    platform permits similar names across environments, and silently acting on
    the first match is how the wrong cluster gets destroyed.
    """
    if cluster_id is not None:
        data = await client.get("/api/v1/cluster", params={"cluster_id": cluster_id})
        return data if isinstance(data, dict) else {}

    data = await client.get(
        "/api/v1/clusters/search",
        params={"cluster_name": cluster_name, "limit": 10, "pageNumber": 1,
                "enable": "true"},
    )
    matches = _rows(data)
    if len(matches) > 1:
        names = ", ".join(
            f"{m.get('cluster_id')} ({m.get('name')}, {m.get('cluster_status')})"
            for m in matches[:10])
        raise AmbiguousTarget(
            f"Name {cluster_name!r} matches {len(matches)} clusters: {names}. "
            f"Re-issue the call with the cluster_id you mean -- a name is not a "
            f"safe way to address a destructive action."
        )
    return matches[0] if matches else {}


async def cluster_blast_radius(
    cluster_id: Optional[int], cluster_name: Optional[str]
) -> Tuple[Optional[int], Optional[str], Dict[str, Any]]:
    """Read a cluster, the work in flight on it, and who points at it.

    Returns (resolved_cluster_id, rendered_record, facts). The record is None
    when nothing could be read; `facts` carries the counts so a caller can word
    the impact around them. Raises AmbiguousTarget, which must not be
    swallowed: it is a refusal in its own right.
    """
    facts: Dict[str, Any] = {"active_runs": None, "workspaces": []}

    async with YeeduClient() as client:
        record = await _resolve_cluster(client, cluster_id, cluster_name)
        if not record:
            return cluster_id, None, facts

        resolved = record.get("cluster_id") or cluster_id
        conf = record.get("cluster_conf") or {}
        machine = (conf.get("machine_type") or {}).get("name")
        status = record.get("cluster_status")
        facts["status"] = status

        lines = [
            "field,value",
            f"cluster_id,{resolved}",
            f"cluster_name,{record.get('name') or 'N/A'}",
            f"cluster_status,{status or 'N/A'}",
            f"machine_type,{machine or 'N/A'}",
        ]
        # The cluster record carries no enabled/active flag -- only
        # enable_public_ip, is_turbo_enabled and is_cuda_enabled, none of which
        # answer it -- so the preview does not claim to show one.

        # Work in flight. Skipped for a cluster that is already down: the call
        # is pure latency there, and the answer is always zero.
        if status not in _INERT_CLUSTER_STATUSES:
            try:
                stats = await client.get(
                    "/api/v1/cluster/stats", params={"cluster_id": resolved})
                counts = stats if isinstance(stats, dict) else {}
                active = sum(
                    int(counts.get(key) or 0) for key in _ACTIVE_RUN_STATUSES)
                facts["active_runs"] = active
                lines.append(f"runs_in_flight,{active}")
                if counts.get("STOPPING"):
                    lines.append(f"runs_stopping,{counts['STOPPING']}")
            except Exception as exc:  # best effort -- the gate matters more
                logger.debug("cluster stats unavailable for %s: %s", resolved, exc)
                lines.append("runs_in_flight,unknown (could not be read)")

        try:
            attached = await client.get(
                f"/api/v1/ac/cluster/{resolved}/workspaces",
                params={"limit": 20, "pageNumber": 1})
            names = [
                (row.get("workspace") or {}).get("name")
                for row in _rows(attached)
            ]
            facts["workspaces"] = [n for n in names if n]
            if facts["workspaces"]:
                lines.append(
                    f"workspaces_attached,{len(facts['workspaces'])} "
                    f"({', '.join(facts['workspaces'][:5])}"
                    f"{'…' if len(facts['workspaces']) > 5 else ''})")
        except Exception as exc:
            logger.debug("cluster workspaces unavailable for %s: %s", resolved, exc)

    return resolved, "\n".join(lines), facts


def cluster_impact(action: str, facts: Dict[str, Any]) -> str:
    """One sentence on what this lifecycle action costs, given what is running."""
    active = facts.get("active_runs")
    if active is None:
        in_flight = ("The number of runs in flight could not be read, so assume "
                     "work would be lost.")
    elif active == 0:
        in_flight = "No runs are in flight, so no work in progress is lost."
    else:
        in_flight = (f"{active} run(s) are in flight on this cluster and would be "
                     f"killed mid-execution; their partial output is not kept.")

    attached = facts.get("workspaces") or []
    shared = ""
    if len(attached) > 1:
        shared = (f" The cluster is attached to {len(attached)} workspaces "
                  f"({', '.join(attached[:5])}"
                  f"{'…' if len(attached) > 5 else ''}), so this affects more "
                  f"than one team's work.")

    if action == "destroy":
        return (f"Destroying a cluster is irreversible: the instance and anything "
                f"written to its local disks are gone, and it cannot be started "
                f"again -- a replacement has to be created. {in_flight}{shared}")
    if action == "stop":
        return (f"{in_flight} The cluster itself can be started again, and its "
                f"configuration survives.{shared}")
    if action == "stop_all_jobs":
        return (f"{in_flight} The cluster keeps running and stays available for "
                f"new runs.{shared}")
    if action == "disable":
        return (f"A disabled cluster cannot be selected for new runs, so "
                f"schedules and pipelines pointing at it start failing to "
                f"launch.{shared}")
    return in_flight + shared


async def job_run_blast_radius(
    workspace_id: int, run_id: int
) -> Tuple[Optional[str], Optional[str]]:
    """Read a Spark job run back before it is stopped.

    Returns (rendered_record, status). A terminal status is the caller's signal
    that there is nothing to stop, which is a better answer than asking someone
    to confirm a no-op.
    """
    from yeedu_mcp_server.utils.run_metrics import fetch_run

    try:
        run = await fetch_run(workspace_id, run_id)
    except Exception as exc:
        logger.debug("run %s unreadable: %s", run_id, exc)
        return None, None

    elapsed = run.get("total_sec")
    lines = [
        "field,value",
        f"run_id,{run.get('run_id')}",
        f"job_name,{run.get('job_name') or 'N/A'}",
        f"job_id,{run.get('job_id') or 'N/A'}",
        f"run_status,{run.get('status')}",
        f"cluster,{run.get('cluster_id')} ({run.get('cluster_name') or 'N/A'})",
        f"running_for_sec,{f'{elapsed:.1f}' if elapsed is not None else 'N/A'}",
    ]
    return "\n".join(lines), run.get("status")


async def notebook_run_blast_radius(
    workspace_id: int, run_id: int
) -> Tuple[Optional[str], Optional[str]]:
    """Read a notebook run back before its session is ended."""
    try:
        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}")
    except Exception as exc:
        logger.debug("notebook run %s unreadable: %s", run_id, exc)
        return None, None

    if not isinstance(data, dict) or not data:
        return None, None

    conf = data.get("notebook_conf") or data.get("job_conf") or {}
    cluster = conf.get("cluster_info") or {}
    status = data.get("run_status") or data.get("status")

    lines = [
        "field,value",
        f"run_id,{data.get('run_id') or run_id}",
        f"notebook_name,{conf.get('name') or conf.get('notebook_name') or 'N/A'}",
        f"notebook_id,{conf.get('notebook_id') or 'N/A'}",
        f"run_status,{status or 'N/A'}",
        f"cluster,{cluster.get('cluster_id')} ({cluster.get('name') or 'N/A'})",
    ]
    return "\n".join(lines), (str(status).upper() if status else None)


#: Statuses in which a run is already over and a stop would do nothing.
TERMINAL_RUN_STATUSES = {"DONE", "ERROR", "TERMINATED", "STOPPED", "KILLED"}
