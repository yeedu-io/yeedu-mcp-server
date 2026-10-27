"""Object Storage Manager (OSM) tools for Yeedu MCP Server.

Provides capabilities to list object storage manager configurations and manage files
(listing, downloading, deleting) stored within cloud object storage buckets.
"""

from typing import Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Lists all object storage manager (OSM) configurations with optional filters by cloud provider or name search. Returns CSV: osm_id, osm_name, cloud_provider, bucket_name, storage_type with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_storage_managers(
    cloud_provider: Optional[str] = Field(
        default=None, description="Filter by cloud provider: GCP, AWS, Azure, OnPrem"
    ),
    osm_name: Optional[str] = Field(
        default=None, description="Search by OSM name"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
) -> str:
    """List all configured object storage managers."""
    try:
        params = {"pageNumber": page, "limit": limit}
        if cloud_provider:
            params["cloud_provider"] = cloud_provider

        async with YeeduClient() as client:
            if osm_name:
                params["object_storage_manager_name"] = osm_name
                data = await client.get(
                    "/api/v1/object_storage_managers/search", params=params
                )
            else:
                data = await client.get(
                    "/api/v1/object_storage_managers", params=params
                )

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}. HINT: Check the parameters or name filter."

            if not data or "data" not in data:
                return "No object storage managers found."

            lines = ["osm_id,osm_name,cloud_provider,bucket_name,storage_type"]
            for osm in data["data"]:
                cloud = osm.get("cloud_provider", {}) or {}
                lines.append(
                    f"{osm.get('object_storage_manager_id', 'N/A')},"
                    f"{osm.get('object_storage_manager_name', osm.get('name', 'N/A'))},"
                    f"{cloud.get('name', 'N/A')},"
                    f"{osm.get('object_storage_bucket_name', 'N/A')},"
                    f"{osm.get('storage_type', {}).get('name', 'N/A') if isinstance(osm.get('storage_type'), dict) else osm.get('storage_type', 'N/A')}"
                )

            result_set = data.get("result_set", {})
            if result_set:
                lines.append(
                    f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} "
                    f"| Total: {result_set.get('total_objects', len(data['data']))}"
                )

            return "\n".join(lines)

    except Exception as e:
        return f"Error listing storage managers: {str(e)}. HINT: Ensure your API connection is valid and cloud_provider filter value is correct."


