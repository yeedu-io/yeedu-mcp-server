"""Platform reference data and lookup tools for Yeedu MCP Server."""

from typing import List, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Returns all valid cluster lifecycle status values with descriptions. Returns a table: id, cluster_status, description. Use these values as status filters in list_clusters.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_statuses(
) -> str:
    """Get all available cluster status values (RUNNING, STOPPED, ERROR, etc.) with descriptions"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/lookup_engine_cluster_instance_status")

            if not data:
                return "Error in API response."

            # Extract relevant fields
            lookup_cluster_statuses = []

            for lookup_cluster_status in data:
                cluster_status_id = lookup_cluster_status.get(
                    "engine_cluster_instance_status_id"
                )
                cluster_status_name = lookup_cluster_status.get("name")
                description = lookup_cluster_status.get("description", "N/A")

                lookup_cluster_statuses.append(
                    [cluster_status_id, cluster_status_name, description]
                )

            # Create table
            table = serialize_table(
                lookup_cluster_statuses,
                headers=["id", "cluster_status", "description"],
            )

            return table

    except Exception as e:
        return f"Error getting cluster statuses: {str(e)}. HINT: Ensure API endpoint is accessible and base URL is correct."


@mcp.tool(
    description="Returns all valid job run status values with descriptions. Returns a table: id, run_status, description. Use these values as status filters in workspace_job_runs and list_job_configurations.",
    annotations={"readOnlyHint": True}
)
async def get_job_statuses(
) -> str:
    """Get all job run status values (SUBMITTED, RUNNING, DONE, ERROR, etc.) with descriptions"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/lookup_spark_job_status")

            if not data:
                return "Error in API response."

            # Extract relevant fields
            lookup_spark_job_statuses = []

            for lookup_spark_job_status in data:
                spark_job_status_id = lookup_spark_job_status.get(
                    "spark_job_status_id")
                spark_job_status_name = lookup_spark_job_status.get("name")
                description = lookup_spark_job_status.get("description", "N/A")

                lookup_spark_job_statuses.append(
                    [spark_job_status_id, spark_job_status_name, description]
                )

            # Create table
            table = serialize_table(
                lookup_spark_job_statuses,
                headers=["id", "run_status", "description"],
            )

            return table

    except Exception as e:
        return f"Error getting job statuses: {str(e)}. HINT: Ensure API endpoint is accessible and base URL is correct."


@mcp.tool(
    description="Returns all user roles available in the Yeedu platform with their descriptions. Returns a table: id, role, description.",
    annotations={"readOnlyHint": True}
)
async def get_roles(
) -> str:
    """Get all available user roles and permissions in Yeedu platform"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/roles")

            if not data:
                return "Error in API response."

            # Extract relevant fields
            lookup_roles = []

            for role in data:
                role_id = role.get("role_id")
                role_name = role.get("role")
                description = role.get("description", "N/A")

                lookup_roles.append([role_id, role_name, description])

            # Create table
            table = serialize_table(
                lookup_roles,
                headers=["id", "role", "description"],
            )

            return table

    except Exception as e:
        return f"Error getting roles: {str(e)}. HINT: Ensure API credentials and connection are valid."


@mcp.tool(
    description="Returns all supported job types with their internal API codes and display names. Returns a table: id, job_type (internal code e.g. SPARK_JOB), display_name. Use the internal job_type code when filtering by job type in other tools.",
    annotations={"readOnlyHint": True}
)
async def get_job_types(
) -> str:
    """Get all available job types supported by platform - shows internal name and display_name; use the internal name (e.g. SPARK_JOB) as the job_type filter value"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/lookup_spark_job_type")

            if not data:
                return "Error in API response."

            # Extract relevant fields
            lookup_job_type = []

            for job_type in data:
                id = job_type.get("spark_job_type_id")
                name = job_type.get("name")
                display_name = job_type.get("display_name")

                lookup_job_type.append([id, name, display_name])

            # Create table
            table = serialize_table(
                lookup_job_type,
                headers=["id", "job_type", "display_name"],
            )

            return table

    except Exception as e:
        return f"Error getting job types: {str(e)}. HINT: Ensure API connection and base URL configuration are correct."


@mcp.tool(
    description="Returns all supported job type + language combinations, optionally filtered by job_type. Returns a table: id, job_type, language_name, display_name, support_runtime_arguments, support_runtime_configurations.",
    annotations={"readOnlyHint": True}
)
async def get_job_languages(
    job_type: Optional[List[str]] = Field(
        default=None,
        description="Optional list of job types to filter results by. Valid values: Custom Code, Notebook, Thrift SQL, Functions, JAR",
    ),
) -> str:
    """List all the Languages for different Job Types in Yeedu (Optional to use job type for filtering)"""
    try:
        params = {}
        if job_type:
            params["job_type"] = job_type

        async with YeeduClient() as client:
            data = await client.get("/api/v1/lookup_spark_job_type_lang", params=params)

            if not data:
                return "Error in API response."

            # Extract relevant fields
            lookup_job_type_langs = []

            for job_type_lang in data:
                id = job_type_lang.get("spark_job_type_lang_id")
                job_type_name = job_type_lang.get(
                    "spark_job_type", {}).get("name")
                lang_name = job_type_lang.get("lang_name")
                display_name = job_type_lang.get("display_name")
                support_runtime_args = job_type_lang.get(
                    "support_runtime_args")
                support_runtime_conf = job_type_lang.get(
                    "support_runtime_conf")

                lookup_job_type_langs.append([
                    id, job_type_name, lang_name, display_name, support_runtime_args, support_runtime_conf
                ])

            # Create table
            table = serialize_table(
                lookup_job_type_langs,
                headers=["id", "job_type", "language_name", "display_name",
                         "support_runtime_arguments", "support_runtime_configurations"],
            )

            return table

    except Exception as e:
        return f"Error getting job languages: {str(e)}. HINT: Ensure the job_type parameter values are correct."


@mcp.tool(
    description="Returns all permission levels available for workspace access control. Returns a table: ID, PERMISSION NAME, DESCRIPTION.",
    annotations={"readOnlyHint": True}
)
async def list_workspace_permissions(
) -> str:
    """List all the Permissions available for workspaces in Yeedu"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/workspace/permissions")

            if not data:
                return "Error in API response."

            lookup_workspace_permissions = []
            # Extract relevant fields
            for permission in data:
                permission_id = permission.get("permission_id")
                name = permission.get("name")
                description = permission.get("description", "N/A")

                lookup_workspace_permissions.append(
                    [permission_id, name, description])

            # Create table
            table = serialize_table(
                lookup_workspace_permissions,
                headers=["ID", "PERMISSION NAME", "DESCRIPTION"],
            )

            return table

    except Exception as e:
        return f"Error getting workspace permissions: {str(e)}. HINT: Check the connection and configuration settings."


@mcp.tool(
    description="Returns all valid cluster node status values with descriptions (e.g. INITIATING, RUNNING, DESTROYED, ERROR). Returns a table: id, node_status, description. Use these values as reference when interpreting node_status fields in get_cluster_node_events.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_node_statuses(
) -> str:
    """Get all available cluster node status values with descriptions"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/lookup_engine_cluster_instance_node_status")

            if not data:
                return "Error in API response."

            rows = []
            for item in data:
                rows.append([
                    item.get("engine_cluster_instance_node_status_id", "N/A"),
                    item.get("name", "N/A"),
                    item.get("description", "N/A"),
                ])

            return serialize_table(rows, headers=["id", "node_status", "description"])

    except Exception as e:
        return f"Error getting cluster node statuses: {str(e)}."
