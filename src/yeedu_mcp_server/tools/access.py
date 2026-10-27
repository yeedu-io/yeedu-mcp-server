"""Workspace access control for Yeedu MCP Server.

Users, groups, roles and permissions themselves are synced from the identity
provider and cannot be created here. What Yeedu does own is the mapping of an
existing user or group to a permission on a workspace, and that is what this
module manages: listing who has access, resolving a username to an id, and
granting or revoking one permission.

Revoking is gated behind `confirm` like every other destructive operation, and
the gate reads back the access row first, because a revoke aimed at the wrong
user id is the mistake worth catching.
"""

from typing import Any, Optional

from pydantic import Field

from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.utils.params import opt

_ACTIONS = ("list", "search", "match", "get", "grant", "revoke")

#: Per principal kind: URL segment, plural segment, name parameter, id parameter.
_PRINCIPALS = {
    "user": ("user", "users", "username", "user_id"),
    "group": ("group", "groups", "groupname", "group_id"),
}


def _rows(data: Any) -> list:
    """Pull the record list out of a Yeedu response of any shape."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "result", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    return []


def _holders(data: Any, many: str, id_key: str) -> list:
    """The principals in a response that describes who holds something.

    These endpoints answer with one object for the thing being described — a
    workspace, a role, a group — and nest the principals inside it under
    `users` or `groups`, a level deeper than a plain listing. Taking the outer
    object as a row yields one row of blanks, and when nothing holds the thing
    the nested list is simply absent, which is an empty answer rather than a
    record. `id_key` is what tells the two apart.
    """
    if isinstance(data, dict):
        nested = data.get(many)
        if isinstance(nested, list):
            return [r for r in nested if isinstance(r, dict)]

    unwrapped = []
    for row in _rows(data):
        if not isinstance(row, dict):
            continue
        nested = row.get(many)
        if isinstance(nested, list):
            unwrapped += [r for r in nested if isinstance(r, dict)]
        elif id_key in row:
            unwrapped.append(row)
    return unwrapped


def _access_row(record: dict, kind: str) -> list:
    """Flatten one access record to id, name, permission, granted-by."""
    permission = record.get("permission")
    if not isinstance(permission, dict):
        permission = {}
    created_by = record.get("created_by")
    if isinstance(created_by, dict):
        granted_by = created_by.get("username") or "N/A"
    else:
        granted_by = record.get("created_by_username") or created_by or "N/A"
    return [
        record.get(f"{kind}_id", "N/A"),
        record.get("username") or record.get("group_name")
        or record.get("display_name") or record.get("groupname")
        or record.get("name") or "N/A",
        permission.get("permission_id", record.get("permission_id", "N/A")),
        permission.get("name", "N/A"),
        granted_by,
    ]


def _page_note(data: Any, page: int) -> str:
    """Render the pagination footer, when the response carries one."""
    if not isinstance(data, dict):
        return ""
    result_set = data.get("result_set")
    if not isinstance(result_set, dict):
        return ""
    return (
        f"\n\nPage {result_set.get('current_page', page)} of "
        f"{result_set.get('total_pages', 1)} | "
        f"Total: {result_set.get('total_objects', '?')}"
    )


@mcp.tool(description=(
    "Manages who has access to a workspace. principal='user' or 'group'. "
    "action=list returns the access rows on the workspace (CSV: id, name, permission_id, permission, granted_by), "
    "optionally filtered by permission_id. "
    "action=search needs name and returns the matching access rows. "
    "action=match needs name and resolves one exact username/groupname to its id. "
    "action=get needs user_id or group_id and returns that principal's permission on the workspace. "
    "action=grant needs user_id or group_id plus permission_id. "
    "action=revoke needs the same and requires confirm=True: the first call returns the access it would revoke and does nothing, "
    "so ask the user before repeating the call with confirm=True. "
    "Users, groups and permissions cannot be created here — they come from the identity provider; "
    "call list_workspace_permissions for the permission_id values."
))
async def manage_workspace_access(
    workspace_id: int = Field(description="Workspace ID"),
    principal: str = Field(
        description="Principal kind: user or group"),
    action: str = Field(
        description="Action: list, search, match, get, grant, or revoke"),
    user_id: Optional[int] = Field(
        default=None, description="User ID (for get, grant, revoke when principal='user')"),
    group_id: Optional[int] = Field(
        default=None, description="Group ID (for get, grant, revoke when principal='group')"),
    name: Optional[str] = Field(
        default=None, description="Username or group name (for search and match)"),
    permission_id: Optional[int] = Field(
        default=None, description="Permission ID — required for grant and revoke, optional filter for list and search"),
    limit: int = Field(default=20, ge=1, le=100,
                       description="Items per page (for list and search)"),
    page: int = Field(default=1, ge=1,
                      description="Page number (for list and search)"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='revoke'. Set it only after the user has confirmed this specific access.",
    ),
) -> str:
    """List, search, grant or revoke workspace access for a user or group."""
    try:
        principal = (opt(principal) or "").strip().lower()
        action = (opt(action) or "").strip().lower()
        user_id = opt(user_id)
        group_id = opt(group_id)
        name = opt(name)
        permission_id = opt(permission_id)
        limit = opt(limit) or 20
        page = opt(page) or 1

        if principal not in _PRINCIPALS:
            return (
                f"Error: Invalid principal '{principal}'. "
                "HINT: Must be 'user' or 'group'."
            )
        if action not in _ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_ACTIONS)}."
            )

        one, many, name_field, id_field = _PRINCIPALS[principal]
        principal_id = user_id if principal == "user" else group_id
        base = f"/api/v1/ac/workspace/{workspace_id}"

        if action in ("search", "match") and not name:
            return (
                f"Error: name is required for action='{action}'. "
                f"HINT: It is the {name_field}; call manage_workspace_access with action='list' to see the ones already on this workspace."
            )
        if action in ("get", "grant", "revoke") and principal_id is None:
            return (
                f"Error: {id_field} is required for action='{action}'. "
                f"HINT: Call manage_workspace_access with action='match' and the {name_field} to resolve the id."
            )
        if action in ("grant", "revoke") and permission_id is None:
            return (
                f"Error: permission_id is required for action='{action}'. "
                "HINT: Call list_workspace_permissions to see the available permissions."
            )

        headers = [id_field, name_field, "permission_id",
                   "permission", "granted_by"]

        if action in ("list", "search"):
            params: dict = {"pageNumber": page, "limit": limit}
            if permission_id is not None:
                params["permission_id"] = permission_id
            endpoint = f"{base}/{many}/permission"
            if action == "search":
                params[name_field] = name
                endpoint = f"{endpoint}/search"

            async with YeeduClient() as client:
                data = await client.get(endpoint, params=params)

            rows = [_access_row(r, one) for r in _holders(data, many, id_field)]
            if not rows:
                return (
                    f"No {many} with access to workspace {workspace_id}"
                    + (f" matching '{name}'" if action == "search" else "")
                    + (f" with permission_id {permission_id}" if permission_id is not None else "")
                    + "."
                )
            return serialize_table(rows, headers=headers) + _page_note(data, page)

        if action == "match":
            async with YeeduClient() as client:
                data = await client.get(f"{base}/{one}/match/{name}")
            records = [r for r in _rows(data) if isinstance(r, dict)]
            if not records:
                return (
                    f"No {one} named '{name}'. "
                    f"HINT: {one.capitalize()}s come from the identity provider; check the spelling."
                )
            record = records[0]
            rows = [
                [id_field, record.get(f"{one}_id", "N/A")],
                [name_field, record.get("username") or record.get("groupname")
                 or record.get("name") or name],
            ]
            return serialize_table(rows, headers=["Field", "Value"])

        if action == "get":
            async with YeeduClient() as client:
                data = await client.get(
                    f"{base}/{one}/{principal_id}/permission")
            rows = [_access_row(r, one) for r in _holders(data, many, id_field)]
            if not rows:
                return (
                    f"{one.capitalize()} {principal_id} has no permission on workspace {workspace_id}."
                )
            return serialize_table(rows, headers=headers)

        if action == "grant":
            body = {id_field: principal_id, "permission_id": permission_id}
            async with YeeduClient() as client:
                await client.post(f"{base}/{one}", json=body)
            return (
                f"Granted permission_id {permission_id} to {one} {principal_id} "
                f"on workspace {workspace_id}."
            )

        # revoke — refuse before opening a connection, so an unreachable API
        # cannot turn a refusal into an error the agent retries its way past.
        if not confirmed(confirm):
            record = None
            try:
                async with YeeduClient() as client:
                    data = await client.get(
                        f"{base}/{one}/{principal_id}/permission")
                rows = [_access_row(r, one)
                        for r in _rows(data) if isinstance(r, dict)]
                if rows:
                    record = serialize_table(rows, headers=headers)
            except Exception:
                record = None
            return confirmation_required(
                f"{one.capitalize()} workspace access",
                f"{id_field}={principal_id}, permission_id={permission_id}",
                "manage_workspace_access",
                action="revoke",
                record=record,
                impact=(
                    f"The {one} loses this permission on workspace {workspace_id}. "
                    f"Re-granting needs the same permission_id, and a {one} whose access came from a group keeps that group's access."
                ),
                retry=(
                    f"workspace_id={workspace_id}, principal='{principal}', "
                    f"{id_field}={principal_id}, permission_id={permission_id}"
                ),
            )

        async with YeeduClient() as client:
            await client.delete(
                f"{base}/{one}/{principal_id}/permission/{permission_id}")
        return (
            f"Revoked permission_id {permission_id} from {one} {principal_id} "
            f"on workspace {workspace_id}."
        )

    except Exception as e:
        return (
            f"Error running action '{action}' on workspace access: {str(e)}. "
            "HINT: Confirm the workspace with list_workspaces, the permission_id with list_workspace_permissions, "
            f"and the principal id with manage_workspace_access(action='match')."
        )


# ---------------------------------------------------------------------------
# Tenant directory
#
# Users and groups are synced from the identity provider, so nothing here
# creates one. `match` and `sync` reach past the tenant to the IdP: match
# resolves a name the tenant may not have seen yet, and sync pulls that
# account's current identity into Yeedu so it can be given a role.
#
# Every action is tenant-scoped. With no tenant_id it reads the caller's own
# tenant through /admin and needs a tenant Admin role; with tenant_id it reads
# any tenant through /platform/admin and needs Platform Admin.
# ---------------------------------------------------------------------------

_DIRECTORY_ACTIONS = (
    "list", "search", "match", "get", "sync", "members", "memberships")

#: Per principal kind: singular segment, plural segment, name field, id field.
_DIR_PRINCIPALS = {
    "user": ("user", "users", "username", "user_id"),
    "group": ("group", "groups", "groupname", "group_id"),
}


def _directory_row(record: dict, kind: str) -> list:
    """Flatten one directory record to id, name, email, source.

    A user and a group are named by different fields: a user carries
    `username`/`email`, a group carries `group_name`/`group_mail`. The CLI's
    `--groupname` is a request parameter, not a response field, so a group read
    through `groupname` comes back empty.
    """
    return [
        record.get(f"{kind}_id", "N/A"),
        record.get("username") or record.get("group_name")
        or record.get("display_name") or record.get("groupname")
        or record.get("name") or "N/A",
        record.get("email") or record.get("group_mail")
        or record.get("user_email") or "N/A",
        record.get("group_type") or record.get("auth_provider")
        or record.get("source") or "IdP",
    ]


@mcp.tool(description=(
    "Reads the tenant's directory of users and groups. principal='user' or 'group'. "
    "action=list returns the principals in the tenant; action=search needs name and matches "
    "on a substring; action=get needs user_id or group_id; action=match needs name and "
    "resolves one exact name against the identity provider, including an account the tenant "
    "has not seen yet; action=sync needs name and refreshes that account from the identity "
    "provider so it can be given a role; action=members needs group_id and lists the users in "
    "that group; action=memberships needs user_id and lists the groups that user belongs to. "
    "Pass tenant_id to read another tenant, which needs the Platform Admin role; without it "
    "the call reads your own tenant and needs a tenant Admin role. "
    "Users and groups cannot be created here — they come from the identity provider. "
    "Use manage_role_assignment to give one of them a role."
))
async def browse_directory(
    principal: str = Field(description="Principal kind: user or group"),
    action: str = Field(
        description="Action: list, search, match, get, sync, members, or memberships"),
    name: Optional[str] = Field(
        default=None, description="Username or group name (for search, match, and sync)"),
    user_id: Optional[int] = Field(
        default=None,
        description="User ID — the subject for get when principal='user', or the user whose "
                    "groups to list for action='memberships'"),
    group_id: Optional[int] = Field(
        default=None,
        description="Group ID — the subject for get when principal='group', or the group "
                    "whose users to list for action='members'"),
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant ID (UUID) to read another tenant; needs the Platform Admin role. "
                    "Omit to read your own tenant."),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
) -> str:
    """List, search, match or sync the users and groups in a tenant."""
    try:
        principal = (opt(principal) or "").strip().lower()
        action = (opt(action) or "").strip().lower()
        name = opt(name)
        user_id = opt(user_id)
        group_id = opt(group_id)
        tenant_id = opt(tenant_id)
        limit = opt(limit) or 20
        page = opt(page) or 1

        if principal not in _DIR_PRINCIPALS:
            return (
                f"Error: Invalid principal '{principal}'. "
                "HINT: Must be 'user' or 'group'."
            )
        if action not in _DIRECTORY_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_DIRECTORY_ACTIONS)}."
            )

        one, many, name_field, id_field = _DIR_PRINCIPALS[principal]
        principal_id = user_id if principal == "user" else group_id
        # /admin reads the caller's tenant; /platform/admin/tenant/<id> reads any.
        scoped = f"/api/v1/platform/admin/tenant/{tenant_id}" if tenant_id else "/api/v1/admin"
        shared = "/api/v1/platform/shared/admin"
        headers = [id_field, name_field, "email", "source"]
        paging = {"pageNumber": page, "limit": limit}

        if action in ("search", "match", "sync") and not name:
            return (
                f"Error: name is required for action='{action}'. "
                f"HINT: It is the {name_field}."
            )
        if action == "get" and principal_id is None:
            return (
                f"Error: {id_field} is required for action='get'. "
                f"HINT: Resolve a name to its id with action='match'."
            )
        if action == "members" and group_id is None:
            return "Error: group_id is required for action='members'."
        if action == "memberships" and user_id is None:
            return "Error: user_id is required for action='memberships'."

        if action == "list":
            async with YeeduClient() as client:
                data = await client.get(f"{scoped}/{many}", params=paging)
        elif action == "search":
            async with YeeduClient() as client:
                data = await client.get(
                    f"{scoped}/{many}/search",
                    params={**paging, name_field: name},
                )
        elif action == "get":
            # The platform spells the user path singular and the group path plural.
            endpoint = (
                f"{scoped}/user/{principal_id}" if principal == "user"
                else (f"{scoped}/group/{principal_id}" if tenant_id
                      else f"{scoped}/groups/{principal_id}")
            )
            async with YeeduClient() as client:
                data = await client.get(endpoint)
        elif action == "match":
            async with YeeduClient() as client:
                data = await client.get(f"{shared}/match/{one}/{name}")
        elif action == "sync":
            async with YeeduClient() as client:
                data = await client.get(f"{shared}/sync/{one}/{name}")
        elif action == "members":
            async with YeeduClient() as client:
                data = await client.get(
                    f"{shared}/users", params={**paging, "group_id": group_id})
        else:  # memberships
            async with YeeduClient() as client:
                data = await client.get(
                    f"{shared}/groups", params={**paging, "user_id": user_id})

        if isinstance(data, dict) and data.get("error_message"):
            return (
                f"Error: {data['error_message']}. "
                "HINT: Reading your own tenant needs a tenant Admin role; passing "
                "tenant_id needs Platform Admin."
            )

        if action in ("members", "memberships"):
            kind = "user" if action == "members" else "group"
            headers = [f"{kind}_id", "username" if kind == "user" else "groupname",
                       "email", "source"]
            records = _holders(
                data, "users" if kind == "user" else "groups", f"{kind}_id")
            rows = [_directory_row(r, kind) for r in records]
        else:
            records = [r for r in _rows(data) if isinstance(r, dict)]
            rows = [_directory_row(r, one) for r in records]

        if not rows:
            if action == "match":
                return (
                    f"No {one} named '{name}' in the identity provider. "
                    "HINT: Check the spelling; the name has to match exactly."
                )
            if action == "members":
                return f"Group {group_id} has no users."
            if action == "memberships":
                return f"User {user_id} belongs to no groups."
            return (
                f"No {many} found"
                + (f" matching '{name}'" if action == "search" else "")
                + (f" in tenant {tenant_id}" if tenant_id else " in your tenant")
                + "."
            )

        table = serialize_table(rows, headers=headers)
        if action == "sync":
            return f"Synced {one} '{name}' from the identity provider.\n\n{table}"
        return table + _page_note(data, page)

    except Exception as e:
        return (
            f"Error running action '{action}' on the {principal} directory: {str(e)}. "
            "HINT: Reading your own tenant needs a tenant Admin role, and passing tenant_id "
            "needs Platform Admin; call get_user_roles to check yours."
        )


# ---------------------------------------------------------------------------
# Role assignment
#
# A role is platform-wide and read-only (get_roles lists them). What is
# assignable is the pairing of an existing user or group with one of those
# roles inside a tenant, and that is what this tool manages.
#
# The URL shape differs per scope in ways that are not guessable, so the paths
# are spelled out rather than built from a pattern: the tenant-scoped assign
# and unassign calls put the tenant in a query parameter while every read puts
# it in the path.
# ---------------------------------------------------------------------------

_ROLE_ACTIONS = ("list", "get", "by_role", "assign", "unassign")


#: Where a role's id hides. The platform spells it both ways: `roles_id` in the
#: per-principal role reads, `role_id` in the tenant-wide ones.
_ROLE_ID_KEYS = ("roles_id", "role_id")
#: Where a role's display name hides, by the list that carried it.
_ROLE_NAME_KEYS = ("user_role", "group_role", "role_name", "role")


def _role_fields(role: dict) -> tuple:
    """Pull one role object's id and display name out, whatever it is keyed by."""
    role_id = next(
        (role[k] for k in _ROLE_ID_KEYS if role.get(k) is not None), None)
    name = next(
        (role[k] for k in _ROLE_NAME_KEYS
         if isinstance(role.get(k), str) and role[k].strip()), None)
    return role_id, name