@mcp.tool(description="Manages files in an object storage manager. action=list returns CSV: file_id, file_name, file_path, file_size, is_directory with pagination. action=download returns the raw file content (up to 5000 chars). action=delete removes a file and returns CSV: action, file_id, status. Requires osm_id or osm_name; file_id or file_path is required for download/delete. action='delete' requires confirm=True: the first call returns the file it would delete and does nothing, so ask the user before repeating the call with confirm=True.")
async def manage_osm_files(
    action: str = Field(description="Action: list, download, or delete"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific file.",
    ),
    osm_id: Optional[int] = Field(
        default=None, description="Object storage manager ID"),
    osm_name: Optional[str] = Field(
        default=None, description="Object storage manager name (if ID not provided)"),
    file_id: Optional[int] = Field(
        default=None, description="File ID (for download/delete)"),
    file_path: Optional[str] = Field(
        default=None, description="File path filter"),
    file_name: Optional[str] = Field(
        default=None, description="File name to search for (for list)"),
    recursive: Optional[bool] = Field(
        default=None, description="List files recursively"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
) -> str:
    """List, download, or delete files in an object storage manager."""
    try:
        valid_actions = ["list", "download", "delete"]
        if action not in valid_actions:
            return f"Error: Invalid action '{action}'. HINT: Must be one of: list, download, or delete."

        if osm_id is None and osm_name is None:
            return "Error: Either osm_id or osm_name is required. HINT: Find valid storage managers first using list_storage_managers."

        if action in ("download", "delete") and file_id is None and file_path is None:
            return f"Error: file_id or file_path is required for {action} action. HINT: Call manage_osm_files with action='list' first to view files."

        # Refuse before opening a connection, so an unreachable API cannot turn
        # a refusal into an error the agent retries its way past.
        if action == "delete" and not confirmed(confirm):
            target = (
                f"file_id={file_id}" if file_id is not None else f"'{file_path}'"
            )
            osm = f"osm_id={osm_id}" if osm_id is not None else f"osm_name='{osm_name}'"
            retry = (
                f"{osm}, "
                + (f"file_id={file_id}" if file_id is not None
                   else f"file_path='{file_path}'")
            )
            return confirmation_required(
                "Object storage file",
                target,
                "manage_osm_files",
                record=f"Storage manager: {osm}",
                impact="The object is deleted from the storage bucket and cannot be recovered through Yeedu.",
                retry=retry,
            )

        async with YeeduClient() as client:
            # Common params
            params = {}
            if osm_id is not None:
                params["object_storage_manager_id"] = osm_id
            elif osm_name:
                params["object_storage_manager_name"] = osm_name

            if action == "list":
                params["pageNumber"] = page
                params["limit"] = limit
                if file_path:
                    params["file_path"] = file_path
                if recursive is not None:
                    params["recursive"] = str(recursive).lower()

                if file_name:
                    params["file_name"] = file_name
                    data = await client.get(
                        "/api/v1/object_storage_manager/files/search", params=params
                    )
                else:
                    data = await client.get(
                        "/api/v1/object_storage_manager/files", params=params
                    )

                if isinstance(data, dict) and "error_message" in data:
                    return f"Error: {data['error_message']}. HINT: Check if the osm_id or file_path is correct."

                if not data or "data" not in data:
                    return "No files found in object storage manager."

                lines = ["file_id,file_name,file_path,file_size,is_directory"]
                for f in data["data"]:
                    file_id_val = (
                        f.get("object_storage_manager_file_id")
                        if f.get("object_storage_manager_file_id") is not None
                        else f.get("file_id")
                        if f.get("file_id") is not None
                        else f.get("workspace_file_id")
                        if f.get("workspace_file_id") is not None
                        else f.get("id")
                    )
                    if file_id_val is None:
                        file_id_val = "N/A"

                    file_name_val = f.get("file_name") or f.get("name") or f.get("key") or "N/A"

                    file_path_val = (
                        f.get("full_file_path")
                        if f.get("full_file_path") is not None and str(f.get("full_file_path")).strip() != ""
                        else f.get("file_path")
                        if f.get("file_path") is not None and str(f.get("file_path")).strip() != ""
                        else f.get("path")
                        if f.get("path") is not None and str(f.get("path")).strip() != ""
                        else f.get("relative_path")
                        if f.get("relative_path") is not None and str(f.get("relative_path")).strip() != ""
                        else f.get("full_path")
                        if f.get("full_path") is not None and str(f.get("full_path")).strip() != ""
                        else f.get("key")
                        if f.get("key") is not None and str(f.get("key")).strip() != ""
                        else f.get("location")
                        if f.get("location") is not None and str(f.get("location")).strip() != ""
                        else None
                    )
                    if file_path_val is None:
                        file_path_val = file_name_val if file_name_val != "N/A" else "N/A"

                    is_dir_val = (
                        f.get("is_dir")
                        if f.get("is_dir") is not None
                        else f.get("is_directory")
                        if f.get("is_directory") is not None
                        else f.get("directory", False)
                    )

                    file_size_raw = (
                        f.get("file_size_bytes")
                        if f.get("file_size_bytes") is not None
                        else f.get("file_size")
                        if f.get("file_size") is not None
                        else f.get("size")
                        if f.get("size") is not None
                        else f.get("length")
                        if f.get("length") is not None
                        else f.get("bytes")
                    )
                    if file_size_raw is not None:
                        file_size_val = str(file_size_raw)
                    else:
                        file_size_val = "-" if is_dir_val else "N/A"

                    lines.append(
                        f"{file_id_val},"
                        f"{file_name_val},"
                        f"{file_path_val},"
                        f"{file_size_val},"
                        f"{is_dir_val}"
                    )

                result_set = data.get("result_set", {})
                if result_set:
                    lines.append(
                        f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} "
                        f"| Total: {result_set.get('total_objects', len(data['data']))}"
                    )
                return "\n".join(lines)

            elif action == "download":
                if file_id:
                    params["file_id"] = file_id
                if file_path:
                    params["file_path"] = file_path

                data = await client.get(
                    "/api/v1/object_storage_manager/file/download", params=params
                )

                if isinstance(data, dict) and "error_message" in data:
                    return f"Error: {data['error_message']}. HINT: Check if the file exists and you have read permissions."

                # Return file content or download info
                if isinstance(data, str):
                    return data[:5000]  # Limit output
                return str(data)[:5000]

            else:  # delete
                payload = {}
                if osm_id is not None:
                    payload["object_storage_manager_id"] = osm_id
                elif osm_name:
                    payload["object_storage_manager_name"] = osm_name
                if file_id:
                    payload["file_id"] = file_id
                if file_path:
                    payload["file_path"] = file_path

                data = await client.delete(
                    "/api/v1/object_storage_manager/file", json=payload
                )

                if isinstance(data, dict) and "error_message" in data:
                    return f"Error: {data['error_message']}. HINT: Verify if the file is locked, or already deleted."

                lines = ["field,value"]
                lines.append(f"action,delete")
                lines.append(f"file_id,{file_id or 'N/A'}")
                lines.append(f"status,File deleted successfully")
                return "\n".join(lines)

    except Exception as e:
        return f"Error managing OSM files: {str(e)}. HINT: Double-check action (list, download, delete), file_id, file_path, and your credentials."


