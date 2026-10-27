"""Tenant, user profile, and role listing tools for Yeedu MCP Server."""

from typing import Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Lists tenants accessible to the current user with optional name search filter. Returns a table: Tenant ID, Name, Description with pagination info.",
    annotations={"readOnlyHint": True}
)
async def list_tenants(
    tenant_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the tenant to filter"
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of tenants to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """List tenants with optional name filtering - shows tenant ID, name, and description with pagination"""
    try:
        params = {}
        params["pageNumber"] = page
        params["limit"] = limit

        async with YeeduClient() as client:
            if tenant_name:
                params["tenant_name"] = tenant_name
                data = await client.get("/api/v1/user/tenants/search", params=params)
            else:
                data = await client.get("/api/v1/user/tenants", params=params)

            if not data or "data" not in data:
                return "No tenants found or error in API response."

            # Extract relevant fields
            tenants = []
            for tenant in data["data"]:
                tenants.append(
                    [
                        tenant.get("tenant_id"),
                        tenant.get("name"),
                        tenant.get("description", "N/A"),
                    ]
                )

            # Create table
            table = serialize_table(
                tenants,
                headers=["Tenant ID", "Name", "Description"],
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
        return f"Error listing tenants: {str(e)}"

