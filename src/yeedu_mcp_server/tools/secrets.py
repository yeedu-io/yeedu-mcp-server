"""Secrets and Git Credentials management tools for Yeedu MCP Server.

A secret's *value* never crosses this interface. Creating a secret, and
rotating the value of an existing one, are handed back to the user as a CLI
command plus a UI screen — see `utils/handoff.py` for why. What stays here is
everything that does not carry a value: listing, renaming, re-describing,
deleting, and referencing a secret by id (which is all the catalog link tools
need).
"""


from typing import Optional, Dict
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.handoff import secret_handoff
from yeedu_mcp_server.utils.params import opt


# ─── Helper: format secret list response ─────────────────────────────────────

def _csv_cell(value) -> str:
    """Render one CSV cell, keeping a free-text field on its own line.

    Descriptions are user-entered and really do arrive with newlines and commas
    in them, which would otherwise split one record across lines and shift
    every later column.
    """
    if value is None:
        return "N/A"
    text = " ".join(str(value).split())
    if not text:
        return "N/A"
    if "," in text or '"' in text:
        return '"' + text.replace('"', '""') + '"'
    return text


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
        secret_id = s.get(
            "workspace_secret_id",
            s.get("user_secret_id", s.get("tenant_secret_id", "N/A")),
        )
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


