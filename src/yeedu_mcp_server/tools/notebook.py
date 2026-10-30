"""Notebook configuration, run, kernel, code execution, and log tools for Yeedu MCP Server.

create_notebook, start_notebook, and start_notebook_kernel live in spark_job.py.
"""

import asyncio
import json
import ssl
import uuid
from datetime import datetime, timezone
from typing import List, Optional
import websockets
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.config import SSL_VERIFY_YEEDU, YEEDU_API_BASE_URL
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.blast_radius import (
    TERMINAL_RUN_STATUSES,
    notebook_run_blast_radius,
)
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed


def _ident(notebook_id: Optional[int], notebook_name: Optional[str]) -> dict:
    """Build the notebook_id/notebook_name query params (either one identifies a notebook)."""
    params = {}
    if notebook_id is not None:
        params["notebook_id"] = notebook_id
    if notebook_name:
        params["notebook_name"] = notebook_name
    return params


def _as_json(data) -> str:
    return data if isinstance(data, str) else json.dumps(data, indent=2, default=str)


@mcp.tool(
    description="Lists or searches notebooks in a workspace with optional filters (name, enabled state, cluster IDs, language, last run status, has_run, creator/modifier). Returns a table: notebook_id, notebook_name, language, cluster, last_run_id, last_run_status, created_by, modified_by with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_notebooks(
    workspace_id: int = Field(description="Workspace ID containing the notebooks"),
    notebook_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the notebook to search for"),
    enable: Optional[bool] = Field(
        default=True, description="Filter by state: True=active, False=disabled, None=all"),
    has_run: Optional[bool] = Field(
        default=None, description="True=only notebooks that have run, False=never run, None=all"),
    cluster_ids: Optional[List[int]] = Field(
        default=None, description="Set of cluster instance IDs to filter on"),
    job_type_langs: Optional[List[str]] = Field(
        default=None, description="Filter by languages: Python3, Scala, SQL"),
    last_run_status: Optional[List[str]] = Field(
        default=None, description="Filter by run status: SUBMITTED, RUNNING, DONE, ERROR, TERMINATED, STOPPING, STOPPED"),
    created_by_ids: Optional[List[int]] = Field(
        default=None, description="Set of user IDs to filter by creator"),
    modified_by_ids: Optional[List[int]] = Field(
        default=None, description="Set of user IDs to filter by modifier"),
    limit: int = Field(default=20, ge=1, le=100, description="Number of notebooks to retrieve"),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """List/search notebooks in a workspace"""
    try:
        params = {"pageNumber": page, "limit": limit}
        if enable is not None:
            params["enable"] = str(enable).lower()
        if isinstance(has_run, bool):
            params["has_run"] = str(has_run).lower()
        for key, value in (("cluster_ids", cluster_ids), ("job_type_langs", job_type_langs),
                           ("last_run_status", last_run_status), ("created_by_ids", created_by_ids),
                           ("modified_by_ids", modified_by_ids)):
            if value:
                params[key] = value

        async with YeeduClient() as client:
            if notebook_name:
                params["notebook_name"] = notebook_name
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/notebooks/search", params=params)
            else:
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/notebooks", params=params)

        if not data or "data" not in data:
            return f"No notebooks found for workspace ID {workspace_id} or error in API response."

        rows = []
        for nb in data["data"]:
            cluster = nb.get("cluster_info") or {}
            last_run = nb.get("last_notebook_run") or nb.get("last_job_run") or {}
            job_type = nb.get("spark_job_type") or {}
            rows.append([
                nb.get("notebook_id", nb.get("job_id", "N/A")),
                nb.get("notebook_name", nb.get("name", nb.get("job_name", "N/A"))),
                job_type.get("language", "N/A"),
                cluster.get("name", "N/A"),
                last_run.get("run_id", "N/A"),
                last_run.get("run_status", "N/A"),
                (nb.get("created_by") or {}).get("username", "N/A"),
                (nb.get("modified_by") or {}).get("username", "N/A"),
            ])
        table = serialize_table(rows, headers=[
            "notebook_id", "notebook_name", "language", "cluster_name",
            "last_run_id", "last_run_status", "created_by", "modified_by"])
        pagination = data.get("pagination") or {}
        if pagination:
            table += f"\n{_as_json(pagination)}"
        return table

    except Exception as e:
        return f"Error listing notebooks: {str(e)}. HINT: Ensure workspace_id is valid."


@mcp.tool(
    description="Gets the configuration of a notebook by notebook_id or notebook_name: cluster, concurrency, Spark conf/packages/jars/files, resources. Returns the notebook details as JSON.",
    annotations={"readOnlyHint": True}
)
async def get_notebook(
    workspace_id: int = Field(description="Workspace ID containing the notebook"),
    notebook_id: Optional[int] = Field(default=None, description="Notebook ID"),
    notebook_name: Optional[str] = Field(
        default=None, description="Notebook name (if notebook_id not provided)"),
) -> str:
    """Get a notebook configuration"""
    try:
        if notebook_id is None and not notebook_name:
            return "Error: Either notebook_id or notebook_name is required."
        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook",
                params=_ident(notebook_id, notebook_name))
        return _as_json(data)
    except Exception as e:
        return f"Error getting notebook: {str(e)}. HINT: Ensure the notebook exists in this workspace."


