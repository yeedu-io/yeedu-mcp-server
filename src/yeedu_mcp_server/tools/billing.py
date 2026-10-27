"""Migrated from AssistantX: users.py, billing.py
Migration: yeedu_token removed, tabulate → serialize_table, imports fixed.
"""
import datetime
import json
from typing import Any, Dict, List, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Returns the current authenticated user's profile information including user ID, username, email, tenant memberships, and assigned roles. Returns full JSON.",
    annotations={"readOnlyHint": True}
)
async def get_user_info() -> str:
    """Get user information"""
    try:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/user/info")
            return json.dumps(data, indent=2)

    except Exception as e:
        return f"Error getting user info: {str(e)}"


@mcp.tool(
    description="Returns the user roles. Optionally filter by tenant_id (UUID). If omitted, returns roles across all tenants. Returns full JSON.",
    annotations={"readOnlyHint": True}
)
async def get_user_roles(
    tenant_id: Optional[str] = Field(
        default=None,
        description="The tenant_id (UUID) to filter roles for a specific tenant. Omit to get roles across all tenants."),
) -> str:
    """Get user roles, optionally filtered by tenant"""
    try:
        params = {"tenant_id": tenant_id} if tenant_id else {}

        async with YeeduClient() as client:
            data = await client.get("/api/v1/user/info/roles", params=params)
            return json.dumps(data, indent=2)

    except Exception as e:
        return f"Error getting user roles: {str(e)}"



@mcp.tool(
    description="Retrieves a detailed billing usage report for a date range (max 90 days). Optionally filter by tenant IDs, cloud providers, cluster IDs, machine types, or labels. Returns a usage overview (total minutes, YCU, total clusters and tenants) and a per-cluster breakdown table: Tenant, Cluster, Provider, Machine Type, Minutes, YCU, Price/YCU.",
    annotations={"readOnlyHint": True}
)
async def get_billing_report(
    start_date: str = Field(description="Start date in YYYY-MM-DD format"),
    end_date: str = Field(description="End date in YYYY-MM-DD format"),
    tenant_ids: Optional[List[str]] = Field(
        default=None, description="Filter by tenant IDs (UUIDs)"
    ),
    cloud_providers: Optional[List[str]] = Field(
        default=None, description="Filter by cloud providers: GCP, AWS, Azure"
    ),
    cluster_ids: Optional[List[int]] = Field(
        default=None, description="Filter by cluster instance IDs"
    ),
    machine_type_ids: Optional[List[int]] = Field(
        default=None, description="Filter by machine type IDs"
    ),
    labels: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Filter by key-value labels. Example:\n"
            '{"env": "test_billing", "resource": "yeedu"}'
        )
    )
) -> str:
    """Get detailed billing usage report with YCU costs, cluster minutes, and tenant breakdown for specified date range (max 90 days)"""
    try:
        # Validate date range
        try:
            start = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.datetime.strptime(end_date, "%Y-%m-%d")
            if (end - start).days > 90:
                return "Error: Date range cannot exceed 90 days. HINT: Adjust start_date or end_date to be within a 90-day window."
            if start > end:
                return "Error: Start date must be before end date. HINT: Check the values of start_date and end_date."
        except ValueError:
            return "Error: Invalid date format. Use YYYY-MM-DD format. HINT: Ensure dates match YYYY-MM-DD format exactly."

        params = {}
        params["start_date"] = start_date
        params["end_date"] = end_date

        if tenant_ids:
            params["tenant_ids"] = tenant_ids
        if cloud_providers:
            params["cloud_providers"] = cloud_providers
        if cluster_ids:
            params["cluster_ids"] = cluster_ids
        if machine_type_ids:
            params["machine_type_ids"] = machine_type_ids
        if labels:
            params["labels"] = labels

        async with YeeduClient() as client:
            data = await client.get("/api/v1/platform/billing/usage", params=params)

            if not data or "usage" not in data:
                return "No billing data found for the specified date range"

            # Extract usage overview
            overview = data.get("usage_overview", {})
            total_minutes = overview.get("overall_total_minutes", 0)
            total_ycu = overview.get("overall_total_ycu", 0)
            total_clusters = overview.get("total_clusters_count", 0)
            total_tenants = overview.get("total_tenants", 0)

            # Format the response
            result = [
                "## Billing Usage Overview",
                f"Date Range: {start_date} to {end_date}",
                f"Total Minutes: {total_minutes:,.0f}",
                f"Total YCU: {total_ycu:,.2f}",
                f"Total Clusters: {total_clusters}",
                f"Total Tenants: {total_tenants}",
                "\n## Detailed Usage by Cluster",
                "| Tenant | Cluster | Provider | Machine Type | Minutes | YCU | Price/YCU |",
                "|--------|---------|----------|--------------|---------|-----|-----------|",
            ]

            # Sort clusters by tenant and name
            clusters = sorted(
                data["usage"],
                key=lambda x: (x.get("tenant_name", ""),
                               x.get("cluster_name", ""))
            )

            for cluster in clusters:
                tenant = cluster.get("tenant_name", "N/A")
                cluster_name = cluster.get("cluster_name", "N/A")
                provider = cluster.get("cloud_provider_name", "N/A")
                machine_type = cluster.get("machine_type", "N/A")
                minutes = cluster.get("total_daily_minutes", 0)
                ycu = cluster.get("total_daily_ycu", 0)
                price_ycu = cluster.get("machine_price_ycu_hour", 0)

                result.append(
                    f"| {tenant} | {cluster_name} | {provider} | {machine_type} | {minutes:,.0f} | {ycu:,.2f} | {price_ycu:.2f} |"
                )

            return "\n".join(result)

    except Exception as e:
        return f"Error getting billing report: {str(e)}"