@mcp.tool(
    description="Lists or searches secrets configured at the tenant level, which every workspace in the tenant can use. Optionally filter by name (substring match) or secret_type. Returns CSV: secret_id, secret_name, secret_type, created_by, modified_date. Reading tenant secrets needs a tenant Admin or Platform Admin role.",
    annotations={"readOnlyHint": True}
)
async def list_tenant_secrets(
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
    """List all tenant secrets, optionally filtering by name or type."""
    try:
        async with YeeduClient() as client:
            params: Dict = {"pageNumber": page, "limit": limit}
            if secret_type:
                params["secret_type"] = secret_type

            if search_name:
                params["secret_name"] = search_name
                data = await client.get("/api/v1/tenant/secrets/search", params=params)
            else:
                data = await client.get("/api/v1/tenant/secrets", params=params)

            return _format_secrets_list(data, "tenant")
    except Exception as e:
        return f"Error listing tenant secrets: {str(e)}. HINT: Listing tenant secrets needs a tenant Admin or Platform Admin role; use list_workspace_secrets or list_user_secrets for the narrower scopes."


# ═══════════════════════════════════════════════════════════════════════════════
# MANAGE SECRETS (rename, re-describe, delete — never the value)
# ═══════════════════════════════════════════════════════════════════════════════

#: Per scope: URL segment, id parameter, CLI subcommand suffix, UI tab.
_SECRET_SCOPES = {
    "user": ("user", "user_secret_id", "user-secret", "User"),
    "workspace": ("workspace", "workspace_secret_id", "workspace-secret", "Workspace"),
    "tenant": ("tenant", "tenant_secret_id", "tenant-secret", "Tenant"),
}

#: The flags each secret type needs on create, from the CLI's SECRET_TYPE_ARGS.
_SECRET_TYPE_FLAGS = {
    "HIVE KERBEROS": "--principal --keytab_file_path",
    "HIVE BASIC": "--username --password",
    "DATABRICKS UNITY TOKEN": "--token",
    "ENVIRONMENT VARIABLE": "--value",
    "AWS ACCESS SECRET KEY PAIR": (
        "--aws_access_key_id --aws_secret_access_key --aws_default_region"),
    "AZURE SERVICE PRINCIPAL": (
        "--tenant_id --subscription_id --client_id --client_secret"),
    "GOOGLE SERVICE ACCOUNT": (
        "--type --project_id --private_key_id --private_key --client_email "
        "--client_id --auth_uri --token_uri --auth_provider_x509_cert_url "
        "--client_x509_cert_url"),
}

#: Which role may create or delete a secret at each scope.
_SECRET_ROLE_NOTE = {
    "user": "no role gate; a user secret is visible only to its creator",
    "workspace": "needs MANAGE on the workspace to create or delete, EDIT to edit",
    "tenant": "tenant Admin or Platform Admin only",
}


def _secret_cli_create(scope: str, secret_type: Optional[str],
                       workspace_id: Optional[int]) -> str:
    """Build the CLI command that creates a secret at this scope."""
    _, _, suffix, _ = _SECRET_SCOPES[scope]
    kind = (secret_type or "<type>").upper()
    flags = _SECRET_TYPE_FLAGS.get(kind, "<type-specific flags>")
    workspace = (
        f" --workspace_id {workspace_id}" if scope == "workspace" and workspace_id
        else " --workspace_id <id>" if scope == "workspace" else ""
    )
    return (
        f"yeedu secret create-{suffix} --name <name> "
        f"--secret_type '{kind}'{workspace} {flags}"
    )


@mcp.tool(description=(
    "Renames, re-describes or deletes a secret. scope is user, workspace or tenant. "
    "action=create and action=rotate are not performed here: they carry the secret value, "
    "so the tool returns the exact CLI command and the UI screen for the user to run that one "
    "step themselves, then you continue here by id. "
    "action=edit changes name and/or description only, and needs the secret's id and its "
    "secret_type (the platform requires the type on every update). "
    "action=delete needs the secret's id and requires confirm=True: the first call returns the "
    "secret it would delete and does nothing, so ask the user before repeating the call with "
    "confirm=True. "
    "Ids come from list_user_secrets, list_workspace_secrets and list_tenant_secrets."
))
async def manage_secret(
    scope: str = Field(description="Secret scope: user, workspace, or tenant"),
    action: str = Field(
        description="Action: create, rotate, edit, or delete. create and rotate return "
                    "instructions for the user instead of running."),
    secret_id: Optional[int] = Field(
        default=None, description="Secret ID (required for edit, rotate, and delete)"),
    secret_type: Optional[str] = Field(
        default=None,
        description="Secret type: ENVIRONMENT VARIABLE, HIVE KERBEROS, HIVE BASIC, "
                    "AWS ACCESS SECRET KEY PAIR, AZURE SERVICE PRINCIPAL, "
                    "GOOGLE SERVICE ACCOUNT, DATABRICKS UNITY TOKEN. Required for edit."),
    name: Optional[str] = Field(
        default=None, description="New secret name (for edit)"),
    description: Optional[str] = Field(
        default=None, description="New secret description (for edit)"),
    workspace_id: Optional[int] = Field(
        default=None, description="Workspace ID (required when scope='workspace')"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific secret.",
    ),
) -> str:
    """Rename, re-describe or delete a secret; hand value-bearing steps back."""
    try:
        scope = (opt(scope) or "").strip().lower()
        action = (opt(action) or "").strip().lower()
        secret_id = opt(secret_id)
        secret_type = opt(secret_type)
        name = opt(name)
        description = opt(description)
        workspace_id = opt(workspace_id)

        if scope not in _SECRET_SCOPES:
            return (
                f"Error: Invalid scope '{scope}'. "
                f"HINT: Must be one of: {', '.join(_SECRET_SCOPES)}."
            )
        if action not in ("create", "rotate", "edit", "delete"):
            return (
                f"Error: Invalid action '{action}'. "
                "HINT: Must be one of: create, rotate, edit, delete."
            )

        segment, id_field, suffix, tab = _SECRET_SCOPES[scope]
        url = f"/api/v1/{segment}/secret"
        list_tool = f"list_{scope}_secrets"

        if scope == "workspace" and action != "create" and workspace_id is None:
            return (
                "Error: workspace_id is required when scope='workspace'. "
                f"HINT: Call {list_tool} to find the secret and its workspace."
            )

        # Value-bearing operations: hand back, do not call the API.
        if action in ("create", "rotate"):
            cli = _secret_cli_create(scope, secret_type, workspace_id)
            if action == "rotate":
                cli = (
                    f"yeedu secret edit-{suffix} --{id_field} "
                    f"{secret_id if secret_id is not None else '<id>'} "
                    f"--secret_type '{(secret_type or '<type>').upper()}'"
                    + (f" --workspace_id {workspace_id}" if scope == "workspace" and workspace_id else "")
                    + f" {_SECRET_TYPE_FLAGS.get((secret_type or '').upper(), '<type-specific flags>')}"
                )
            return secret_handoff(
                what=(
                    f"a {scope} secret" if action == "create"
                    else f"a new value for {scope} secret {secret_id}"
                ),
                cli_command=cli,
                ui_nav=f"left rail → Secrets → {tab} tab → + Secret",
                ui_path=f"secrets/create-{suffix}",
                next_step=(
                    f"{list_tool} to pick up its id, then manage_catalog_secret_link "
                    "to attach it to a catalog"
                ),
                role_note=_SECRET_ROLE_NOTE[scope],
            )

        if secret_id is None:
            return (
                f"Error: secret_id is required for action='{action}'. "
                f"HINT: Call {list_tool} to find it."
            )

        params: Dict = {id_field: secret_id}
        if scope == "workspace":
            params["workspace_id"] = workspace_id

        if action == "delete":
            # Refuse before opening a connection, so an unreachable API cannot
            # turn a refusal into an error the agent retries its way past.
            if not confirmed(confirm):
                record = None
                try:
                    async with YeeduClient() as client:
                        data = await client.get(
                            f"/api/v1/{segment}/secrets", params=params)
                    record = _format_secrets_list(data, scope)
                except Exception:
                    record = None
                return confirmation_required(
                    f"{scope.capitalize()} secret",
                    f"{id_field}={secret_id}",
                    "manage_secret",
                    record=record,
                    impact=(
                        "The secret value is destroyed and cannot be recovered through Yeedu. "
                        "Any catalog link using it stops authenticating, so browsing that "
                        "catalog and jobs reading through it fail until a new secret is "
                        "created and linked."
                    ),
                    retry=(
                        f"scope='{scope}', secret_id={secret_id}"
                        + (f", workspace_id={workspace_id}" if scope == "workspace" else "")
                    ),
                )

            async with YeeduClient() as client:
                await client.delete(url, params=params)
            return f"{scope.capitalize()} secret {secret_id} deleted successfully."

        # edit — name and description only; the value is a separate, handed-back step.
        if not secret_type:
            return (
                "Error: secret_type is required for action='edit'. "
                f"HINT: Call {list_tool} and copy the secret_type column — the platform "
                "requires the type on every update, even when only the name changes."
            )
        if not name and description is None:
            return (
                "Error: action='edit' needs name and/or description. "
                "HINT: To change the secret's value, call this tool with action='rotate' "
                "and the user will be given the command to run."
            )

        body: Dict = {"secret_type": secret_type.upper()}
        if name:
            body["name"] = name
        if description is not None:
            body["description"] = description

        async with YeeduClient() as client:
            data = await client.put(url, params=params, json=body)

        if isinstance(data, dict) and data.get("error_message"):
            return (
                f"Error: {data['error_message']}. "
                f"HINT: Confirm the secret_type matches the secret — call {list_tool}."
            )

        changed = ", ".join(
            part for part in (
                f"name='{name}'" if name else None,
                "description" if description is not None else None,
            ) if part
        )
        return f"{scope.capitalize()} secret {secret_id} updated ({changed})."

    except Exception as e:
        return (
            f"Error running action '{action}' on {scope} secret: {str(e)}. "
            "HINT: Confirm the id with list_user_secrets, list_workspace_secrets or "
            "list_tenant_secrets, and note that a workspace secret needs workspace_id "
            "as well."
        )


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


_GIT_PROVIDERS = ("GitHub", "BitBucket Cloud", "GitLab", "Azure DevOps Services")


def _git_credential_record(data) -> Optional[str]:
    """Render one git credential as a CSV block, token never included."""
    if isinstance(data, dict) and "error_message" in data:
        return None
    items = data if isinstance(data, list) else (
        data.get("data", [data]) if isinstance(data, dict) else [])
    rows = [c for c in items if isinstance(c, dict)]
    if not rows:
        return None
    lines = ["credential_id,git_provider,username"]
    for cred in rows:
        provider = cred.get("lookup_git_provider") or {}
        lines.append(
            f"{cred.get('git_credential_id', 'N/A')},"
            f"{provider.get('name', 'N/A') if isinstance(provider, dict) else provider},"
            f"{cred.get('git_username', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool(description=(
    "Manages the current user's Git credentials — the username and personal access token "
    "Yeedu uses to clone and push a workspace's repository. "
    "action=get needs credential_id and returns that credential (never the token). "
    "action=create and action=rotate carry the token, so they are not performed here: the tool "
    "returns the exact CLI command and the UI screen for the user to run that one step "
    "themselves, then you continue here by id. "
    "action=edit changes the username on an existing credential and needs credential_id and username. "
    "action=delete needs credential_id and requires confirm=True: the first call returns the "
    "credential it would delete and does nothing, so ask the user before repeating the call "
    "with confirm=True. "
    "Use list_git_credentials to find credential_id."
))
async def manage_git_credential(
    action: str = Field(
        description="Action: get, create, rotate, edit, or delete. create and rotate "
                    "return instructions for the user instead of running."),
    credential_id: Optional[int] = Field(
        default=None,
        description="Git credential ID (required for get, rotate, edit, and delete)"),
    git_provider: Optional[str] = Field(
        default=None,
        description="Provider for create: GitHub, BitBucket Cloud, GitLab, or Azure DevOps Services"),
    username: Optional[str] = Field(
        default=None, description="Git username (for edit, or to fill into the create command)"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific credential.",
    ),
) -> str:
    """Get, edit or delete a git credential; hand token-bearing steps back."""
    try:
        action = (opt(action) or "").strip().lower()
        credential_id = opt(credential_id)
        git_provider = opt(git_provider)
        username = opt(username)
        if action not in ("get", "create", "rotate", "edit", "delete"):
            return (
                f"Error: Invalid action '{action}'. "
                "HINT: Must be one of: get, create, rotate, edit, delete."
            )

        url = "/api/v1/user/git/credential"

        if action in ("create", "rotate"):
            if action == "create":
                provider = git_provider or "<provider>"
                if git_provider and git_provider not in _GIT_PROVIDERS:
                    return (
                        f"Error: Invalid git_provider '{git_provider}'. "
                        f"HINT: Must be one of: {', '.join(_GIT_PROVIDERS)}."
                    )
                cli = (
                    f"yeedu git-credentials create --git_provider '{provider}' "
                    f"--username {username or '<username>'} --token <token>"
                )
                what = "a Git credential"
            else:
                cli = (
                    "yeedu git-credentials edit --git_credential_id "
                    f"{credential_id if credential_id is not None else '<id>'} "
                    "--token <token>"
                )
                what = f"a new token for Git credential {credential_id}"
            return secret_handoff(
                what=what,
                cli_command=cli,
                ui_nav="user button → Settings → USER → Linked Accounts",
                ui_path="settings/linked-accounts",
                next_step=(
                    "list_git_credentials to pick up its id, then git_clone_repo or "
                    "git_sync to use it"
                ),
                role_note="every role; in 2.10.1 only GitHub is enabled in the UI",
            )

        if credential_id is None:
            return (
                f"Error: credential_id is required for action='{action}'. "
                "HINT: Call list_git_credentials to find it."
            )

        params: Dict = {"git_credential_id": credential_id}

        if action == "get":
            async with YeeduClient() as client:
                data = await client.get(url, params=params)
            record = _git_credential_record(data)
            if not record:
                return (
                    f"No git credential with id {credential_id}. "
                    "HINT: Call list_git_credentials to see the ones that exist."
                )
            return record

        if action == "delete":
            # Refuse before opening a connection, so an unreachable API cannot
            # turn a refusal into an error the agent retries its way past.
            if not confirmed(confirm):
                record = None
                try:
                    async with YeeduClient() as client:
                        data = await client.get(url, params=params)
                    record = _git_credential_record(data)
                except Exception:
                    record = None
                return confirmation_required(
                    "Git credential",
                    f"git_credential_id={credential_id}",
                    "manage_git_credential",
                    record=record,
                    impact=(
                        "The stored token is destroyed. Every workspace repository that "
                        "authenticates with this credential fails to clone, pull or push "
                        "until a new credential is created, and the token itself cannot be "
                        "recovered from Yeedu."
                    ),
                    retry=f"credential_id={credential_id}",
                )

            async with YeeduClient() as client:
                await client.delete(url, params=params)
            return f"Git credential {credential_id} deleted successfully."

        # edit — username only; a new token is the handed-back 'rotate' action.
        if not username:
            return (
                "Error: action='edit' needs username. "
                "HINT: To replace the token, call this tool with action='rotate' and the "
                "user will be given the command to run."
            )

        async with YeeduClient() as client:
            data = await client.put(
                url, params=params, json={"git_username": username})

        if isinstance(data, dict) and data.get("error_message"):
            return (
                f"Error: {data['error_message']}. "
                "HINT: Confirm the credential_id with list_git_credentials."
            )
        return f"Git credential {credential_id} updated (username='{username}')."

    except Exception as e:
        return (
            f"Error running action '{action}' on git credential: {str(e)}. "
            "HINT: Call list_git_credentials to confirm the credential_id."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# API TOKENS
#
# A token *is* the credential, and the platform returns its value once, at
# creation. Creating one here would print that value into the model's context
# and the transcript, so create is handed back like any other secret. Listing
# and revoking carry no value and stay on MCP.
# ═══════════════════════════════════════════════════════════════════════════════


def _token_table(data) -> Optional[str]:
    """Render token metadata as CSV. The token value is never in a list response."""
    if isinstance(data, dict) and "error_message" in data:
        return None
    items = data if isinstance(data, list) else (
        data.get("data", []) if isinstance(data, dict) else [])
    rows = [t for t in items if isinstance(t, dict)]
    if not rows:
        return None
    lines = ["token_id,description,created_by,expiry"]
    for token in rows:
        # The platform records who created a token and when it expires, but not
        # when it was created, so there is no created date to show.
        created_by = token.get("created_by")
        author = (created_by.get("username")
                  if isinstance(created_by, dict) else None)
        expiry = token.get("expiry_time") or token.get("expiry_date") or "never"
        if isinstance(expiry, str) and expiry.strip().lower() == "infinity":
            expiry = "never"
        lines.append(
            f"{token.get('token_id', token.get('user_token_id', 'N/A'))},"
            f"{_csv_cell(token.get('description'))},"
            f"{_csv_cell(author)},"
            f"{_csv_cell(expiry)}"
        )
    return "\n".join(lines)


@mcp.tool(description=(
    "Manages the current user's Yeedu API tokens. "
    "action=list returns the tokens that exist (CSV: token_id, description, created_by, "
    "expiry) — never a token value, which the platform only returns once at creation. "
    "The platform records no creation date for a token; an expiry of 'infinity' is "
    "reported as never. "
    "action=create is not performed here, because the new token value would pass through the "
    "model's context: the tool returns the exact CLI command and the UI screen for the user "
    "to run that step themselves. "
    "action=delete needs token_id and requires confirm=True: the first call returns the token "
    "it would revoke and does nothing, so ask the user before repeating the call with "
    "confirm=True."
))
async def manage_token(
    action: str = Field(
        description="Action: list, create, or delete. create returns instructions for the "
                    "user instead of running."),
    token_id: Optional[int] = Field(
        default=None, description="Token ID (required for delete)"),
    description: Optional[str] = Field(
        default=None, description="What the token is for — filled into the create command"),
    timeout: Optional[str] = Field(
        default=None,
        description="Token lifetime for the create command, e.g. 24h, 2 days, 1700s, or "
                    "infinity for no expiry"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific token.",
    ),
) -> str:
    """List or revoke API tokens; hand token creation back to the user."""
    try:
        action = (opt(action) or "").strip().lower()
        token_id = opt(token_id)
        description = opt(description)
        timeout = opt(timeout)
        limit = opt(limit) or 20
        page = opt(page) or 1

        if action not in ("list", "create", "delete"):
            return (
                f"Error: Invalid action '{action}'. "
                "HINT: Must be one of: list, create, delete."
            )

        if action == "create":
            return secret_handoff(
                what="a Yeedu API token",
                cli_command=(
                    "yeedu token create "
                    f"--description '{description or '<what it is for>'}' "
                    f"--timeout={timeout or '<24h | 2 days | infinity>'}"
                ),
                ui_nav="user button → Settings → USER → Tokens → + Token",
                ui_path="settings/tokens",
                next_step="manage_token(action='list') to see it listed by id",
                role_note=(
                    "every role; the token carries the roles of the user who created it, "
                    "and its value is shown once and cannot be retrieved again"
                ),
            )

        if action == "list":
            async with YeeduClient() as client:
                data = await client.get(
                    "/api/v1/user/tokens",
                    params={"pageNumber": page, "limit": limit},
                )
            table = _token_table(data)
            return table or "No API tokens found for the current user."

        if token_id is None:
            return (
                "Error: token_id is required for action='delete'. "
                "HINT: Call manage_token(action='list') to find it."
            )

        # Refuse before opening a connection, so an unreachable API cannot turn
        # a refusal into an error the agent retries its way past.
        if not confirmed(confirm):
            record = None
            try:
                async with YeeduClient() as client:
                    data = await client.get(
                        "/api/v1/user/tokens",
                        params={"pageNumber": 1, "limit": 100},
                    )
                rows = data.get("data", []) if isinstance(data, dict) else []
                match = [
                    t for t in rows if isinstance(t, dict)
                    and t.get("token_id", t.get("user_token_id")) == token_id
                ]
                record = _token_table(match)
            except Exception:
                record = None
            return confirmation_required(
                "API token",
                f"token_id={token_id}",
                "manage_token",
                record=record,
                impact=(
                    "The token stops working immediately. Anything authenticating with it — "
                    "a script, a CI job, another MCP client, possibly this session — starts "
                    "failing, and the value cannot be recovered, so a replacement has to be "
                    "created and distributed."
                ),
                retry=f"token_id={token_id}",
            )

        async with YeeduClient() as client:
            await client.delete(f"/api/v1/user/token/{token_id}")
        return f"API token {token_id} revoked successfully."

    except Exception as e:
        return (
            f"Error running action '{action}' on API token: {str(e)}. "
            "HINT: Call manage_token(action='list') to confirm the token_id."
        )