@mcp.tool(
    description="Updates a notebook configuration by notebook_id or notebook_name. Only provided fields change. Editable: notebook_name, cluster (cluster_id/cluster_name), max_concurrency, Spark conf/packages/jars/files, driver/executor resources, should_append_params. Returns the updated notebook details.",
    annotations={"idempotentHint": True}
)
async def edit_notebook(
    workspace_id: int = Field(description="Workspace ID containing the notebook"),
    notebook_id: Optional[int] = Field(default=None, description="ID of the notebook to edit"),
    current_notebook_name: Optional[str] = Field(
        default=None, description="Current notebook name (if notebook_id not provided)"),
    notebook_name: Optional[str] = Field(default=None, description="New notebook name"),
    cluster_id: Optional[int] = Field(default=None, description="Cluster ID to attach"),
    cluster_name: Optional[str] = Field(default=None, description="Cluster name to attach"),
    max_concurrency: Optional[int] = Field(default=None, ge=0, description="Maximum concurrent runs"),
    conf: Optional[List[str]] = Field(default=None, description="Spark conf key-value pairs"),
    packages: Optional[List[str]] = Field(default=None, description="List of Spark packages"),
    jars: Optional[List[str]] = Field(default=None, description="List of jar files"),
    files: Optional[List[str]] = Field(default=None, description="List of file paths"),
    driver_memory: Optional[str] = Field(default=None, description="Driver memory"),
    executor_memory: Optional[str] = Field(default=None, description="Executor memory"),
    driver_cores: Optional[int] = Field(default=None, ge=1, description="Driver cores"),
    executor_cores: Optional[int] = Field(default=None, ge=1, description="Cores per executor"),
    num_executors: Optional[int] = Field(default=None, ge=1, description="Number of executors"),
    total_executor_cores: Optional[int] = Field(default=None, ge=1, description="Total executor cores"),
    should_append_params: Optional[bool] = Field(
        default=None, description="Append notebook Spark configuration to the cluster-level configuration"),
) -> str:
    """Edit a notebook configuration"""
    try:
        if notebook_id is None and not current_notebook_name:
            return "Error: Either notebook_id or current_notebook_name is required."
        body = {
            "notebook_name": notebook_name, "cluster_id": cluster_id,
            "cluster_name": cluster_name, "max_concurrency": max_concurrency,
            "conf": conf, "packages": packages, "jars": jars, "files": files,
            "driver_memory": driver_memory, "executor_memory": executor_memory,
            "driver_cores": driver_cores, "executor_cores": executor_cores,
            "num_executors": num_executors, "total_executor_cores": total_executor_cores,
            "should_append_params": should_append_params,
        }
        body = {k: v for k, v in body.items() if v is not None}
        if not body:
            return "Error: Provide at least one field to update."
        async with YeeduClient() as client:
            data = await client.put(
                f"/api/v1/workspace/{workspace_id}/notebook",
                params=_ident(notebook_id, current_notebook_name), json=body)
        return f"Notebook updated successfully: {_as_json(data)}"
    except Exception as e:
        return f"Error editing notebook: {str(e)}. HINT: Ensure the notebook exists and cluster details are valid."


@mcp.tool(
    description="Enables or disables a notebook by notebook_id or notebook_name. action must be 'enable' or 'disable'. Disabling hides the notebook from active listings.",
    annotations={"idempotentHint": True}
)
async def set_notebook_state(
    workspace_id: int = Field(description="Workspace ID containing the notebook"),
    action: str = Field(description="Action: enable or disable"),
    notebook_id: Optional[int] = Field(default=None, description="Notebook ID"),
    notebook_name: Optional[str] = Field(
        default=None, description="Notebook name (if notebook_id not provided)"),
) -> str:
    """Enable or disable a notebook"""
    try:
        action = action.lower()
        if action not in ("enable", "disable"):
            return f"Error: Invalid action '{action}'. Must be: enable, disable."
        if notebook_id is None and not notebook_name:
            return "Error: Either notebook_id or notebook_name is required."
        async with YeeduClient() as client:
            data = await client.put(
                f"/api/v1/workspace/{workspace_id}/notebook/{action}",
                params=_ident(notebook_id, notebook_name))
        return f"Notebook {action}d successfully: {_as_json(data)}"
    except Exception as e:
        return f"Error trying to {action} notebook: {str(e)}. HINT: Ensure the notebook exists in this workspace."


