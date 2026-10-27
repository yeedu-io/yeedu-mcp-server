"""Workspace, file management, and permissions tools for Yeedu MCP Server."""
import json
from typing import Any, List, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Lists workspaces with optional filters (name, enabled/disabled state, cluster attachment). Returns a table: workspace_id, workspace_name, total_jobs, total_notebooks, created_by_username, modified_by_username with pagination info.",
    annotations={"readOnlyHint": True}
)
async def list_workspaces(
    workspace_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the workspace to filter"
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of workspaces to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
    enable: Optional[bool] = Field(
        default=True,
        description="Filter by state: True=active, False=disabled, None=all",
    ),
    cluster_id: Optional[int] = Field(
        default=None, description="Filter by cluster ID (requires is_attached)"
    ),
    is_attached: Optional[bool] = Field(
        default=None,
        description="Filter by cluster attachment status (requires cluster_id)",
    ),
) -> str:
    """List workspaces with filtering by name, enable status active/disabled, and cluster attachment -return list of workspaces and shows job/notebook counts for each"""
    try:
        params = {}

        # Only add list parameters if they have values
        if cluster_id:
            params["cluster_id"] = cluster_id

        if isinstance(enable, bool):
            params["enable"] = str(enable).lower()

        if isinstance(is_attached, bool):
            params["is_attached"] = str(is_attached).lower()

        # Always add pagination parameters
        params["pageNumber"] = page
        params["limit"] = limit

        async with YeeduClient() as client:
            if workspace_name:
                params["workspace_name"] = workspace_name
                data = await client.get("/api/v1/workspaces/search", params=params)
            else:
                data = await client.get("/api/v1/workspaces", params=params)

            if not data or "data" not in data:
                return "No workspaces found or error in API response."

            # Extract relevant fields
            workspaces = [
                [
                    workspace.get("workspace_id", "N/A"),
                    workspace.get("name", "N/A"),
                    workspace.get("job_count", 0),
                    workspace.get("notebook_count", 0),
                    workspace.get("created_by", {}).get("username", "N/A"),
                    workspace.get("modified_by", {}).get("username", "N/A"),
                ]
                for workspace in data["data"]
            ]

            # Create table
            table = serialize_table(
                workspaces,
                headers=[
                    "workspace_id",
                    "workspace_name",
                    "total_jobs",
                    "total_notebooks",
                    "created_by_username",
                    "modified_by_username",
                ],
            )

            # Add pagination info if available
            result_set = data.get("result_set")
            if result_set:
                pagination_info = (
                    f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} | "
                    f"Total Items: {result_set.get('total_objects', 0)} | "
                    f"Items Per Page: {limit}"
                )
                table += pagination_info

            return table

    except Exception as e:
        return f"Error listing workspaces: {str(e)}. HINT: Make sure the server connection is active and parameters are valid."


