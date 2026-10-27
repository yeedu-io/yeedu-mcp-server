"""Tenant, user profile, and role listing tools for Yeedu MCP Server.

`list_tenants` answers "which tenants can I see", from the current user's own
view. `manage_tenant` is the Platform Admin view of the same objects, and the
only place a tenant is created, renamed or deleted.
"""

from typing import Any, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.utils.params import opt
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


# ---------------------------------------------------------------------------
# Tenant administration (Platform Admin)
#
# Every endpoint here lives under /platform/admin, so a caller without the
# Platform Admin role gets a 403 rather than a partial answer. The tools say
# so in their hints instead of pretending the object does not exist.
#
# A tenant owns its workspaces, clusters, secrets and billing history, which
# is why delete is gated twice over: once by the confirm flag, and once by
# listing what the tenant contains before anything is sent.
# ---------------------------------------------------------------------------

_TENANT_ACTIONS = ("list", "search", "get", "create", "edit", "delete", "user")


def _tenant_rows(data: Any) -> list:
    """Pull tenant records out of a response of any shape."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        if data.get("tenant_id"):
            return [data]
    return []


def _tenant_table(data: Any, page: int, limit: int) -> str:
    """Render tenant records as a table with a pagination footer."""
    rows = _tenant_rows(data)
    if not rows:
        return ""
    table = serialize_table(
        [[r.get("tenant_id"), r.get("name"), r.get("description", "N/A")]
         for r in rows],
        headers=["Tenant ID", "Name", "Description"],
    )
    if isinstance(data, dict):
        result_set = data.get("result_set")
        if isinstance(result_set, dict):
            table += (
                f"\nPage {result_set.get('current_page', page)} of "
                f"{result_set.get('total_pages', 1)} | "
                f"Total Items: {result_set.get('total_objects', len(rows))} | "
                f"Items Per Page: {limit}"
            )
    return table


@mcp.tool(description=(
    "Administers tenants. Every action needs the Platform Admin role. "
    "action=list returns all tenants on the platform; action=search needs tenant_name; "
    "action=get needs tenant_id or tenant_name; action=user needs user_id and returns the "
    "tenants that user belongs to. "
    "action=create needs name and accepts description. "
    "action=edit needs tenant_id or tenant_name plus name and/or description. "
    "action=delete needs tenant_id or tenant_name and requires confirm=True: the first call "
    "returns the tenant it would delete and does nothing, so ask the user before repeating "
    "the call with confirm=True. "
    "For the tenants the current user can simply see and use, call list_tenants instead."
))
async def manage_tenant(
    action: str = Field(
        description="Action: list, search, get, create, edit, delete, or user"),
    tenant_id: Optional[str] = Field(
        default=None, description="Tenant ID (UUID) — selects the tenant for get, edit and delete"),
    tenant_name: Optional[str] = Field(
        default=None,
        description="Tenant name — selects the tenant for get, edit and delete, and is the "
                    "search term for action='search'"),
    name: Optional[str] = Field(
        default=None, description="New tenant name (required for create, optional for edit)"),
    description: Optional[str] = Field(
        default=None, description="Tenant description (for create and edit)"),
    user_id: Optional[int] = Field(
        default=None, description="User ID (required for action='user')"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific tenant.",
    ),
) -> str:
    """List, search, create, edit or delete a tenant as Platform Admin."""
    try:
        action = (opt(action) or "").strip().lower()
        tenant_id = opt(tenant_id)
        tenant_name = opt(tenant_name)
        name = opt(name)
        description = opt(description)
        user_id = opt(user_id)
        limit = opt(limit) or 20
        page = opt(page) or 1

        if action not in _TENANT_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_TENANT_ACTIONS)}."
            )

        base = "/api/v1/platform/admin"
        selector = {}
        if tenant_id:
            selector["tenant_id"] = tenant_id
        elif tenant_name:
            selector["tenant_name"] = tenant_name

        if action in ("get", "edit", "delete") and not selector:
            return (
                f"Error: tenant_id or tenant_name is required for action='{action}'. "
                "HINT: Call manage_tenant(action='list') to see the tenants on this platform."
            )

        if action == "list":
            async with YeeduClient() as client:
                data = await client.get(
                    f"{base}/tenants", params={"pageNumber": page, "limit": limit})
            table = _tenant_table(data, page, limit)
            return table or (
                "No tenants returned. HINT: This action needs the Platform Admin role; "
                "call list_tenants for the tenants your own user can see."
            )

        if action == "search":
            if not tenant_name:
                return (
                    "Error: tenant_name is required for action='search'. "
                    "HINT: It is a substring match on the tenant name."
                )
            async with YeeduClient() as client:
                data = await client.get(
                    f"{base}/tenants/search",
                    params={"tenant_name": tenant_name,
                            "pageNumber": page, "limit": limit},
                )
            table = _tenant_table(data, page, limit)
            return table or f"No tenants matching '{tenant_name}'."

        if action == "user":
            if user_id is None:
                return (
                    "Error: user_id is required for action='user'. "
                    "HINT: Resolve a username to its id with "
                    "browse_directory(principal='user', action='match', name='<username>')."
                )
            async with YeeduClient() as client:
                data = await client.get(
                    f"{base}/user/{user_id}/tenants",
                    params={"pageNumber": page, "limit": limit},
                )
            table = _tenant_table(data, page, limit)
            return table or f"User {user_id} belongs to no tenants."

        if action == "get":
            async with YeeduClient() as client:
                data = await client.get(f"{base}/tenant", params=selector)
            table = _tenant_table(data, page, limit)
            return table or (
                f"No tenant matching {selector}. "
                "HINT: Call manage_tenant(action='list') to see what exists."
            )

        if action == "delete":
            # Refuse before opening a connection, so an unreachable API cannot
            # turn a refusal into an error the agent retries its way past.
            if not confirmed(confirm):
                record = None
                try:
                    async with YeeduClient() as client:
                        data = await client.get(f"{base}/tenant", params=selector)
                    record = _tenant_table(data, page, limit) or None
                except Exception:
                    record = None
                return confirmation_required(
                    "Tenant",
                    ", ".join(f"{k}={v}" for k, v in selector.items()),
                    "manage_tenant",
                    record=record,
                    impact=(
                        "Everything scoped to the tenant goes with it: its workspaces, "
                        "notebooks and job definitions, its cluster and infrastructure "
                        "configurations, its secrets, its role assignments and its billing "
                        "history. Users keep their accounts but lose access to all of it. "
                        "This cannot be undone."
                    ),
                    retry=", ".join(
                        f"{k}='{v}'" for k, v in selector.items()),
                )

            async with YeeduClient() as client:
                await client.delete(f"{base}/tenant", params=selector)
            return (
                "Tenant deleted successfully ("
                + ", ".join(f"{k}={v}" for k, v in selector.items()) + ")."
            )

        # create / edit
        body = {}
        if name:
            body["name"] = name
        if description is not None:
            body["description"] = description

        if action == "create":
            if not name:
                return (
                    "Error: name is required for action='create'. "
                    "HINT: A tenant also takes an optional description."
                )
            async with YeeduClient() as client:
                data = await client.post(f"{base}/tenant", json=body)
            verb = "created"
        else:
            if not body:
                return (
                    "Error: action='edit' needs name and/or description. "
                    "HINT: Those are the only tenant fields that can change."
                )
            async with YeeduClient() as client:
                data = await client.put(f"{base}/tenant", params=selector, json=body)
            verb = "updated"

        if isinstance(data, dict) and data.get("error_message"):
            return (
                f"Error: {data['error_message']}. "
                "HINT: Tenant names are unique, and this action needs the Platform Admin role."
            )

        table = _tenant_table(data, page, limit)
        return f"Tenant {verb} successfully." + (f"\n\n{table}" if table else "")

    except Exception as e:
        return (
            f"Error running action '{action}' on tenant: {str(e)}. "
            "HINT: Every manage_tenant action needs the Platform Admin role; call "
            "get_user_roles to check yours, and list_tenants for the tenants your user "
            "can see without it."
        )