@mcp.tool(
    description="Clones a notebook into a new notebook (same configuration and content) by notebook_id or notebook_name. Optional clone_file_path sets the directory of the new notebook file. Returns the new notebook details."
)
async def clone_notebook(
    workspace_id: int = Field(description="Workspace ID containing the notebook"),
    new_notebook_name: str = Field(min_length=1, description="Name of the cloned notebook"),
    notebook_id: Optional[int] = Field(default=None, description="ID of the notebook to clone"),
    notebook_name: Optional[str] = Field(
        default=None, description="Name of the notebook to clone (if notebook_id not provided)"),
    clone_file_path: Optional[str] = Field(
        default=None, description="Directory path for the cloned notebook file"),
) -> str:
    """Clone a notebook"""
    try:
        if notebook_id is None and not notebook_name:
            return "Error: Either notebook_id or notebook_name is required."
        params = _ident(notebook_id, notebook_name)
        params["new_notebook_name"] = new_notebook_name
        if clone_file_path:
            params["clone_file_path"] = clone_file_path
        async with YeeduClient() as client:
            data = await client.post(
                f"/api/v1/workspace/{workspace_id}/notebook/clone", params=params)
        return f"Notebook cloned successfully: {_as_json(data)}"
    except Exception as e:
        return f"Error cloning notebook: {str(e)}. HINT: Ensure new_notebook_name is unique in the workspace."


@mcp.tool(
    description="Lists or searches notebook runs in a workspace with optional filters (notebook name/IDs, run status, cluster IDs, language, background runs, active only). Returns a table: run_id, notebook_id, notebook_name, status, cluster, created_by with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_notebook_runs(
    workspace_id: int = Field(description="Workspace ID containing the notebook runs"),
    notebook_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the notebook to search runs for"),
    notebook_ids: Optional[List[int]] = Field(
        default=None, description="Set of notebook IDs to filter on"),
    run_status: Optional[str] = Field(
        default=None, description="Filter by run status: SUBMITTED, RUNNING, DONE, ERROR, TERMINATED, STOPPING, STOPPED"),
    cluster_ids: Optional[List[int]] = Field(
        default=None, description="Set of cluster instance IDs to filter on"),
    job_type_langs: Optional[List[str]] = Field(
        default=None, description="Filter by languages: Python3, Scala, SQL"),
    is_background: Optional[bool] = Field(
        default=None, description="Filter background runs"),
    is_active: Optional[bool] = Field(
        default=None, description="True=only active (not finished) runs"),
    limit: int = Field(default=20, ge=1, le=100, description="Number of runs to retrieve"),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """List/search notebook runs"""
    try:
        params = {"pageNumber": page, "limit": limit}
        if notebook_ids:
            params["notebook_ids"] = notebook_ids
        if run_status:
            params["run_status"] = run_status
        if cluster_ids:
            params["cluster_ids"] = cluster_ids
        if job_type_langs:
            params["job_type_langs"] = job_type_langs
        if isinstance(is_background, bool):
            params["is_background"] = str(is_background).lower()
        if isinstance(is_active, bool):
            params["isActive"] = str(is_active).lower()

        async with YeeduClient() as client:
            if notebook_name:
                params["notebook_name"] = notebook_name
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/notebook/runs/search", params=params)
            else:
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/notebook/runs", params=params)

        if not data or "data" not in data:
            return f"No notebook runs found for workspace ID {workspace_id} or error in API response."

        rows = []
        for run in data["data"]:
            # Run rows nest the notebook and cluster details under notebook_conf
            conf = run.get("notebook_conf") or {}
            cluster = conf.get("cluster_info") or run.get("cluster_info") or {}
            rows.append([
                run.get("run_id", "N/A"),
                conf.get("notebook_id", run.get("notebook_id", "N/A")),
                conf.get("notebook_name", run.get("notebook_name", "N/A")),
                run.get("run_status", run.get("status", "N/A")),
                cluster.get("name", run.get("cluster_name", "N/A")),
                (run.get("created_by") or {}).get("username", "N/A"),
            ])
        table = serialize_table(rows, headers=[
            "run_id", "notebook_id", "notebook_name", "status", "cluster_name", "created_by"])
        pagination = data.get("pagination") or {}
        if pagination:
            table += f"\n{_as_json(pagination)}"
        return table

    except Exception as e:
        return f"Error listing notebook runs: {str(e)}. HINT: Ensure workspace_id is valid."


@mcp.tool(
    description="Gets the details and status of a specific notebook run by run_id (not notebook_id). Returns the run details as JSON.",
    annotations={"readOnlyHint": True}
)
async def get_notebook_run(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID"),
) -> str:
    """Get notebook run details/status"""
    try:
        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}")
        return _as_json(data)
    except Exception as e:
        return f"Error getting notebook run: {str(e)}. HINT: Ensure run_id is a notebook run ID in this workspace."