@mcp.tool(
    description="Browses files and directories in a workspace at a given path (defaults to /). Supports recursive listing and type filtering (files vs directories). Returns a table: Name, Path, Type, Size, Last Modified, Created By, Modified By with pagination.",
    annotations={"readOnlyHint": True}
)
async def browse_workspace_files(
    workspace_id: int = Field(description="Workspace ID containing the files"),
    is_dir: Optional[bool] = Field(
        default=None,
        description="Filter by type: True for directories, False for files, None for both",
    ),
    file_id: Optional[int] = Field(default=None, description="File ID"),
    file_path: Optional[str] = Field(
        default="/", description="Path to list files from"
    ),
    recursive: bool = Field(
        default=True,
        description="Include subdirectories and their contents recursively",
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of files to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """Browse workspace files and directories with recursive listing - shows file metadata, size, etc"""
    try:
        # Validate required combinations
        if not file_id and not file_path:
            return "Error: Either file_id or file_path must be provided"

        params = {}
        params["workspace_id"] = workspace_id

        # File location (required - one of these)
        if file_id:
            params["file_id"] = file_id
        elif file_path:
            params["file_path"] = file_path

        # Optional filters
        if is_dir is not None:
            params["is_dir"] = str(is_dir).lower()

        params["recursive"] = str(recursive).lower()
        params["limit"] = limit
        params["pageNumber"] = page

        async with YeeduClient() as client:
            data = await client.get("/api/v1/workspace/files", params=params)

            if not data or "data" not in data:
                return f"No files found in {file_path} or error in API response."

            # Extract relevant fields
            files = []
            for file in data["data"]:
                # Get file type, falling back to directory/file based on is_dir
                file_type = file.get("file_type") or (
                    "directory" if file.get("is_dir") else "file"
                )

                # Format size with appropriate units
                size_bytes = file.get("file_size_bytes")
                if size_bytes:
                    try:
                        size = int(size_bytes)
                        if size < 1024:
                            size_str = f"{size} B"
                        elif size < 1024 * 1024:
                            size_str = f"{size/1024:.1f} KB"
                        else:
                            size_str = f"{size/(1024*1024):.1f} MB"
                    except (ValueError, TypeError):
                        size_str = "N/A"
                else:
                    size_str = "N/A"

                files.append(
                    [
                        file.get("file_name", "N/A"),
                        file.get("full_file_path", "N/A"),
                        file_type,
                        size_str,
                        file.get("last_update_date", "N/A"),
                        file.get("created_by", {}).get("username", "N/A"),
                        file.get("modified_by", {}).get("username", "N/A"),
                    ]
                )

            # Create table
            table = serialize_table(
                files,
                headers=[
                    "Name",
                    "Path",
                    "Type",
                    "Size",
                    "Last Modified",
                    "Created By",
                    "Modified By",
                ],
            )

            # Add pagination info if available
            result_set = data.get("result_set")
            if result_set:
                pagination_info = (
                    f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} | "
                    f"Total Items: {result_set.get('total_objects', 0)} | "
                    f"Items Per Page: {limit}"
                )
                table += pagination_info

            return table

    except Exception as e:
        return f"Error browsing workspace files: {str(e)}. HINT: Check if workspace_id exists and file_path is valid."


@mcp.tool(
    description="Downloads and returns the raw text content of a workspace file identified by file_id or file_path. Returns the file content as a plain string.",
    annotations={"readOnlyHint": True}
)
async def get_workspace_file(
    workspace_id: int = Field(description="Workspace ID containing the file"),
    file_id: Optional[int] = Field(default=None, description="File ID"),
    file_path: Optional[str] = Field(
        description="File path in workspace (e.g., '/breast_cancer_analysis_pipeline.py')"
    ),
) -> str:
    """Get workspace file content by workspace ID + either file ID or path - returns file contents as a string"""
    try:
        params = {}
        params["workspace_id"] = workspace_id

        # Handle file identifier
        if file_id:
            params["file_id"] = file_id
        elif file_path:
            # Ensure file_path starts with a single forward slash
            if file_path.startswith("/files/"):
                file_path = file_path[6:]  # Remove '/files/' prefix
            if not file_path.startswith("/"):
                file_path = "/" + file_path  # Add leading slash if missing
            params["file_path"] = file_path

        async with YeeduClient() as client:
            data = await client.get("/api/v1/workspace/file/download", params=params)

            if data is None:
                result_text = ""
            elif isinstance(data, str):
                result_text = data
            elif isinstance(data, bytes):
                result_text = data.decode("utf-8", errors="replace")
            elif isinstance(data, (dict, list)):
                result_text = json.dumps(data, indent=2, default=str)
            else:
                result_text = str(data)

            return str(result_text)

    except Exception as e:
        return f"Error getting workspace file: {str(e)}. HINT: Double-check if the file exists at file_path or has correct file_id."


@mcp.tool(
    description="Retrieves the access permissions of a specific user for a workspace. Requires workspace_id and user_id. Returns the user's individual permission level (name + description) and any group permissions as formatted tables.",
    annotations={"readOnlyHint": True}
)
async def get_workspace_permissions(
    workspace_id: int = Field(
        description="Workspace ID to retrieve permissions"),
    user_id: int = Field(
        description="User ID of the user to check permissions for"),
) -> str:
    """Retrieve workspace permissions of a user for a specific Workspace using Workspace ID and User ID."""
    try:
        params = {}

        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/ac/workspace/{workspace_id}/user/{user_id}/permission",
                params=params
            )

            # check if user_permission key exists in the data or if it is null
            if "user_permission" not in data or data.get("user_permission") is None:
                user_permission = [
                    # Basic Job Information
                    ["User ID", data.get("user_id", "N/A")],
                    ["Username", data.get("username", "N/A")],
                    ["Workspace ID", data.get("workspace", {}).get(
                        "workspace_id", "N/A")],
                    ["Workspace Name", data.get(
                        "workspace", {}).get("name", "N/A")]
                ]
                table1 = "User Permissions: \n"+serialize_table(user_permission, headers=[
                    "Field", "Value"]) + "\nNo user permissions found for the specified user in this workspace."

            # Extract all relevant fields
            else:
                user_permission = [
                    # Basic Job Information
                    ["User ID", data.get("user_id", "N/A")],
                    ["Username", data.get("username", "N/A")],
                    ["Workspace ID", data.get("workspace", {}).get(
                        "workspace_id", "N/A")],
                    ["Workspace Name", data.get(
                        "workspace", {}).get("name", "N/A")],
                    ["Permission Name", data.get("user_permission", {}).get(
                        "permission", {}).get("name", "N/A")],
                    ["Permission Description", data.get("user_permission", {}).get(
                        "permission", {}).get("description", "N/A")]
                ]

                # Create table
                table1 = "User Permissions: \n" + serialize_table(user_permission, headers=[
                    "Field", "Value"])

            group_permissions = data.get("group_permission") or []
            if len(group_permissions) > 0:
                group_permission = []
                for group in group_permissions:
                    group_permission.append([
                        group.get("group_id", "N/A"),
                        group.get("permission", {}).get("name", "N/A"),
                        group.get("permission", {}).get("description", "N/A"),
                    ])

                table2 = serialize_table(
                    group_permission,
                    headers=[
                        "Group ID",
                        "Permission Name",
                        "Permission Description"
                    ])
            else:
                table2 = "No group permissions found."

            return table1 + "\n\nGroup Permissions: \n" + table2

    except Exception as e:
        return f"Error getting workspace permissions: {str(e)}. HINT: Ensure workspace_id and user_id are valid."