def _role_rows(record: dict, kind: str, fallback_role: Any = None) -> list:
    """Expand one principal's record into a row per role it holds.

    A principal's roles arrive nested, one list per source: `user_roles` are
    granted to the account itself and `group_roles` are inherited from its
    groups, so one record can yield several rows and reading a flat `role_id`
    off it yields none. Inherited rows name the group they come from, because
    that is where such a role has to be removed.

    `fallback_role` covers the by_role listing, whose rows are bare principals
    under a role the caller already named.
    """
    principal_id = record.get(f"{kind}_id", "N/A")
    name = (record.get("username") or record.get("group_name")
            or record.get("display_name") or record.get("name") or "N/A")

    rows = []
    for key in ("user_roles", "group_roles", "roles"):
        held = record.get(key)
        if not isinstance(held, list):
            continue
        for role in held:
            if not isinstance(role, dict):
                continue
            role_id, role_name = _role_fields(role)
            via = ""
            if key == "group_roles" and kind == "user":
                group = role.get("group_name") or role.get("group_id")
                via = f" (via group {group})" if group else " (via a group)"
            rows.append([
                principal_id, name,
                role_id if role_id is not None else "N/A",
                (role_name or "N/A") + via,
            ])
    if rows:
        return rows

    # A flat record, or a principal that holds no role at all.
    role_id, role_name = _role_fields(record)
    if role_id is None and role_name is None and fallback_role is not None:
        role_id = fallback_role
    return [[
        principal_id, name,
        role_id if role_id is not None else "—",
        role_name or "—",
    ]]


