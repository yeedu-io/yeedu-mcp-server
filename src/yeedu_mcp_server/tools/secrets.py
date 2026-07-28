"""Secrets and Git Credentials management tools for Yeedu MCP Server."""


from typing import Optional, Dict
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp


# ─── Helper: format secret list response ─────────────────────────────────────

def _format_secrets_list(data: dict, scope: str) -> str:
    """Format secrets list API response to CSV."""
    if isinstance(data, dict) and "error_message" in data:
        return f"Error: {data['error_message']}"
    if isinstance(data, dict) and "error" in data:
        return f"No {scope} secrets found: {data['error']}"
    if not data or "data" not in data:
        return f"No {scope} secrets found."

    lines = ["secret_id,secret_name,secret_type,created_by,modified_date"]
    for s in data["data"]:
        secret_id = s.get("workspace_secret_id",
                          s.get("user_secret_id", "N/A"))
        created_by = (
            s.get("created_by", {}).get("username", "N/A")
            if isinstance(s.get("created_by"), dict)
            else s.get("created_by", "N/A")
        )
        lines.append(
            f"{secret_id},"
            f"{s.get('secret_name', s.get('name', 'N/A'))},"
            f"{s.get('secret_type', s.get('secret_type_name', 'N/A'))},"
            f"{created_by},"
            f"{s.get('last_update_date', s.get('modified_date', 'N/A'))}"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# LIST SECRETS
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description="Lists or searches secrets configured at the workspace level. Optionally filter by name (substring match) or secret_type. Returns CSV: secret_id, secret_name, secret_type, created_by, modified_date with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_workspace_secrets(
    workspace_id: int = Field(description="Workspace ID"),
    search_name: Optional[str] = Field(
        default=None, description="Filter secrets by name (substring match)"
    ),
    secret_type: Optional[str] = Field(
        default=None,
        description="Filter by type: ENVIRONMENT VARIABLE, HIVE KERBEROS, HIVE BASIC, AWS ACCESS SECRET KEY PAIR, AZURE SERVICE PRINCIPAL, GOOGLE SERVICE ACCOUNT, DATABRICKS UNITY TOKEN",
    ),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
) -> str:
    """List all workspace secrets, optionally filtering by name or type. Output is base64 encoded."""
    try:
        async with YeeduClient() as client:
            params: Dict = {
                "workspace_id": workspace_id,
                "pageNumber": page,
                "limit": limit,
            }
            if secret_type:
                params["secret_type"] = secret_type

            if search_name:
                params["secret_name"] = search_name
                data = await client.get("/api/v1/workspace/secrets/search", params=params)
            else:
                data = await client.get("/api/v1/workspace/secrets", params=params)

            return _format_secrets_list(data, "workspace")
    except Exception as e:
        return f"Error listing workspace secrets: {str(e)}. HINT: Double-check that workspace_id is correct and you have permission to view it."


@mcp.tool(
    description="Lists or searches secrets owned by the current user (user-scoped). Optionally filter by name (substring match) or secret_type. Returns CSV: secret_id, secret_name, secret_type, created_by, modified_date with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_user_secrets(
    search_name: Optional[str] = Field(
        default=None, description="Filter secrets by name (substring match)"
    ),
    secret_type: Optional[str] = Field(
        default=None,
        description="Filter by type: ENVIRONMENT VARIABLE, HIVE KERBEROS, HIVE BASIC, AWS ACCESS SECRET KEY PAIR, AZURE SERVICE PRINCIPAL, GOOGLE SERVICE ACCOUNT, DATABRICKS UNITY TOKEN",
    ),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
) -> str:
    """List all user secrets, optionally filtering by name or type. Output is base64 encoded."""
    try:
        async with YeeduClient() as client:
            params: Dict = {"pageNumber": page, "limit": limit}
            if secret_type:
                params["secret_type"] = secret_type

            if search_name:
                params["secret_name"] = search_name
                data = await client.get("/api/v1/user/secrets/search", params=params)
            else:
                data = await client.get("/api/v1/user/secrets", params=params)

            return _format_secrets_list(data, "user")
    except Exception as e:
        return f"Error listing user secrets: {str(e)}. HINT: Verify you are authenticated and have permissions to view user secrets."


# ═══════════════════════════════════════════════════════════════════════════════
# GIT CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description="Lists all Git credentials for the current user, optionally filtered by git_provider (GitHub, BitBucket Cloud, GitLab, Azure DevOps Services). Returns CSV: credential_id, git_provider, username.",
    annotations={"readOnlyHint": True}
)
async def list_git_credentials(
    git_provider: Optional[str] = Field(
        default=None,
        description="Filter by provider: GitHub, BitBucket Cloud, GitLab, Azure DevOps Services",
    ),
) -> str:
    """List Git credentials, optionally filtered by provider."""
    try:
        async with YeeduClient() as client:
            params: Dict = {}
            if git_provider:
                params["git_provider"] = git_provider

            data = await client.get("/api/v1/user/git/credentials", params=params if params else None)

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            # Response is a plain array (not paginated)
            items = data if isinstance(data, list) else data.get("data", [])
            if not items:
                return "No git credentials found."

            lines = ["credential_id,git_provider,username"]
            for cred in items:
                provider = cred.get("lookup_git_provider") or {}
                lines.append(
                    f"{cred.get('git_credential_id', 'N/A')},"
                    f"{provider.get('name', 'N/A') if isinstance(provider, dict) else provider},"
                    f"{cred.get('git_username', 'N/A')}"
                )
            return "\n".join(lines)
    except Exception as e:
        return f"Error listing git credentials: {str(e)}. HINT: Ensure git_provider filter matches supported providers: GitHub, BitBucket Cloud, GitLab, or Azure DevOps Services."