@mcp.tool(
    description="Uploads or creates a text file or directory in a workspace at the specified path. For files (is_dir=False), provide file_content. For directories (is_dir=True), set is_dir=True and omit file_content. Returns a success confirmation string.",
    annotations={"idempotentHint": True}
)
async def upload_workspace_file(
    workspace_id: int = Field(description="Workspace ID containing the files"),
    overwrite: bool = Field(
        default=False,
        description="Whether to overwrite the file if it already exists",
    ),
    is_dir: bool = Field(
        default=False,
        description="A boolean flag indicating whether the uploaded file is a directory.",
    ),
    path: str = Field(description="destination file path. should start with '/' and filename should be included in the path"
                      ),
    target_dir: Optional[str] = Field(
        default=None,
        description="The target directory prefix for uploaded file(s). should start with '/'",
    ),
    file_content: Optional[Any] = Field(
        default=None,
        description="The content of the file to be uploaded as a decoded readable string. JSON content (e.g. .ipynb notebooks) may be passed as a string or as a JSON object. Required for files (is_dir=False). Omit for directories.",
    ),
) -> str:
    """Upload/create file or directory in workspace at specified path. Supports overwrite mode, directory creation, and target directory specification. Returns success confirmation."""
    try:
        params = {}
        params["workspace_id"] = workspace_id
        params["path"] = path

        # Optional filters
        if is_dir is not None:
            params["is_dir"] = str(is_dir).lower()

        if overwrite is not None:
            params["overwrite"] = str(overwrite).lower()
        if target_dir:
            params["target_dir"] = target_dir

        async with YeeduClient() as client:
            if is_dir:
                # Directory creation: Do not send X-File-Size header or binary payload
                await client.post(
                    "/api/v1/workspace/files",
                    params=params
                )
                return "Directory created successfully."
            else:
                if file_content is None:
                    return "Error: file_content is required when creating/uploading a file (is_dir=False)."
                # MCP clients may auto-parse JSON-looking strings (e.g. .ipynb) into dict/list
                if not isinstance(file_content, str):
                    file_content = json.dumps(file_content, indent=1)
                headers = {
                    "x-file-size": str(len(file_content.encode("utf-8"))),
                    "Content-Type": "application/octet-stream",
                }
                await client.post(
                    "/api/v1/workspace/files",
                    params=params,
                    headers=headers,
                    data=file_content
                )
                return "File uploaded successfully."

    except Exception as e:
        return f"Error uploading file to workspace: {str(e)}. HINT: Make sure the file path starts with '/' and is not within a write-protected directory."