@mcp.tool(description=(
    "Assigns and removes tenant roles for a user or group. principal='user' or 'group'. "
    "action=list returns every role assignment in the tenant; action=get needs user_id or "
    "group_id and returns that principal's roles; action=by_role needs role_id and returns "
    "who holds it. "
    "action=assign needs user_id or group_id plus role_id. "
    "action=unassign needs the same and requires confirm=True: the first call returns the "
    "assignment it would remove and does nothing, so ask the user before repeating the call "
    "with confirm=True. "
    "Pass tenant_id to act on another tenant, which needs the Platform Admin role; without it "
    "the call acts on your own tenant and needs a tenant Admin role. "
    "Roles themselves cannot be created or edited — call get_roles for the role_id values, and "
    "browse_directory to resolve a name to a user_id or group_id."
))
async def manage_role_assignment(
    principal: str = Field(description="Principal kind: user or group"),
    action: str = Field(
        description="Action: list, get, by_role, assign, or unassign"),
    user_id: Optional[int] = Field(
        default=None, description="User ID (for get, assign, unassign when principal='user')"),
    group_id: Optional[int] = Field(
        default=None, description="Group ID (for get, assign, unassign when principal='group')"),
    role_id: Optional[int] = Field(
        default=None,
        description="Role ID — required for assign, unassign and by_role. Call get_roles for the values."),
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant ID (UUID) to act on another tenant; needs the Platform Admin "
                    "role. Omit to act on your own tenant."),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='unassign'. Set it only after the user has confirmed this specific assignment.",
    ),
) -> str:
    """List, assign or remove a tenant role for a user or group."""
    try:
        principal = (opt(principal) or "").strip().lower()
        action = (opt(action) or "").strip().lower()
        user_id = opt(user_id)
        group_id = opt(group_id)
        role_id = opt(role_id)
        tenant_id = opt(tenant_id)
        limit = opt(limit) or 20
        page = opt(page) or 1

        if principal not in _DIR_PRINCIPALS:
            return (
                f"Error: Invalid principal '{principal}'. "
                "HINT: Must be 'user' or 'group'."
            )
        if action not in _ROLE_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_ROLE_ACTIONS)}."
            )

        one, many, name_field, id_field = _DIR_PRINCIPALS[principal]
        principal_id = user_id if principal == "user" else group_id
        scoped = f"/api/v1/platform/admin/tenant/{tenant_id}" if tenant_id else "/api/v1/admin"
        headers = [id_field, name_field, "role_id", "role"]
        paging = {"pageNumber": page, "limit": limit}

        if action in ("get", "assign", "unassign") and principal_id is None:
            return (
                f"Error: {id_field} is required for action='{action}'. "
                f"HINT: Resolve the name with browse_directory(principal='{principal}', "
                "action='match', name='<name>')."
            )
        if action in ("assign", "unassign", "by_role") and role_id is None:
            return (
                f"Error: role_id is required for action='{action}'. "
                "HINT: Call get_roles to see the roles this platform defines."
            )

        def _assignment_path() -> str:
            """The assign/unassign path, which carries the tenant as a query param."""
            if tenant_id:
                return (
                    f"/api/v1/platform/admin/tenant/{one}/{principal_id}/role/{role_id}")
            return f"/api/v1/admin/{one}/{principal_id}/role/{role_id}"

        async def _read_assignment(client) -> Any:
            """Read the principal's current roles, for display."""
            endpoint = (
                f"{scoped}/user/{principal_id}/roles" if principal == "user"
                else f"{scoped}/groups/{principal_id}/roles"
            )
            return await client.get(endpoint)

        if action == "list":
            async with YeeduClient() as client:
                data = await client.get(f"{scoped}/roles/{many}", params=paging)
        elif action == "by_role":
            async with YeeduClient() as client:
                data = await client.get(
                    f"{scoped}/roles/{many}/{role_id}", params=paging)
        elif action == "get":
            async with YeeduClient() as client:
                data = await _read_assignment(client)
        elif action == "assign":
            async with YeeduClient() as client:
                data = await client.post(
                    _assignment_path(),
                    params={"tenant_id": tenant_id} if tenant_id else None,
                )
            if isinstance(data, dict) and data.get("error_message"):
                return (
                    f"Error: {data['error_message']}. "
                    "HINT: Confirm the role_id with get_roles and the principal id with "
                    "browse_directory; a principal the tenant has never seen may need "
                    "browse_directory(action='sync') first."
                )
            return (
                f"Assigned role_id {role_id} to {one} {principal_id}"
                + (f" in tenant {tenant_id}" if tenant_id else " in your tenant") + "."
            )
        else:
            # unassign — refuse before opening a connection, so an unreachable
            # API cannot turn a refusal into an error the agent retries past.
            if not confirmed(confirm):
                record = None
                try:
                    async with YeeduClient() as client:
                        data = await _read_assignment(client)
                    rows = [row for r in _rows(data) if isinstance(r, dict)
                            for row in _role_rows(r, one)]
                    if rows:
                        record = serialize_table(rows, headers=headers)
                except Exception:
                    record = None
                return confirmation_required(
                    f"{one.capitalize()} role assignment",
                    f"{id_field}={principal_id}, role_id={role_id}",
                    "manage_role_assignment",
                    action="unassign",
                    record=record,
                    impact=(
                        f"The {one} loses every platform capability this role carries"
                        + (f" in tenant {tenant_id}" if tenant_id else " in your tenant")
                        + ". Workspace permissions are separate and unaffected, and a user "
                        "who also holds the role through a group keeps it."
                    ),
                    retry=(
                        f"principal='{principal}', {id_field}={principal_id}, "
                        f"role_id={role_id}"
                        + (f", tenant_id='{tenant_id}'" if tenant_id else "")
                    ),
                )

            async with YeeduClient() as client:
                await client.delete(
                    _assignment_path(),
                    params={"tenant_id": tenant_id} if tenant_id else None,
                )
            return (
                f"Removed role_id {role_id} from {one} {principal_id}"
                + (f" in tenant {tenant_id}" if tenant_id else " in your tenant") + "."
            )

        if isinstance(data, dict) and data.get("error_message"):
            return (
                f"Error: {data['error_message']}. "
                "HINT: Reading your own tenant needs a tenant Admin role; passing "
                "tenant_id needs Platform Admin."
            )

        # by_role answers with one object naming the role, holders nested
        # inside it; the other reads answer with the principals directly.
        records = _holders(data, many, id_field) if action == "by_role" else [
            r for r in _rows(data) if isinstance(r, dict)]

        rows = [row for r in records
                for row in _role_rows(r, one, fallback_role=role_id)]
        if not rows:
            if action == "get":
                return (
                    f"{one.capitalize()} {principal_id} holds no roles"
                    + (f" in tenant {tenant_id}" if tenant_id else " in your tenant") + "."
                )
            if action == "by_role":
                return f"No {many} hold role_id {role_id}."
            return (
                f"No role assignments for {many}"
                + (f" in tenant {tenant_id}" if tenant_id else " in your tenant") + "."
            )
        return serialize_table(rows, headers=headers) + _page_note(data, page)

    except Exception as e:
        return (
            f"Error running action '{action}' on {principal} role assignment: {str(e)}. "
            "HINT: Confirm the role_id with get_roles and the principal id with "
            "browse_directory; acting on your own tenant needs a tenant Admin role and "
            "passing tenant_id needs Platform Admin."
        )