@mcp.tool(
    description=(
        "Stops (kills) a running notebook run by run_id (not notebook_id). This "
        "ends the notebook session and its kernel; unsaved cell state is lost. "
        "Requires confirm=True: called without it, the tool stops nothing and "
        "returns the run and the notebook it belongs to, so the user can confirm "
        "it is the right session before its in-memory state is discarded."
    ),
    annotations={"destructiveHint": True}
)
async def stop_notebook_run(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID to stop"),
    confirm: bool = Field(
        default=False,
        description=(
            "Must be True to stop the run. Set it only after the user has seen "
            "this specific notebook run."
        ),
    ),
) -> str:
    """Stop a notebook run"""
    try:
        if not confirmed(confirm):
            record, status = await notebook_run_blast_radius(workspace_id, run_id)
            if status and status in TERMINAL_RUN_STATUSES:
                return (f"Notebook run {run_id} is already {status}, so there is "
                        f"nothing to stop.")
            return confirmation_required(
                "Notebook run",
                f"run_id={run_id}",
                "stop_notebook_run",
                action="stop",
                record=record,
                impact=(
                    "This ends the notebook session and kills its kernel. Every "
                    "variable and DataFrame held in memory is lost, along with any "
                    "cell output not already saved, and a cell executing now is "
                    "cut off. The notebook file itself is untouched and can be run "
                    "again."
                ),
                retry=f"workspace_id={workspace_id}, run_id={run_id}",
                include_action=False,
            )

        async with YeeduClient() as client:
            data = await client.post(
                f"/api/v1/workspace/{workspace_id}/notebook/run/kill/{run_id}")
        return f"Notebook run stop requested: {_as_json(data)}"
    except Exception as e:
        return f"Error stopping notebook run: {str(e)}. HINT: Ensure the run exists and is not already finished."


@mcp.tool(
    description="Retrieves the last N lines of STDOUT or STDERR logs for a notebook run. Returns raw log text.",
    annotations={"readOnlyHint": True}
)
async def get_notebook_run_logs(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID"),
    log_type: str = Field(description="Type of logs to retrieve: STDOUT or STDERR"),
    last_n_lines: int = Field(
        default=100, ge=1, le=1000, description="Number of lines to retrieve from the end of the file"),
) -> str:
    """Retrieve notebook run logs"""
    try:
        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}/log/{log_type.lower()}",
                params={"last_n_lines": last_n_lines},
                headers={"Accept": "text/plain"},
            )
        if not data:
            return f"No {log_type} logs available for notebook run {run_id}."
        if isinstance(data, dict):
            for key in ("last_n_lines", "output", "logs", "log"):
                if isinstance(data.get(key), str):
                    return data[key]
        return _as_json(data)
    except Exception as e:
        return f"Error getting notebook logs: {str(e)}. HINT: Verify that log_type is STDOUT or STDERR and the run has started."


@mcp.tool(
    description="Gets the kernel status of a notebook run by run_id (not notebook_id).",
    annotations={"readOnlyHint": True}
)
async def get_notebook_kernel_status(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID"),
) -> str:
    """Get notebook kernel status"""
    try:
        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}/kernel/status")
        return f"Notebook kernel status: {_as_json(data)}"
    except Exception as e:
        return f"Error getting notebook kernel status: {str(e)}. HINT: The kernel must have been started for this run (start_notebook_kernel)."