@mcp.tool(description="Performs a file management operation on a workspace file: delete, rename (requires new_name), move, or copy (requires destination_path). Identify the file by file_id or file_path. Returns CSV: action, workspace_id, file_id/file_path, result_file_id/result_path, status. action='delete' requires confirm=True: the first call returns the file it would delete and does nothing, so ask the user before repeating the call with confirm=True.")
async def manage_workspace_file(
    workspace_id: int = Field(description="Workspace ID"),
    action: str = Field(description="Action: delete, rename, move, or copy"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific file.",
    ),
    file_id: Optional[int] = Field(
        default=None, description="File ID to act on"),
    file_path: Optional[str] = Field(
        default=None, description="File path (if file_id not provided)"),
    new_name: Optional[str] = Field(
        default=None, description="New file name (for rename action)"),
    destination_path: Optional[str] = Field(
        default=None, description="Destination path (for move and copy actions)"
    ),
) -> str:
    """Delete, rename, move, or copy a workspace file."""
    try:
        valid_actions = ["delete", "rename", "move", "copy"]
        if action not in valid_actions:
            return f"Error: Invalid action '{action}'. Must be: {', '.join(valid_actions)}"

        if file_id is None and file_path is None:
            return "Error: Either file_id or file_path is required."

        if action == "rename" and not new_name:
            return "Error: new_name is required for rename action."
        if action in ("move", "copy") and not destination_path:
            return f"Error: destination_path is required for {action} action."

        # Refuse before opening a connection, so an unreachable API cannot turn
        # a refusal into an error the agent retries its way past.
        if action == "delete" and not confirmed(confirm):
            target = (
                f"file_id={file_id}" if file_id is not None else f"'{file_path}'"
            )
            retry = (
                f"workspace_id={workspace_id}, "
                + (f"file_id={file_id}" if file_id is not None
                   else f"file_path='{file_path}'")
            )
            return confirmation_required(
                "Workspace file",
                target,
                "manage_workspace_file",
                record=f"Workspace: {workspace_id}",
                impact="The file is removed from the workspace; notebooks and jobs that reference it will fail to load.",
                retry=retry,
            )

        async with YeeduClient() as client:
            # Clean file_path and destination_path if provided
            if file_path:
                if file_path.startswith("file:///files/"):
                    file_path = file_path[14:]
                elif file_path.startswith("file:///"):
                    file_path = file_path[7:]
                elif file_path.startswith("/files/"):
                    file_path = file_path[6:]
                if not file_path.startswith("/"):
                    file_path = "/" + file_path

            if destination_path:
                if destination_path.startswith("file:///files/"):
                    destination_path = destination_path[14:]
                elif destination_path.startswith("file:///"):
                    destination_path = destination_path[7:]
                elif destination_path.startswith("/files/"):
                    destination_path = destination_path[6:]
                if not destination_path.startswith("/"):
                    destination_path = "/" + destination_path

            if action == "delete":
                params = {"workspace_id": workspace_id}
                if file_id is not None:
                    params["file_id"] = file_id
                elif file_path:
                    params["file_path"] = file_path

                data = await client.delete(
                    "/api/v1/workspace/file", params=params
                )

            elif action == "rename":
                params = {
                    "workspace_id": workspace_id,
                    "file_name": new_name,
                }
                if file_id is not None:
                    params["file_id"] = file_id
                elif file_path:
                    params["file_path"] = file_path

                data = await client.put(
                    "/api/v1/workspace/file/rename", params=params
                )

            elif action == "move":
                params = {
                    "workspace_id": workspace_id,
                    "destination_file_path": destination_path,
                }
                if file_id is not None:
                    params["source_file_id"] = file_id
                elif file_path:
                    params["source_file_path"] = file_path

                data = await client.put(
                    "/api/v1/workspace/file/move", params=params
                )

            else:  # copy
                params = {
                    "workspace_id": workspace_id,
                    "destination_file_path": destination_path,
                }
                if file_id is not None:
                    params["source_file_id"] = file_id
                elif file_path:
                    params["source_file_path"] = file_path

                data = await client.post(
                    "/api/v1/workspace/file/copy", params=params
                )

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            lines = ["field,value"]
            lines.append(f"action,{action}")
            lines.append(f"workspace_id,{workspace_id}")
            if file_id:
                lines.append(f"file_id,{file_id}")
            if file_path:
                lines.append(f"file_path,{file_path}")
            if new_name:
                lines.append(f"new_name,{new_name}")
            if destination_path:
                lines.append(f"destination_path,{destination_path}")

            if isinstance(data, dict):
                if "workspace_file_id" in data:
                    lines.append(f"result_file_id,{data['workspace_file_id']}")
                if "file_path" in data:
                    lines.append(f"result_path,{data['file_path']}")
            lines.append(
                f"status,{action.capitalize()} completed successfully")
            return "\n".join(lines)

    except Exception as e:
        return f"Error {action} file: {str(e)}. HINT: Double-check if the file exists and is not locked or write-protected."


@mcp.tool(
    description="Exports all job and notebook configurations from a workspace. If the workspace contains only jobs, the export is a single .yeedu file. If it contains notebooks, the export is a .zip archive containing the .yeedu file and .ipynb notebook files. Saves to output_path if provided, otherwise saves to the current directory using the server-provided filename. NOTE: The file is saved on the filesystem where the MCP server is running (same machine as the client in stdio mode). Returns the saved path and size.",
    annotations={"readOnlyHint": True}
)
async def export_workspace(
    workspace_id: Optional[int] = Field(
        default=None, description="Workspace ID to export"),
    workspace_name: Optional[str] = Field(
        default=None, description="Workspace name to export"),
    output_path: str = Field(
        description="Local path to save the exported file, e.g. /home/user/exports/my_workspace.zip or /tmp/my_workspace.yeedu. If a directory path is given, the server-provided filename is appended automatically."
    ),
    strip_outputs: Optional[bool] = Field(
        default=None, description="Strip notebook outputs from the export"),
) -> str:
    """Export all jobs and notebooks of a workspace as a ZIP package saved to disk."""
    try:
        if workspace_id is None and workspace_name is None:
            return "Error: Either workspace_id or workspace_name is required."

        params: dict = {}
        if workspace_id is not None:
            params["workspace_id"] = workspace_id
        elif workspace_name:
            params["workspace_name"] = workspace_name
        if strip_outputs is not None:
            params["strip_outputs"] = str(strip_outputs).lower()

        async with YeeduClient() as client:
            response = await client._client.request(
                method="GET",
                url="/api/v1/workspace/export",
                params=params,
                headers=client._auth_headers(),
            )

            if response.status_code >= 400:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text
                return f"Error exporting workspace: {detail}"

            # Extract filename from Content-Disposition header
            disposition = response.headers.get("content-disposition", "")
            server_filename = None
            for part in disposition.split(";"):
                part = part.strip()
                if part.startswith("filename"):
                    server_filename = part.split("=", 1)[1].strip().strip('"')
                    break
            if not server_filename:
                name = workspace_name or str(workspace_id)
                server_filename = f"{name}.yeedu"  # fallback; actual ext set by server via Content-Disposition

            # Determine save path: if output_path has no extension, treat as directory
            import os
            _, ext = os.path.splitext(output_path)
            if not ext or output_path.endswith(os.sep):
                save_path = os.path.join(output_path, server_filename)
            else:
                save_path = output_path

            with open(save_path, "wb") as f:
                f.write(response.content)

            return f"Workspace exported successfully.\nFile saved to: {save_path}\nSize: {len(response.content)} bytes"

    except Exception as e:
        return f"Error exporting workspace: {str(e)}. HINT: Ensure you have read/export permissions for workspace_id/workspace_name."


# ---------------------------------------------------------------------------
# Workspace lifecycle
#
# Yeedu has no DELETE for a workspace — disable is the only removal path, and
# it is reversible with enable. It is still gated behind confirm, because a
# disabled workspace takes its jobs and notebooks out of reach.
# ---------------------------------------------------------------------------

_WORKSPACE_ACTIONS = ("create", "edit", "enable", "disable")

#: Pages of /workspaces to scan when looking a workspace up by id alone.
_LOOKUP_PAGE_LIMIT = 100
_LOOKUP_MAX_PAGES = 3