@mcp.tool(
    description=(
        "Interrupts or restarts the kernel of a notebook run by run_id (not "
        "notebook_id). action must be 'interrupt' (stop the currently executing "
        "cell) or 'restart' (restart the kernel; in-memory variables are lost). "
        "restart requires confirm=True and returns the run for review when called "
        "without it; interrupt is not gated, because stopping one cell is the "
        "ordinary way to recover a stuck notebook and keeps all session state."
    )
)
async def notebook_kernel_action(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID"),
    action: str = Field(description="Action: interrupt or restart"),
    confirm: bool = Field(
        default=False,
        description=(
            "Must be True for action='restart'. Set it only after the user has "
            "seen this specific notebook run. Not needed for interrupt."
        ),
    ),
) -> str:
    """Interrupt or restart a notebook kernel"""
    try:
        action = action.lower()
        if action not in ("interrupt", "restart"):
            return f"Error: Invalid action '{action}'. Must be: interrupt, restart."

        # Only restart is gated. Interrupt stops the running cell and leaves
        # every variable in place, so requiring confirmation for it would add
        # friction to the standard remedy for a wedged notebook.
        if action == "restart" and not confirmed(confirm):
            record, status = await notebook_run_blast_radius(workspace_id, run_id)
            # A run that is over has no kernel. Asking someone to approve the
            # loss of session state that ended when the run did invites them to
            # confirm a call whose only outcome is an error.
            if status and status in TERMINAL_RUN_STATUSES:
                return (
                    f"Notebook run {run_id} is already {status}, so there is no "
                    f"live kernel to restart and no session state left to lose."
                    + (f"\n\n{record}" if record else "")
                    + "\n\nTo work in this notebook again, start a new run "
                      "with start_notebook."
                )
            return confirmation_required(
                "Notebook kernel for",
                f"run_id={run_id}",
                "notebook_kernel_action",
                action="restart",
                record=record,
                impact=(
                    "Restarting discards every variable, DataFrame and cached "
                    "result held in the kernel, so the notebook has to be re-run "
                    "from the top to get back to its current state. Saved cell "
                    "output and the notebook file are untouched. To stop a single "
                    "stuck cell without losing session state, use "
                    "action='interrupt' instead."
                ),
                retry=f"workspace_id={workspace_id}, run_id={run_id}",
            )
        async with YeeduClient() as client:
            data = await client.post(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}/kernel/{action}")
        return f"Notebook kernel {action} requested: {_as_json(data)}"
    except Exception as e:
        return f"Error trying to {action} notebook kernel: {str(e)}. HINT: The notebook run must be RUNNING with a started kernel."


_MAX_OUTPUT_CHARS = 20000
_MAX_NOTEBOOK_BYTES = 30 * 1024 * 1024


def _ws_ssl_context(ws_url: str):
    """TLS settings for the kernel websocket, honouring the server's SSL_VERIFY_YEEDU setting."""
    if not ws_url.startswith("wss://"):
        return None
    if SSL_VERIFY_YEEDU is False:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if isinstance(SSL_VERIFY_YEEDU, str):
        return ssl.create_default_context(cafile=SSL_VERIFY_YEEDU)
    return ssl.create_default_context()


_CONNECTABLE_KERNEL_STATES = ("idle", "busy", "starting")