async def _describe_workspace(
    workspace_id: Optional[int], workspace_name: Optional[str]
) -> Optional[str]:
    """Read a workspace back, for the line shown before it is disabled.

    There is no GET on /workspace, and /workspace/stats returns job-run status
    counts rather than a workspace, so the record comes from the listing: by
    name through /workspaces/search, or by scanning /workspaces when only an id
    is known. Best effort — the confirmation still names the target without it.
    """
    try:
        async with YeeduClient() as client:
            if workspace_name:
                data = await client.get(
                    "/api/v1/workspaces/search",
                    params={"workspace_name": workspace_name,
                            "pageNumber": 1, "limit": _LOOKUP_PAGE_LIMIT},
                )
                rows = _candidate_rows(data)
                match = next(
                    (r for r in rows if r.get("name") == workspace_name), None)
            else:
                match = None
                for page in range(1, _LOOKUP_MAX_PAGES + 1):
                    data = await client.get(
                        "/api/v1/workspaces",
                        params={"pageNumber": page, "limit": _LOOKUP_PAGE_LIMIT},
                    )
                    rows = _candidate_rows(data)
                    match = next(
                        (r for r in rows
                         if r.get("workspace_id") == workspace_id), None)
                    if match or len(rows) < _LOOKUP_PAGE_LIMIT:
                        break
    except Exception:
        return None

    if not isinstance(match, dict):
        return None
    return (
        f"Workspace: {match.get('name', 'N/A')} "
        f"(id {match.get('workspace_id', 'N/A')}) — "
        f"{match.get('job_count', '?')} jobs, "
        f"{match.get('notebook_count', '?')} notebooks"
    )


def _candidate_rows(data) -> list:
    """The workspace records in a listing response, whatever it is wrapped in."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


@mcp.tool(description="Creates, renames or enables/disables a workspace. action=create requires name (description optional). action=edit/enable/disable identify the workspace by workspace_id or workspace_name; edit changes name and/or description. There is no delete for workspaces — disable is the removal path and is reversible with enable, and it requires confirm=True: the first call returns the workspace it would disable and does nothing. Returns the resulting workspace record.")
async def manage_workspace(
    action: str = Field(
        description="Action: create, edit, enable, or disable"),
    workspace_id: Optional[int] = Field(
        default=None, description="Workspace ID (for edit, enable, disable)"),
    workspace_name: Optional[str] = Field(
        default=None, description="Workspace name, if workspace_id is not given (for edit, enable, disable)"),
    name: Optional[str] = Field(
        default=None, description="Workspace name to set (required for create, optional for edit)"),
    description: Optional[str] = Field(
        default=None, description="Workspace description (optional for create and edit)"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='disable'. Set it only after the user has confirmed this specific workspace.",
    ),
) -> str:
    """Create, edit, enable or disable a workspace."""
    try:
        if action not in _WORKSPACE_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_WORKSPACE_ACTIONS)}."
            )

        if action == "create":
            if not name:
                return "Error: name is required for action='create'."
            body = {"name": name}
            if description:
                body["description"] = description
            async with YeeduClient() as client:
                data = await client.post("/api/v1/workspace", json=body)
            return _workspace_result("created", data)

        if workspace_id is None and not workspace_name:
            return (
                f"Error: Either workspace_id or workspace_name is required for action='{action}'. "
                "HINT: Call list_workspaces to find it."
            )

        params = {}
        if workspace_id is not None:
            params["workspace_id"] = workspace_id
        else:
            params["workspace_name"] = workspace_name

        target = (
            f"workspace_id={workspace_id}" if workspace_id is not None
            else f"'{workspace_name}'"
        )

        if action == "edit":
            if not name and description is None:
                return "Error: Provide name and/or description to change for action='edit'."
            body = {}
            if name:
                body["name"] = name
            if description is not None:
                body["description"] = description
            async with YeeduClient() as client:
                data = await client.put(
                    "/api/v1/workspace", params=params, json=body)
            return _workspace_result("updated", data)

        if action == "disable":
            # Refuse before opening a connection, so an unreachable API cannot
            # turn a refusal into an error the agent retries its way past.
            if not confirmed(confirm):
                record = await _describe_workspace(workspace_id, workspace_name)
                retry = (
                    f"workspace_id={workspace_id}" if workspace_id is not None
                    else f"workspace_name='{workspace_name}'"
                )
                return confirmation_required(
                    "Workspace",
                    target,
                    "manage_workspace",
                    action="disable",
                    record=record,
                    impact="The workspace and every job and notebook in it become inaccessible. Nothing is deleted — manage_workspace(action='enable') restores it.",
                    retry=retry,
                )

        endpoint = f"/api/v1/workspace/{action}"
        async with YeeduClient() as client:
            data = await client.put(endpoint, params=params)
        return _workspace_result(f"{action}d", data)

    except Exception as e:
        return (
            f"Error running action '{action}' on workspace: {str(e)}. "
            "HINT: Call list_workspaces to confirm the workspace exists and that the name is not already taken."
        )


def _workspace_result(verb: str, data: Any) -> str:
    """Render the workspace record an action returned."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return f"Workspace {verb} successfully."
    rows = [
        ["Workspace ID", data.get("workspace_id", "N/A")],
        ["Name", data.get("name", "N/A")],
        ["Description", data.get("description") or "N/A"],
        ["Enabled", data.get("enable", "N/A")],
    ]
    return (
        f"Workspace {verb} successfully.\n\n"
        + serialize_table(rows, headers=["Field", "Value"])
    )


@mcp.tool(description="Imports jobs and notebooks from a workspace export file (.yeedu or .zip produced by export_workspace) into a workspace. The file is read from the filesystem where the MCP server runs. Identify the destination by workspace_id or workspace_name; workspace_name creates the workspace if it does not exist. cluster_id or cluster_name attaches the imported jobs to a cluster. overwrite replaces an existing workspace's contents; permissive completes a partial import instead of failing on the first error. Returns what was imported.")
async def import_workspace(
    file_path: str = Field(
        description="Local path to the export file, e.g. /home/user/exports/my_workspace.yeedu"),
    workspace_id: Optional[int] = Field(
        default=None, description="Destination workspace ID"),
    workspace_name: Optional[str] = Field(
        default=None, description="Destination workspace name, if workspace_id is not given"),
    cluster_id: Optional[int] = Field(
        default=None, description="Cluster instance ID to attach the imported jobs to"),
    cluster_name: Optional[str] = Field(
        default=None, description="Cluster instance name, if cluster_id is not given"),
    overwrite: bool = Field(
        default=False, description="Replace the contents of an existing workspace"),
    permissive: bool = Field(
        default=False, description="Import what can be imported instead of failing on the first error"),
) -> str:
    """Import a workspace export file into a workspace."""
    try:
        if workspace_id is None and not workspace_name:
            return "Error: Either workspace_id or workspace_name is required."

        import os
        import zipfile

        if not os.path.isfile(file_path):
            return (
                f"Error: No file at {file_path}. "
                "HINT: The path is read on the machine running this MCP server. "
                "Produce one with export_workspace first."
            )

        # A workspace export is either a bare .yeedu JSON document or a zip
        # holding that document alongside the .ipynb files.
        if zipfile.is_zipfile(file_path):
            with zipfile.ZipFile(file_path) as archive:
                members = [n for n in archive.namelist()
                           if n.endswith(".yeedu")]
                if not members:
                    return (
                        f"Error: {file_path} is a zip with no .yeedu document inside. "
                        "HINT: Import the file produced by export_workspace, unmodified."
                    )
                raw = archive.read(members[0]).decode("utf-8")
        else:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return (
                f"Error: {file_path} is not a valid workspace export ({e}). "
                "HINT: Import the file produced by export_workspace, unmodified."
            )

        params: dict = {
            "overwrite": str(bool(overwrite)).lower(),
            "permissive": str(bool(permissive)).lower(),
        }
        if workspace_id is not None:
            params["workspace_id"] = workspace_id
        else:
            params["workspace_name"] = workspace_name
        if cluster_id is not None:
            params["cluster_id"] = cluster_id
        elif cluster_name:
            params["cluster_name"] = cluster_name

        async with YeeduClient() as client:
            data = await client.post(
                "/api/v1/workspace/import", params=params, json=payload)

        return "Workspace imported successfully.\n\n" + json.dumps(data, indent=2, default=str)

    except Exception as e:
        return (
            f"Error importing workspace: {str(e)}. "
            "HINT: If the destination workspace already has these jobs, retry with overwrite=True; "
            "to keep going past individual failures, retry with permissive=True."
        )