async def _wait_for_kernel(client, workspace_id: int, run_id: int, timeout_sec: int = 90) -> str:
    """Start the kernel if needed and wait until it can be connected to. Returns the last status.

    A kernel that has been launched reports "starting" until a client connects to its
    websocket, and this server is that client - so "starting" is a state to connect in,
    not one to wait out. Waiting for "idle" first would deadlock: the status cannot leave
    "starting" while nothing connects. The kernel buffers the execute request if its Spark
    session is still coming up, and a kernel that never comes up is caught by the caller's
    own timeout instead.
    """
    base = f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}/kernel"
    await client.post(f"{base}/startOrGetStatus")
    status = "unknown"
    for _ in range(max(1, timeout_sec // 3)):
        info = await client.get(f"{base}/status")
        info = info if isinstance(info, dict) else {}
        status = str(info.get("kernel_status") or info.get("execution_state")
                     or info.get("status") or "unknown").lower()
        if status in _CONNECTABLE_KERNEL_STATES:
            return status
        await asyncio.sleep(3)
    return status


def _kernel_ws_url(token: str, workspace_id: int, run_id: int) -> str:
    """Websocket URL for a notebook run's kernel (the session token is part of the path)."""
    return (
        f"{YEEDU_API_BASE_URL.rstrip('/')}/api/v1/workspace/{workspace_id}"
        f"/notebook/run/{run_id}/kernel/ws/yeedu_session/{token}"
    ).replace("https://", "wss://", 1).replace("http://", "ws://", 1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run_time_label(start_time: str, end_time: str) -> str:
    """Human-readable cell run time, matching the format the Yeedu UI expects."""
    try:
        if not start_time or not end_time:
            return ""
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        total = (end_dt - start_dt).total_seconds()
        if total < 0:
            return ""
        hours, minutes = int(total // 3600), int((total % 3600) // 60)
        seconds, millis = int(total % 60), int((total - int(total)) * 1000)
        if hours >= 1:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes >= 1:
            return f"{minutes}m {seconds}s"
        if seconds >= 1:
            return f"{seconds}s {millis}ms"
        return f"{millis}ms"
    except Exception:
        return ""


def _execute_request(code: str, session_id: str, msg_id: str) -> str:
    """A Jupyter execute_request for the kernel's shell channel."""
    return json.dumps({
        "header": {
            "msg_type": "execute_request",
            "msg_id": msg_id,
            "username": "yeedu-mcp",
            "session": session_id,
            "date": _now_iso(),
            "version": "5.3",
        },
        "metadata": {},
        "content": {
            "code": code, "silent": False, "store_history": True,
            "user_expressions": {}, "allow_stdin": False, "stop_on_error": True,
        },
        "buffers": [],
        "parent_header": {},
        "channel": "shell",
    })


async def _collect_outputs(ws, msg_id: str):
    """Collect one execute_request's outputs until the kernel returns to idle.

    Returns (outputs, error, reply_status); outputs use Yeedu's notebook cell
    output shape so they can be saved back into the notebook unchanged.
    """
    outputs: List[dict] = []
    error: Optional[str] = None
    reply_status: Optional[str] = None
    while True:
        msg = json.loads(await ws.recv())
        if (msg.get("parent_header") or {}).get("msg_id") != msg_id:
            continue
        msg_type = msg.get("msg_type", "")
        content = msg.get("content") or {}
        if msg_type == "stream":
            outputs.append({"output_type": "text", "Celloutput": content.get("text", "")})
        elif msg_type in ("execute_result", "display_data"):
            data = content.get("data") or {}
            image = data.get("image/png")
            if image:
                outputs.append({"output_type": "image",
                                "Celloutput": f"data:image/png;base64,{image}"})
            text = data.get("text/plain")
            if text:
                outputs.append({"output_type": "text", "Celloutput": text})
        elif msg_type == "error":
            error = f"{content.get('ename', '')}: {content.get('evalue', '')}".strip(": ")
            outputs.append({"output_type": "text",
                            "Celloutput": "\n".join(content.get("traceback") or [])})
        elif msg_type == "execute_reply":
            reply_status = content.get("status")
        elif msg_type == "status" and content.get("execution_state") == "idle":
            return outputs, error, reply_status


def _outputs_as_text(outputs: List[dict]) -> str:
    parts = []
    for out in outputs:
        if out.get("output_type") == "image":
            parts.append("[image output - view in Yeedu UI]\n")
        else:
            parts.append(out.get("Celloutput", ""))
    return "".join(parts)


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[output truncated]"


def _fit_notebook_size(notebook: dict) -> bool:
    """Drop outputs from the end until the notebook fits Yeedu's 30MB save limit."""
    def size() -> int:
        return len(json.dumps(notebook, default=str).encode("utf-8"))
    if size() <= _MAX_NOTEBOOK_BYTES:
        return False
    for cell in reversed(notebook.get("cells") or []):
        while cell.get("outputs"):
            cell["outputs"].pop()
            if not cell["outputs"]:
                cell["outputs"] = [{"output_type": "text",
                                    "Celloutput": "Output removed to comply with the 30MB notebook limit"}]
            if size() <= _MAX_NOTEBOOK_BYTES:
                return True
            if cell["outputs"] and cell["outputs"][0].get("Celloutput", "").startswith("Output removed"):
                cell["outputs"] = []
                break
    return True


@mcp.tool(
    description="Executes Python/Scala/SQL code (matching the notebook language) in the kernel of a RUNNING notebook run and returns its output (stdout/stderr, results, errors). The kernel is started automatically if needed. run_id is the notebook run ID from start_notebook, not the notebook_id. State persists in the kernel between calls. This runs ad-hoc code and does NOT change the notebook file; use run_notebook_cells to execute the notebook's own cells and save their outputs. Output is truncated to 20000 characters."
)
async def execute_notebook_code(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID (must be RUNNING)"),
    code: str = Field(min_length=1, description="Code to execute in the notebook kernel"),
    timeout_sec: int = Field(
        default=120, ge=5, le=1800, description="Seconds to wait for the code to finish"),
) -> str:
    """Execute ad-hoc code in a notebook run's kernel and return the output"""
    token = None
    try:
        async with YeeduClient() as client:
            token = client.token
            status = await _wait_for_kernel(client, workspace_id, run_id)
            if status not in _CONNECTABLE_KERNEL_STATES:
                return (f"Error: kernel of notebook run {run_id} is in state {status}, which cannot be "
                        "connected to. HINT: Check the run with get_notebook_run - if it is RUNNING, "
                        "restart the kernel with notebook_kernel_action 'restart'.")
            # Registers the websocket proxy for this run/session token
            await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}/kernel/ws",
                params={"yeedu_session": token})

        msg_id = str(uuid.uuid4())
        ws_url = _kernel_ws_url(token, workspace_id, run_id)
        async with websockets.connect(
            ws_url, ssl=_ws_ssl_context(ws_url), max_size=None, open_timeout=30
        ) as ws:
            await ws.send(_execute_request(code, str(uuid.uuid4()), msg_id))
            outputs, error, reply_status = await asyncio.wait_for(
                _collect_outputs(ws, msg_id), timeout=timeout_sec)

        text = _truncate(_outputs_as_text(outputs))
        header = f"Execution {'failed: ' + error if error else 'finished (' + str(reply_status or 'ok') + ')'}"
        return f"{header}\n{text}" if text else header

    except asyncio.TimeoutError:
        return (f"Error: code did not finish within {timeout_sec}s. It may still be running in the kernel; "
                "use notebook_kernel_action with 'interrupt' to stop it.")
    except Exception as e:
        # The session token is part of the websocket URL; never echo it back
        message = str(e).replace(token, "***") if token else str(e)
        return f"Error executing notebook code: {message}. HINT: The notebook run must be RUNNING with a started kernel."


async def _save_notebook_outputs(client, workspace_id: int, notebook_id: int, run_id: int,
                                 notebook_file_id: int, notebook: dict,
                                 is_background: bool) -> str:
    """Persist cell outputs into the notebook.

    Background runs use the notebook update endpoint; it rejects interactive runs,
    so those are saved by writing the notebook file back in place.
    """
    for cell in notebook.get("cells") or []:
        for out in cell.get("outputs") or []:
            out.pop("msg_id", None)
            out.setdefault("output_type", "text")
    suffix = " (some outputs trimmed for the 30MB limit)" if _fit_notebook_size(notebook) else ""

    if is_background:
        await client.post(
            f"/api/v1/workspace/{workspace_id}/notebook/{notebook_id}/update",
            params={"run_id": run_id}, json=notebook)
        return "outputs saved to the notebook" + suffix

    listing = await client.get(
        "/api/v1/workspace/files",
        params={"workspace_id": workspace_id, "file_id": notebook_file_id})
    entries = (listing or {}).get("data") or []
    full_path = entries[0].get("full_file_path") if entries else None
    if not full_path:
        raise RuntimeError(
            f"could not resolve the workspace path of notebook file {notebook_file_id}")
    path = full_path.split("file:///files", 1)[-1] if "file:///files" in full_path else full_path
    if not path.startswith("/"):
        path = "/" + path

    content = json.dumps(notebook, indent=1, default=str)
    await client.post(
        "/api/v1/workspace/files",
        params={"workspace_id": workspace_id, "path": path,
                "is_dir": "false", "overwrite": "true"},
        headers={"x-file-size": str(len(content.encode("utf-8"))),
                 "Content-Type": "application/octet-stream"},
        data=content)
    return f"outputs saved to {path}" + suffix


@mcp.tool(
    description="Executes the notebook's own code cells in order in the kernel of a RUNNING notebook run, stopping at the first cell that fails, and saves each cell's output back into the notebook so the outputs are visible in the Yeedu UI. run_id is the notebook run ID from start_notebook, not the notebook_id. Markdown and empty cells are skipped. Returns a per-cell summary of outputs and errors."
)
async def run_notebook_cells(
    workspace_id: int = Field(description="Workspace ID containing the notebook run"),
    run_id: int = Field(description="Notebook run ID (must be RUNNING)"),
    timeout_sec: int = Field(
        default=600, ge=5, le=3600,
        description="Seconds to wait for all cells together"),
    save_outputs: bool = Field(
        default=True,
        description="Save the cell outputs back into the notebook (visible in the Yeedu UI)"),
) -> str:
    """Execute all code cells of a notebook run's notebook and persist their outputs"""
    token = None
    try:
        async with YeeduClient() as client:
            token = client.token
            run = await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}")
            run = run if isinstance(run, dict) else {}
            conf = run.get("notebook_conf") or {}
            notebook_id = conf.get("notebook_id")
            language = str((conf.get("notebook_job_type") or {}).get("language") or "")
            run_status = run.get("run_status")
            is_background = bool(run.get("is_background"))
            if not notebook_id:
                return f"Error: could not determine the notebook_id for run {run_id}. HINT: Check the run with get_notebook_run."
            if run_status != "RUNNING":
                return f"Error: notebook run {run_id} is in state {run_status}, not RUNNING. HINT: Start a run with start_notebook and retry."

            status = await _wait_for_kernel(client, workspace_id, run_id)
            if status not in _CONNECTABLE_KERNEL_STATES:
                return (f"Error: kernel of notebook run {run_id} is in state {status}, which cannot be "
                        "connected to. HINT: Restart it with notebook_kernel_action 'restart'.")

            # The notebook/run/download endpoint only serves batch runs, so read the
            # notebook from its workspace file instead (works for interactive runs too).
            notebook_file_id = conf.get("notebook_file_id")
            if not notebook_file_id:
                return f"Error: could not determine the notebook file for notebook {notebook_id}. HINT: Check the notebook with get_notebook."
            notebook = await client.get(
                "/api/v1/workspace/file/download",
                params={"workspace_id": workspace_id, "file_id": notebook_file_id})
            if isinstance(notebook, bytes):
                notebook = notebook.decode("utf-8", errors="replace")
            if isinstance(notebook, str):
                notebook = json.loads(notebook)
            if not isinstance(notebook, dict):
                return f"Error: unexpected notebook file response for notebook {notebook_id}."

            await client.get(
                f"/api/v1/workspace/{workspace_id}/notebook/run/{run_id}/kernel/ws",
                params={"yeedu_session": token})

        cells = notebook.get("cells") or []
        code_cells = [
            c for c in cells
            if c.get("cell_type", "code") == "code"
            and "".join(c.get("source") or "").strip()
        ]
        if not code_cells:
            return f"Notebook {notebook_id} has no non-empty code cells to execute."

        session_id = str(uuid.uuid4())
        ws_url = _kernel_ws_url(token, workspace_id, run_id)
        lines: List[str] = []
        failed_at = None
        timed_out = False
        executed = 0
        deadline = asyncio.get_event_loop().time() + timeout_sec

        async with websockets.connect(
            ws_url, ssl=_ws_ssl_context(ws_url), max_size=None, open_timeout=30
        ) as ws:
            for number, cell in enumerate(code_cells, start=1):
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    timed_out = True
                    break
                code = "".join(cell.get("source") or "")
                if language.upper() == "SQL":
                    code = f"%%sql\n{code}"
                msg_id = cell.get("cell_uuid") or str(uuid.uuid4())
                cell["cell_uuid"] = msg_id

                start_time = _now_iso()
                await ws.send(_execute_request(code, session_id, msg_id))
                try:
                    outputs, error, reply_status = await asyncio.wait_for(
                        _collect_outputs(ws, msg_id), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    lines.append(f"Cell {number}/{len(code_cells)}: TIMED OUT (still running in the kernel)")
                    break
                end_time = _now_iso()
                executed += 1

                cell["outputs"] = outputs
                metadata = cell.setdefault("metadata", {})
                metadata["startTime"] = start_time
                metadata["endTime"] = end_time
                metadata["lastRunTime"] = _run_time_label(start_time, end_time)

                text = _truncate(_outputs_as_text(outputs).strip(), 2000)
                label = f"Cell {number}/{len(code_cells)}"
                if error:
                    lines.append(f"{label}: FAILED - {error}" + (f"\n{text}" if text else ""))
                    failed_at = number
                    break
                lines.append(f"{label}: ok ({metadata['lastRunTime']})" + (f"\n{text}" if text else ""))
                if reply_status and reply_status != "ok":
                    lines.append(f"{label}: reply status {reply_status}")

        save_note = "outputs not saved (save_outputs=false)"
        if save_outputs:
            try:
                async with YeeduClient() as client:
                    save_note = await _save_notebook_outputs(
                        client, workspace_id, notebook_id, run_id,
                        notebook_file_id, notebook, is_background)
            except Exception as save_error:
                message = str(save_error).replace(token, "***") if token else str(save_error)
                save_note = f"WARNING: cells ran but saving outputs failed: {message}"

        if failed_at:
            headline = (f"Notebook {notebook_id} failed at cell {failed_at} of {len(code_cells)}; "
                        f"remaining cells were not run")
        elif timed_out:
            headline = (f"Notebook {notebook_id} timed out after {timeout_sec}s; "
                        f"{executed} of {len(code_cells)} cells completed")
        else:
            headline = f"Notebook {notebook_id}: all {len(code_cells)} code cells ran successfully"

        return f"{headline} ({save_note})\n\n" + "\n".join(lines)

    except Exception as e:
        message = str(e).replace(token, "***") if token else str(e)
        return f"Error running notebook cells: {message}. HINT: The notebook run must be RUNNING with a started kernel."
