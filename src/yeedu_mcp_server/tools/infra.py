"""Cloud infrastructure tools for Yeedu MCP Server.

Covers the resource chain a cluster is built from. Nothing here can be created
without the layer below it already existing:

    lookups (read-only, platform-seeded)
      |-- credential config  --+
      |-- network config  -----+--> cloud env --+
      |-- boot disk image  ----+                +--> cluster instance
      |-- cluster config  ----------------------+         (tools/cluster.py)

Subnets and availability zones are NOT separate resources — they are fields on
a network configuration, so "create a subnet" means creating a network config.
"""

from typing import Any, Dict, List, Optional

from pydantic import Field
from pydantic.fields import FieldInfo

from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.handoff import secret_handoff

#: Appended to every tool description whose delete is gated.
_CONFIRM_NOTE = (
    "delete needs confirm=True: call it without confirm first to see the record and what breaks, relay that to the user, and only repeat with confirm=True once they agree."
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

#: Cloud provider ids are fixed platform data, not per-tenant.
CLOUD_PROVIDERS = {0: "gcp", 1: "aws", 2: "azure", 3: "onprem", 4: "oci"}

#: Lookup kind -> (endpoint template, whether it needs a cloud provider id in the path)
_LOOKUPS: Dict[str, tuple] = {
    "cloud_providers": ("/api/v1/lookup_cloud_providers", False),
    "availability_zones": (
        "/api/v1/lookup_cloud_providers/{provider}/machine/available/zones",
        True,
    ),
    "machine_types": (
        "/api/v1/lookup_cloud_providers/{provider}/machine/types",
        True,
    ),
    "disk_machine_types": ("/api/v1/lookup_disk_machine_type", False),
    "credential_types": ("/api/v1/lookup_credential_type", False),
    "linux_distros": ("/api/v1/lookup_linux_distro", False),
    "spark_infra_versions": ("/api/v1/lookup_spark_infra_version", False),
    "spark_compute_types": ("/api/v1/lookup_spark_compute_type", False),
}


def _rows(data: Any) -> List[dict]:
    """Normalise a Yeedu response into a list of dicts.

    Endpoints are inconsistent: some return a bare list, some wrap it in
    ``data``, and single-record endpoints return the object itself.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        if isinstance(inner, dict):
            return [inner]
        return [data]
    return []


def _api_error(data: Any) -> Optional[str]:
    """Return the platform's error text if the response carries one."""
    if isinstance(data, dict):
        for key in ("error_message", "error", "message"):
            val = data.get(key)
            if val and not data.get("data"):
                return str(val)
    return None


def _flatten(value: Any) -> str:
    """Render one cell. Nested objects collapse to their id or name."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("name", "id"):
            for k, v in value.items():
                if k.endswith(key) and not isinstance(v, (dict, list)):
                    return str(v)
        # No identifying key — this is free-form data such as cloud env labels,
        # which the user set and needs to read back, so render the pairs.
        pairs = " ".join(f"{k}={_flatten(v)}" for k, v in value.items())
        return pairs[:200] + "..." if len(pairs) > 200 else pairs
    if isinstance(value, list):
        return ";".join(_flatten(v) for v in value) if value else ""
    return str(value).replace(",", " ").replace("\n", " ")


def _csv(rows: List[dict], columns: Optional[List[str]] = None) -> str:
    """CSV-render rows. Without explicit columns, use the first row's keys.

    Deriving columns from the response keeps these tools working when the
    platform adds fields, at the cost of column order being server-defined.
    """
    if not rows:
        return "No results found."
    cols = columns or list(rows[0].keys())
    lines = [",".join(cols)]
    for row in rows:
        lines.append(",".join(_flatten(row.get(c)) for c in cols))
    return "\n".join(lines)


def _page_note(data: Any, page: int) -> str:
    """Trailing pagination line, when the response carries a result_set."""
    if not isinstance(data, dict):
        return ""
    rs = data.get("result_set") or {}
    if not rs:
        return ""
    return (
        f"\n\nPage {rs.get('current_page', page)} of {rs.get('total_pages', 1)}"
        f" | Total: {rs.get('total_objects', '?')}"
    )


def _provided(value: Any) -> bool:
    """Whether a value counts as supplied.

    Matches the CLI: None, an empty list and an empty dict are all "absent",
    so an empty network_tags neither satisfies a required rule nor trips a
    not-applicable one. A bare FieldInfo only reaches here when a tool is
    called directly from Python instead of through FastMCP, which fills
    defaults; treat it as absent so offline checks behave like a real call.
    """
    if value is None or isinstance(value, FieldInfo):
        return False
    if isinstance(value, (list, dict, str)) and len(value) == 0:
        return False
    return True


def _body(**fields: Any) -> Dict[str, Any]:
    """Drop absent fields, keeping explicit nulls.

    The platform clears a field when it receives the string "null" on an edit,
    so that value must survive into the body.
    """
    return {k: v for k, v in fields.items() if _provided(v)}


def _int_or_null(value: Any) -> Any:
    """Accept an int, or the literal "null" that clears a field on edit.

    These fields have to be typed loosely because the clear sentinel is a
    string while the value is an integer; this narrows it back down.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == "null":
            return "null"
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return text
    return value


def _as_list(value: Any) -> Optional[List[str]]:
    """Normalise a comma-separated string or a real list into list[str].

    MCP clients deserialize JSON-looking arguments before a tool sees them, so
    this has to accept both a list and a string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts or None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return parts or None
    return [str(value)]


def _as_labels(value: Any) -> Optional[Dict[str, str]]:
    """Normalise labels into a {key: value} object.

    Accepts a dict, a list of "k=v" strings, or one "k=v,k2=v2" string.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()} or None
    items = _as_list(value) or []
    out: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        out[key.strip()] = val.strip()
    return out or None


# ---------------------------------------------------------------------------
# Generic CRUD dispatch
#
# Every one of these six resources has the same wire shape: selectors are
# always query params and never body, POST carries a body with no query, and
# PUT carries both. That uniformity is what makes one helper viable.
# ---------------------------------------------------------------------------

_ACTIONS = ("create", "list", "get", "edit", "delete", "search")

_RESOURCES: Dict[str, Dict[str, Any]] = {
    "credential_config": {
        "singular": "/api/v1/credential_config",
        "plural": "/api/v1/credential_configs",
        "id_key": "credentials_conf_id",
        "name_key": "credentials_conf_name",
        "label": "Credential configuration",
        "tool": "manage_credential_config",
        "impact": (
            "any cloud environment built on this credential stops working, and the "
            "credential value cannot be restored through this server — recreating it "
            "means re-entering it in the UI or CLI"
        ),
    },
    "network_config": {
        "singular": "/api/v1/machine/network",
        "plural": "/api/v1/machine/networks",
        "id_key": "network_conf_id",
        "name_key": "network_conf_name",
        "label": "Network configuration",
        "tool": "manage_network_config",
        "impact": "any cloud environment using this network stops working",
    },
    "boot_disk_image": {
        "singular": "/api/v1/machine/boot_disk_image",
        "plural": "/api/v1/machine/boot_disk_images",
        "id_key": "boot_disk_image_id",
        "name_key": "boot_disk_image_name",
        "label": "Boot disk image configuration",
        "tool": "manage_boot_disk_image",
        "impact": "any cloud environment booting from this image stops working",
    },
    "volume_config": {
        "singular": "/api/v1/machine/volume",
        "plural": "/api/v1/machine/volumes",
        "id_key": "volume_conf_id",
        "name_key": "volume_conf_name",
        "label": "Volume configuration",
        "tool": "manage_volume_config",
        "impact": "any cluster configuration referencing this disk layout stops working",
    },
    "cloud_env": {
        "singular": "/api/v1/cluster/cloud/environment",
        "plural": "/api/v1/cluster/cloud/environments",
        "id_key": "cloud_env_id",
        "name_key": "cloud_env_name",
        "label": "Cloud environment",
        "tool": "manage_cloud_env",
        "impact": "clusters built on this environment can no longer be created or started",
    },
    "cluster_config": {
        "singular": "/api/v1/cluster/conf",
        "plural": "/api/v1/cluster/confs",
        "id_key": "cluster_conf_id",
        "name_key": "cluster_conf_name",
        "label": "Cluster configuration",
        "tool": "manage_cluster_config",
        "impact": "new cluster instances can no longer be created from this machine shape",
    },
    "mount": {
        "singular": "/api/v1/cluster/mount",
        "plural": "/api/v1/cluster/mounts",
        "id_key": "mount_id",
        "name_key": "mount_name",
        "label": "Mount",
        "tool": "manage_cluster_mount",
        "impact": (
            "clusters that mount this storage lose the mount point, and jobs reading "
            "or writing paths under it fail"
        ),
    },
}


async def _fetch_one(client: YeeduClient, key: str, params: Dict[str, Any]) -> Optional[dict]:
    """GET a single record, or None when it cannot be read."""
    res = _RESOURCES[key]
    data = await client.get(res["singular"], params=params)
    if _api_error(data):
        return None
    rows = _rows(data)
    return rows[0] if rows else None


async def _crud(
    key: str,
    action: str,
    *,
    selector_id: Optional[int] = None,
    selector_name: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    filters: Optional[Dict[str, Any]] = None,
    page: int = 1,
    limit: int = 20,
    confirm: bool = False,
) -> str:
    """Run one CRUD action against a configuration resource."""
    res = _RESOURCES[key]
    label, tool = res["label"], res["tool"]
    id_key, name_key = res["id_key"], res["name_key"]

    selector: Dict[str, Any] = {}
    if _provided(selector_id):
        selector[id_key] = selector_id
    elif _provided(selector_name):
        selector[name_key] = selector_name

    if action in ("get", "edit", "delete") and not selector:
        return (
            f"Error: action='{action}' needs an identifier. "
            f"Hint: Pass {id_key} or {name_key}; list them with "
            f"{tool}(action='list')."
        )

    if action == "search" and not selector_name:
        return (
            "Error: action='search' needs a name to search for. "
            f"Hint: Pass {name_key}, or use {tool}(action='list') to see everything."
        )

    if action in ("create", "edit") and not body:
        verb = "create" if action == "create" else "change"
        return (
            f"Error: No fields supplied to {verb}. "
            f"Hint: Check {tool}'s description for the fields this action needs."
        )

    if action == "delete" and not confirm:
        # Refuse before opening a connection, so an unreachable API can never
        # turn a refusal into an error the agent might then "retry" past. The
        # read-back is a best-effort extra: a delete by name, or by an id the
        # agent carried over from an earlier answer, is exactly where the wrong
        # record gets destroyed, so show the row whenever it can be fetched.
        record = None
        try:
            async with YeeduClient() as client:
                found = _rows(await client.get(res["singular"], params=selector))
            record = _csv(found) if found else None
        except Exception:
            record = None
        return confirmation_required(
            label, str(selector_id if _provided(selector_id) else selector_name), tool,
            record=record,
            impact=res.get("impact"),
            retry=f"{id_key}={selector_id}" if _provided(selector_id)
            else f"{name_key}='{selector_name}'",
        )

    async with YeeduClient() as client:
        if action == "create":
            data = await client.post(res["singular"], json=body)
        elif action == "edit":
            data = await client.put(res["singular"], params=selector, json=body)
        elif action == "delete":
            data = await client.delete(res["singular"], params=selector)
        elif action == "get":
            data = await client.get(res["singular"], params=selector)
        else:  # list / search
            params: Dict[str, Any] = {"pageNumber": page, "limit": limit}
            params.update({k: v for k, v in (filters or {}).items() if v is not None})
            endpoint = res["plural"]
            if action == "search":
                params[name_key] = selector_name
                endpoint = f"{res['plural']}/search"
            try:
                data = await client.get(endpoint, params=params)
            except Exception as exc:
                # A collection with nothing in it answers HTTP 404 rather than
                # an empty array. The listing path is fixed and correct, so a
                # 404 here means "none exist" — reporting it as an error gets
                # the caller retrying a call that already succeeded.
                if "404" not in str(exc):
                    raise
                return f"No {label.lower()}s found." + (
                    f" Nothing matches '{selector_name}'."
                    if action == "search" else ""
                )

    err = _api_error(data)
    if err:
        hints = {
            "create": (
                f"Read the field list in {tool}'s description and confirm every id "
                "it references exists — list_compute_lookups has the lookup ids."
            ),
            "edit": f"Confirm the record exists with {tool}(action='get').",
            "delete": (
                "The record may not exist, or something still references it. "
                f"Check with {tool}(action='get')."
            ),
            "get": f"Confirm the identifier with {tool}(action='list').",
            "list": "Verify the API connection.",
            "search": f"Try {tool}(action='list') to see the available names.",
        }
        return f"Error: {err}. Hint: {hints.get(action, 'Check the parameters and retry.')}"

    if action in ("list", "search"):
        rows = _rows(data)
        if not rows:
            return f"No {label.lower()}s found."
        return _csv(rows) + _page_note(data, page)

    if action == "get":
        rows = _rows(data)
        if not rows:
            return f"{label} not found."
        return _csv(rows)

    if action == "delete":
        target = selector_id if _provided(selector_id) else selector_name
        return f"{label} {target} deleted successfully."

    # create / edit — echo the record so the agent picks up the new id,
    # whatever the response happens to call it.
    rows = _rows(data)
    done = "created" if action == "create" else "updated"
    name = ((body or {}).get("name")
            or (selector_id if _provided(selector_id) else None)
            or (selector_name if _provided(selector_name) else None)
            or "")
    head = f"{label} {name} {done} successfully.".replace("  ", " ")
    return f"{head}\n{_csv(rows)}" if rows else head


# ---------------------------------------------------------------------------
# Provider-conditional field rules
#
# The platform rejects not-applicable fields server-side with little detail, so
# these matrices are validated here first. Each entry is
# (required, optional); anything else in ALL_FIELDS is not applicable.
# ---------------------------------------------------------------------------

_NETWORK_FIELDS = (
    "name", "description", "cloud_provider_id", "network_project_id",
    "network_name", "subnet", "availability_zone_id", "network_tags",
    "bridge", "model",
)

_NETWORK_RULES = {
    "cloud": {
        "create": (
            ("name", "cloud_provider_id", "network_project_id", "network_name",
             "subnet", "availability_zone_id"),
            ("description", "network_tags"),
        ),
        "edit": (
            (),
            ("name", "description", "network_project_id", "network_name",
             "subnet", "availability_zone_id", "network_tags"),
        ),
    },
    "onprem": {
        "create": (
            ("name", "cloud_provider_id", "bridge", "model"),
            ("description",),
        ),
        "edit": ((), ("name", "description", "bridge", "model")),
    },
    "oci": {
        "create": (
            ("name", "cloud_provider_id", "network_name", "subnet",
             "availability_zone_id"),
            ("description", "network_tags"),
        ),
        "edit": (
            (),
            ("name", "description", "network_name", "subnet",
             "availability_zone_id", "network_tags"),
        ),
    },
}

_CLOUD_ENV_FIELDS = (
    "name", "description", "cloud_provider_id", "availability_zone_id",
    "network_conf_id", "cloud_project", "credential_config_id",
    "boot_disk_image_id", "labels", "endpoint", "onprem_node_names",
)

_CLOUD_ENV_RULES = {
    "cloud": {
        "create": (
            ("name", "cloud_provider_id", "credential_config_id",
             "boot_disk_image_id", "network_conf_id", "availability_zone_id",
             "cloud_project"),
            ("description", "labels"),
        ),
        "edit": (
            (),
            ("name", "description", "availability_zone_id", "network_conf_id",
             "cloud_project", "credential_config_id", "boot_disk_image_id",
             "labels"),
        ),
    },
    "onprem": {
        "create": (
            ("name", "cloud_provider_id", "credential_config_id",
             "boot_disk_image_id", "network_conf_id", "endpoint",
             "onprem_node_names"),
            ("description", "labels"),
        ),
        "edit": (
            (),
            ("name", "description", "network_conf_id", "credential_config_id",
             "boot_disk_image_id", "labels", "endpoint", "onprem_node_names"),
        ),
    },
    "oci": {
        "create": (
            ("name", "cloud_provider_id", "credential_config_id",
             "boot_disk_image_id", "network_conf_id", "availability_zone_id"),
            ("description", "labels"),
        ),
        "edit": (
            (),
            ("name", "description", "availability_zone_id", "network_conf_id",
             "credential_config_id", "boot_disk_image_id", "labels"),
        ),
    },
}


def _rule_group(provider_id: int) -> Optional[str]:
    """Map a provider id onto its rule group. gcp/aws/azure share one."""
    if provider_id in (0, 1, 2):
        return "cloud"
    if provider_id == 3:
        return "onprem"
    if provider_id == 4:
        return "oci"
    return None


def _check_provider_fields(
    all_fields: tuple,
    rules: dict,
    provider_id: int,
    op: str,
    body: Dict[str, Any],
    tool: str,
) -> Optional[str]:
    """Validate a body against the provider matrix. Returns an error or None."""
    group = _rule_group(provider_id)
    if group is None:
        return (
            f"Error: Unknown cloud_provider_id {provider_id}. "
            "Hint: Call list_compute_lookups(kind='cloud_providers') for valid ids "
            "(0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci)."
        )

    required, optional = rules[group][op]
    allowed = set(required) | set(optional)
    provider = CLOUD_PROVIDERS.get(provider_id, str(provider_id))

    not_applicable = sorted(
        f for f in all_fields if f in body and f not in allowed and _provided(body[f])
    )
    if not_applicable:
        return (
            f"Error: {', '.join(not_applicable)} "
            f"{'is' if len(not_applicable) == 1 else 'are'} not applicable for "
            f"{provider}. Hint: Omit {'it' if len(not_applicable) == 1 else 'them'} "
            f"and retry. For {provider}, {op} accepts: {', '.join(sorted(allowed))}."
        )

    missing = sorted(f for f in required if not _provided(body.get(f)))
    if missing:
        return (
            f"Error: {provider} requires {', '.join(missing)} on {op}. "
            f"Hint: Supply {'it' if len(missing) == 1 else 'them'}; "
            f"{tool} describes each field, and list_compute_lookups resolves ids."
        )
    return None


async def _resolve_provider(key: str, selector: Dict[str, Any]) -> Optional[int]:
    """Read a record's cloud provider id, for validating an edit.

    There is no provider field on edit, so the provider has to come from the
    stored record. A network config with no availability zone is OnPrem.
    """
    async with YeeduClient() as client:
        row = await _fetch_one(client, key, selector)
    if not row:
        return None
    if key == "network_config":
        az = row.get("availability_zone")
        if not isinstance(az, dict):
            return 3  # no availability zone => OnPrem
        prov = az.get("cloud_provider")
        if isinstance(prov, dict):
            return prov.get("cloud_provider_id")
        return row.get("cloud_provider_id")
    prov = row.get("cloud_provider")
    if isinstance(prov, dict):
        return prov.get("cloud_provider_id")
    return row.get("cloud_provider_id")


def _bad_action(action: str, tool: str, extra: tuple = ()) -> str:
    allowed = ", ".join(_ACTIONS + extra)
    return f"Error: Invalid action '{action}'. Hint: Use one of {allowed} with {tool}."


# ---------------------------------------------------------------------------
# 1. Credential configuration
#
# Creating one requires the credential document as a request field, so this
# server hands that step to the user rather than taking the value as a tool
# parameter. See utils/handoff.py for the reasoning.
# ---------------------------------------------------------------------------

#: UI navigation for the credential screen, as the UI labels it. Settings is not
#: on the left rail — it is reachable only from the user button, top right.
_UI_CREDENTIAL_NAV = (
    "user button (top right) > Settings > ADMIN > Credentials > + Credential, "
    "then fill Name, Provider and Credential Type and press Create"
)
_UI_CREDENTIAL_PATH = "settings/create-credential-config"
_UI_CREDENTIAL_ROLE = "needs tenant Admin or Platform Admin — + Credential is disabled for other roles"


def _credential_create_instructions(
    name: Optional[str], credential_type_id: Optional[int]
) -> str:
    """Instructions for the user to create a credential configuration."""
    name_arg = name if _provided(name) else "<name>"
    type_arg = credential_type_id if _provided(credential_type_id) else "<credential_type_id>"
    cli = (
        "yeedu resource create-credential-conf "
        f"--name {name_arg} "
        f"--credential_type_id {type_arg} "
        "--base64_encoded_credentials <base64-encoded-credential-file>"
    )
    return secret_handoff(
        what="a credential configuration",
        cli_command=cli,
        ui_nav=_UI_CREDENTIAL_NAV,
        ui_path=_UI_CREDENTIAL_PATH,
        role_note=_UI_CREDENTIAL_ROLE,
        next_step=(
            "manage_credential_config(action='list') to get its credentials_conf_id, "
            "then manage_network_config, manage_boot_disk_image and manage_cloud_env "
            "in that order"
        ),
    )

@mcp.tool(
    description=(
        "Manages credential configurations — the cloud credentials every other "
        "infrastructure resource is built on. Create one first when starting from "
        "an empty tenant. "
        "action: create, list, get, edit, delete, search. "
        "This server never accepts the credential value itself, so action='create' "
        "does not create anything: it returns the CLI command and UI screen for the "
        "user to do it themselves, after which the result is usable here by id. "
        "Relay those instructions to the user verbatim and do not look for another "
        "way to supply the credential. "
        "Rotating a credential is the same — use the CLI or UI. "
        "edit here changes only name and description. "
        "get/edit/delete need credentials_conf_id or credentials_conf_name. "
        "delete is destructive and breaks any cloud environment using this "
        "credential. " + _CONFIRM_NOTE + " "
        "On edit, pass the string \"null\" to clear description."
    ),
)
async def manage_credential_config(
    action: str = Field(description="Operation: create, list, get, edit, delete, search"),
    name: Optional[str] = Field(default=None, description="Credential configuration name — on edit, the new name"),
    description: Optional[str] = Field(default=None, description="Free-text description; pass \"null\" on edit to clear"),
    credential_type_id: Optional[int] = Field(
        default=None,
        description=(
            "Credential type id from list_compute_lookups(kind='credential_types'). "
            "Used only to tailor the create instructions; not editable"
        ),
    ),
    credentials_conf_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    credentials_conf_name: Optional[str] = Field(default=None, description="Name for get/edit/delete/search"),
    cloud_provider: Optional[str] = Field(default=None, description="Filter list/search by provider: GCP, AWS, Azure, OnPrem, OCI"),
    credential_type: Optional[str] = Field(default=None, description="Filter list/search by credential type name"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a credential configuration."""
    try:
        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return _bad_action(action, "manage_credential_config")

        if action == "create":
            return _credential_create_instructions(name, credential_type_id)

        body: Dict[str, Any] = {}
        if action == "edit":
            body = _body(name=name, description=description)
            if _provided(credential_type_id):
                return (
                    "Error: credential_type_id cannot be changed after create. "
                    "Hint: Omit it. To replace the credential value itself, use "
                    "manage_credential_config(action='create') for the instructions."
                )

        return await _crud(
            "credential_config", action,
            selector_id=credentials_conf_id,
            selector_name=credentials_conf_name,
            body=body,
            filters={"cloud_provider": cloud_provider, "credential_type": credential_type},
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing credential configuration: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


# ---------------------------------------------------------------------------
# 2. Network configuration (this is where subnets and availability zones live)
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Manages network configurations. There is no separate subnet resource — a "
        "subnet and an availability zone are FIELDS of a network configuration, so "
        "creating one here is how a subnet is defined. "
        "action: create, list, get, edit, delete, search. "
        "Required fields depend on the provider: GCP/AWS/Azure need "
        "network_project_id, network_name, subnet and availability_zone_id; OCI "
        "needs network_name, subnet and availability_zone_id (no project); OnPrem "
        "needs bridge and model instead and accepts none of the cloud fields. "
        "Supplying a field the provider does not accept is rejected before the "
        "request is sent. cloud_provider_id is required on create and cannot be "
        "changed afterwards. "
        "get/edit/delete need network_conf_id or network_conf_name. "
        "delete is destructive and breaks any cloud environment using it. " + _CONFIRM_NOTE
    ),
)
async def manage_network_config(
    action: str = Field(description="Operation: create, list, get, edit, delete, search"),
    name: Optional[str] = Field(default=None, description="Network configuration name (required on create)"),
    description: Optional[str] = Field(default=None, description="Free-text description; pass \"null\" on edit to clear"),
    cloud_provider_id: Optional[int] = Field(
        default=None,
        description="0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci. Required on create, not editable",
    ),
    network_project_id: Optional[str] = Field(default=None, description="Cloud project holding the network (GCP/AWS/Azure only)"),
    network_name: Optional[str] = Field(default=None, description="Existing cloud network/VPC name (not OnPrem)"),
    subnet: Optional[str] = Field(default=None, description="Subnet name or CIDR within the network (not OnPrem)"),
    availability_zone_id: Optional[int] = Field(
        default=None,
        description="Zone id from list_compute_lookups(kind='availability_zones', cloud_provider_id=N). Not OnPrem",
    ),
    network_tags: Optional[Any] = Field(default=None, description="Network tags as a list or comma-separated string (not OnPrem)"),
    bridge: Optional[str] = Field(default=None, description="OnPrem bridge name, e.g. vmbr0 (OnPrem only)"),
    model: Optional[str] = Field(default=None, description="OnPrem NIC model, e.g. virtio (OnPrem only)"),
    network_conf_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    network_conf_name: Optional[str] = Field(default=None, description="Name for get/edit/delete/search"),
    cloud_provider: Optional[str] = Field(default=None, description="Filter list/search by provider: GCP, AWS, Azure, OnPrem, OCI"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a network configuration."""
    try:
        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return _bad_action(action, "manage_network_config")

        body: Dict[str, Any] = {}
        if action in ("create", "edit"):
            body = _body(
                name=name,
                description=description,
                network_project_id=network_project_id,
                network_name=network_name,
                subnet=subnet,
                availability_zone_id=availability_zone_id,
                network_tags=_as_list(network_tags),
                bridge=bridge,
                model=model,
            )
            if action == "create":
                if not _provided(cloud_provider_id):
                    return (
                        "Error: create needs cloud_provider_id. Hint: Call "
                        "list_compute_lookups(kind='cloud_providers') "
                        "(0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci)."
                    )
                body["cloud_provider_id"] = cloud_provider_id
                provider_id: Optional[int] = cloud_provider_id
            else:
                if _provided(cloud_provider_id):
                    return (
                        "Error: cloud_provider_id cannot be changed after create. "
                        "Hint: Omit it, or delete and recreate the network configuration."
                    )
                selector = (
                    {"network_conf_id": network_conf_id} if network_conf_id is not None
                    else {"network_conf_name": network_conf_name} if network_conf_name
                    else {}
                )
                provider_id = await _resolve_provider("network_config", selector) if selector else None

            if provider_id is not None:
                err = _check_provider_fields(
                    _NETWORK_FIELDS, _NETWORK_RULES, provider_id, action, body,
                    "manage_network_config",
                )
                if err:
                    return err

        return await _crud(
            "network_config", action,
            selector_id=network_conf_id,
            selector_name=network_conf_name,
            body=body,
            filters={"cloud_provider": cloud_provider},
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing network configuration: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


# ---------------------------------------------------------------------------
# 3. Boot disk image configuration
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Manages boot disk image configurations — the OS image cluster nodes boot from. "
        "action: create, list, get, edit, delete, search. "
        "create needs name, cloud_provider_id, linux_distro_id (from "
        "list_compute_lookups(kind='linux_distros')), boot_disk_image (the provider's "
        "image name or URI) and architecture_type_id. "
        "architecture_type_id has no lookup endpoint: it is 0 for x86_64 and 1 for "
        "aarch64, and list reports that same id in its architecture_type column. "
        "cloud_provider_id and architecture_type_id cannot be changed after create. "
        "get/edit/delete need boot_disk_image_id or boot_disk_image_name. "
        "delete is destructive and breaks any cloud environment using it. " + _CONFIRM_NOTE
    ),
)
async def manage_boot_disk_image(
    action: str = Field(description="Operation: create, list, get, edit, delete, search"),
    name: Optional[str] = Field(default=None, description="Boot disk image configuration name (required on create)"),
    description: Optional[str] = Field(default=None, description="Free-text description; pass \"null\" on edit to clear"),
    cloud_provider_id: Optional[int] = Field(
        default=None, description="0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci. Required on create, not editable"
    ),
    linux_distro_id: Optional[int] = Field(
        default=None, description="Distro id from list_compute_lookups(kind='linux_distros'). Required on create"
    ),
    boot_disk_image: Optional[str] = Field(
        default=None, description="Provider image name or URI to boot from. Required on create"
    ),
    architecture_type_id: Optional[int] = Field(
        default=None,
        description=(
            "CPU architecture id: 0 for x86_64, 1 for aarch64. Required on create, "
            "not editable. No lookup endpoint exists; list reports it as architecture_type"
        ),
    ),
    boot_disk_image_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    boot_disk_image_name: Optional[str] = Field(default=None, description="Name for get/edit/delete/search"),
    cloud_provider: Optional[str] = Field(default=None, description="Filter list/search by provider: GCP, AWS, Azure, OnPrem, OCI"),
    architecture_type: Optional[str] = Field(default=None, description="Filter list by architecture: x86_64 or aarch64"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a boot disk image configuration."""
    try:
        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return _bad_action(action, "manage_boot_disk_image")

        body: Dict[str, Any] = {}
        if action in ("create", "edit"):
            body = _body(
                name=name,
                description=description,
                linux_distro_id=linux_distro_id,
                boot_disk_image=boot_disk_image,
            )
            if action == "create":
                missing = [
                    label for label, val in (
                        ("name", name),
                        ("cloud_provider_id", cloud_provider_id),
                        ("linux_distro_id", linux_distro_id),
                        ("boot_disk_image", boot_disk_image),
                        ("architecture_type_id", architecture_type_id),
                    ) if not _provided(val)
                ]
                if missing:
                    return (
                        f"Error: create needs {', '.join(missing)}. Hint: Get "
                        "linux_distro_id from list_compute_lookups(kind='linux_distros') "
                        "and copy architecture_type_id from an existing record via "
                        "manage_boot_disk_image(action='list')."
                    )
                body["cloud_provider_id"] = cloud_provider_id
                body["architecture_type_id"] = architecture_type_id
            else:
                blocked = [
                    label for label, val in (
                        ("cloud_provider_id", cloud_provider_id),
                        ("architecture_type_id", architecture_type_id),
                    ) if _provided(val)
                ]
                if blocked:
                    return (
                        f"Error: {', '.join(blocked)} cannot be changed after create. "
                        "Hint: Omit them, or delete and recreate the configuration."
                    )

        return await _crud(
            "boot_disk_image", action,
            selector_id=boot_disk_image_id,
            selector_name=boot_disk_image_name,
            body=body,
            filters={"cloud_provider": cloud_provider, "architecture_type": architecture_type},
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing boot disk image configuration: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


# ---------------------------------------------------------------------------
# 4. Volume configuration
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Manages standalone volume configurations (disk layout templates). "
        "action: create, list, get, edit, delete, search. "
        "create needs name, encrypted, disk_type_id (from "
        "list_compute_lookups(kind='disk_machine_types')), machine_volume_num and "
        "machine_volume_strip_num. "
        "Note that manage_cluster_config does NOT reference a volume configuration by "
        "id — a cluster configuration carries its own inline disk fields and the "
        "platform derives a volume record from them. Use this tool to manage volume "
        "configurations directly, not as a prerequisite for creating a cluster. "
        "get/edit/delete need volume_conf_id or volume_conf_name. "
        "delete is destructive. " + _CONFIRM_NOTE
    ),
)
async def manage_volume_config(
    action: str = Field(description="Operation: create, list, get, edit, delete, search"),
    name: Optional[str] = Field(default=None, description="Volume configuration name (required on create)"),
    description: Optional[str] = Field(default=None, description="Free-text description; pass \"null\" on edit to clear"),
    encrypted: Optional[bool] = Field(default=None, description="Whether the volume is encrypted. Required on create"),
    size: Optional[int] = Field(default=None, description="Disk size in GB"),
    disk_type_id: Optional[int] = Field(
        default=None,
        description="Disk type id from list_compute_lookups(kind='disk_machine_types'). Required on create",
    ),
    machine_volume_num: Optional[int] = Field(default=None, description="Number of volumes per machine. Required on create"),
    machine_volume_strip_num: Optional[int] = Field(default=None, description="Number of volumes to stripe together. Required on create"),
    disk_iops: Optional[Any] = Field(
        default=None, description="Provisioned IOPS as an integer; pass the string \"null\" on edit to clear"
    ),
    disk_throughput_mb: Optional[Any] = Field(
        default=None,
        description="Provisioned throughput in MB/s as an integer; pass the string \"null\" on edit to clear",
    ),
    volume_conf_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    volume_conf_name: Optional[str] = Field(default=None, description="Name for get/edit/delete/search"),
    cloud_provider: Optional[str] = Field(default=None, description="Filter list/search by provider: GCP, AWS, Azure, OnPrem, OCI"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a volume configuration."""
    try:
        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return _bad_action(action, "manage_volume_config")

        body: Dict[str, Any] = {}
        if action in ("create", "edit"):
            # disk_throughput_MB keeps the platform's exact casing on the wire.
            body = _body(
                name=name,
                description=description,
                encrypted=encrypted,
                size=size,
                disk_type_id=disk_type_id,
                machine_volume_num=machine_volume_num,
                machine_volume_strip_num=machine_volume_strip_num,
                disk_iops=_int_or_null(disk_iops),
            )
            throughput = _int_or_null(disk_throughput_mb)
            if _provided(throughput):
                body["disk_throughput_MB"] = throughput
            if action == "create":
                missing = [
                    label for label, val in (
                        ("name", name),
                        ("encrypted", encrypted),
                        ("disk_type_id", disk_type_id),
                        ("machine_volume_num", machine_volume_num),
                        ("machine_volume_strip_num", machine_volume_strip_num),
                    ) if val is None or (isinstance(val, str) and not val)
                ]
                if missing:
                    return (
                        f"Error: create needs {', '.join(missing)}. Hint: Get "
                        "disk_type_id from list_compute_lookups(kind='disk_machine_types')."
                    )
                body["encrypted"] = bool(encrypted)

        return await _crud(
            "volume_config", action,
            selector_id=volume_conf_id,
            selector_name=volume_conf_name,
            body=body,
            filters={"cloud_provider": cloud_provider},
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing volume configuration: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


# ---------------------------------------------------------------------------
# 5. Cloud environment — joins credential, network and boot disk image
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Manages cloud environments, which bind a credential configuration, a network "
        "configuration and a boot disk image into the environment a cluster runs in. "
        "All three must exist first. "
        "action: create, list, get, edit, delete, search, list_onprem_nodes. "
        "create always needs name, cloud_provider_id, credential_config_id, "
        "network_conf_id and boot_disk_image_id. GCP/AWS/Azure additionally need "
        "availability_zone_id and cloud_project; OCI needs availability_zone_id only; "
        "OnPrem needs endpoint and onprem_node_names instead and accepts neither "
        "availability_zone_id nor cloud_project. Fields a provider does not accept are "
        "rejected before the request is sent. "
        "list_onprem_nodes discovers the nodes available on an OnPrem endpoint and "
        "needs endpoint and credential_config_id. "
        "cloud_provider_id cannot be changed after create. "
        "delete is destructive and breaks clusters built on it. " + _CONFIRM_NOTE
    ),
)
async def manage_cloud_env(
    action: str = Field(
        description="Operation: create, list, get, edit, delete, search, list_onprem_nodes"
    ),
    name: Optional[str] = Field(default=None, description="Cloud environment name (required on create)"),
    description: Optional[str] = Field(default=None, description="Free-text description; pass \"null\" on edit to clear"),
    cloud_provider_id: Optional[int] = Field(
        default=None, description="0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci. Required on create, not editable"
    ),
    credential_config_id: Optional[int] = Field(
        default=None,
        description="Credential configuration id from manage_credential_config(action='list'). Required on create",
    ),
    network_conf_id: Optional[int] = Field(
        default=None,
        description="Network configuration id from manage_network_config(action='list'). Required on create",
    ),
    boot_disk_image_id: Optional[int] = Field(
        default=None,
        description="Boot disk image id from manage_boot_disk_image(action='list'). Required on create",
    ),
    availability_zone_id: Optional[int] = Field(
        default=None,
        description="Zone id from list_compute_lookups(kind='availability_zones'). Cloud and OCI only, never OnPrem",
    ),
    cloud_project: Optional[str] = Field(default=None, description="Cloud project the environment runs in. GCP/AWS/Azure only"),
    labels: Optional[Any] = Field(
        default=None, description="Labels as a {key: value} object, a list of \"key=value\" strings, or \"k=v,k2=v2\""
    ),
    endpoint: Optional[str] = Field(
        default=None, description="OnPrem management endpoint. OnPrem only; also required by list_onprem_nodes"
    ),
    onprem_node_names: Optional[Any] = Field(
        default=None, description="OnPrem node names as a list or comma-separated string. OnPrem only"
    ),
    cloud_env_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    cloud_env_name: Optional[str] = Field(default=None, description="Name for get/edit/delete/search"),
    cloud_provider: Optional[str] = Field(default=None, description="Filter list/search by provider: GCP, AWS, Azure, OnPrem, OCI"),
    architecture_type: Optional[str] = Field(default=None, description="Filter list/search by architecture: x86_64 or aarch64"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a cloud environment."""
    try:
        action = (action or "").strip().lower()
        if action not in _ACTIONS + ("list_onprem_nodes",):
            return _bad_action(action, "manage_cloud_env", ("list_onprem_nodes",))

        if action == "list_onprem_nodes":
            if not _provided(endpoint) or not _provided(credential_config_id):
                return (
                    "Error: list_onprem_nodes needs endpoint and credential_config_id. "
                    "Hint: Create the credential configuration first with "
                    "manage_credential_config(action='create')."
                )
            async with YeeduClient() as client:
                data = await client.get(
                    "/api/v1/cluster/cloud/environment/onprem-nodes",
                    params={"endpoint": endpoint, "credential_config_id": credential_config_id},
                )
            err = _api_error(data)
            if err:
                return (
                    f"Error: {err}. Hint: Confirm the endpoint is reachable and that "
                    "credential_config_id is an OnPrem credential."
                )
            rows = _rows(data)
            if not rows:
                return "No OnPrem nodes found at that endpoint."
            return _csv(rows)

        body: Dict[str, Any] = {}
        if action in ("create", "edit"):
            body = _body(
                name=name,
                description=description,
                credential_config_id=credential_config_id,
                network_conf_id=network_conf_id,
                boot_disk_image_id=boot_disk_image_id,
                availability_zone_id=availability_zone_id,
                cloud_project=cloud_project,
                labels=_as_labels(labels),
                endpoint=endpoint,
                onprem_node_names=_as_list(onprem_node_names),
            )
            if action == "create":
                if not _provided(cloud_provider_id):
                    return (
                        "Error: create needs cloud_provider_id. Hint: Call "
                        "list_compute_lookups(kind='cloud_providers') "
                        "(0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci)."
                    )
                body["cloud_provider_id"] = cloud_provider_id
                provider_id: Optional[int] = cloud_provider_id
            else:
                if _provided(cloud_provider_id):
                    return (
                        "Error: cloud_provider_id cannot be changed after create. "
                        "Hint: Omit it, or delete and recreate the cloud environment."
                    )
                selector = (
                    {"cloud_env_id": cloud_env_id} if cloud_env_id is not None
                    else {"cloud_env_name": cloud_env_name} if cloud_env_name
                    else {}
                )
                provider_id = await _resolve_provider("cloud_env", selector) if selector else None

            if provider_id is not None:
                err = _check_provider_fields(
                    _CLOUD_ENV_FIELDS, _CLOUD_ENV_RULES, provider_id, action, body,
                    "manage_cloud_env",
                )
                if err:
                    return err

        return await _crud(
            "cloud_env", action,
            selector_id=cloud_env_id,
            selector_name=cloud_env_name,
            body=body,
            filters={"cloud_provider": cloud_provider, "architecture_type": architecture_type},
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing cloud environment: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


# ---------------------------------------------------------------------------
# 6. Cluster configuration — machine shape for a cluster instance
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Manages cluster configurations — the machine shape and disk layout a cluster "
        "instance is created from. Pair the resulting cluster_conf_id with a "
        "cloud_env_id in create_cluster. "
        "action: create, list, get, edit, delete, search. "
        "create needs only name and machine_type_id (from "
        "list_compute_lookups(kind='machine_types', cloud_provider_id=N)); the disk "
        "fields are optional. "
        "Disks are described inline here — disk_type_id, size, number_of_disks, "
        "disk_iops, disk_throughput_mb — and the platform derives a volume record from "
        "them, which is why a response may show a nested machine_volume_conf that was "
        "never sent. Do not pass a volume_conf_id unless this platform is version "
        "2.10.x, where the older contract referenced one. "
        "get/edit/delete need cluster_conf_id or cluster_conf_name. "
        "delete is destructive. " + _CONFIRM_NOTE
    ),
)
async def manage_cluster_config(
    action: str = Field(description="Operation: create, list, get, edit, delete, search"),
    name: Optional[str] = Field(default=None, description="Cluster configuration name (required on create)"),
    description: Optional[str] = Field(default=None, description="Free-text description; pass \"null\" on edit to clear"),
    machine_type_id: Optional[int] = Field(
        default=None,
        description=(
            "Machine type id from list_compute_lookups(kind='machine_types', "
            "cloud_provider_id=N). Required on create"
        ),
    ),
    disk_type_id: Optional[Any] = Field(
        default=None,
        description=(
            "Disk type id from list_compute_lookups(kind='disk_machine_types'); "
            "pass the string \"null\" on edit to clear"
        ),
    ),
    size: Optional[int] = Field(default=None, description="Disk size in GB"),
    number_of_disks: Optional[int] = Field(default=None, description="Number of disks per node"),
    disk_iops: Optional[int] = Field(default=None, description="Provisioned IOPS"),
    disk_throughput_mb: Optional[int] = Field(default=None, description="Provisioned throughput in MB/s"),
    volume_conf_id: Optional[int] = Field(
        default=None,
        description=(
            "Only for Yeedu 2.10.x, which referenced a volume configuration by id. "
            "Omit on current versions, which take the inline disk fields instead"
        ),
    ),
    cluster_conf_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    cluster_conf_name: Optional[str] = Field(default=None, description="Name for get/edit/delete/search"),
    cloud_provider: Optional[str] = Field(default=None, description="Filter list/search by provider: GCP, AWS, Azure, OnPrem, OCI"),
    compute_type: Optional[str] = Field(
        default=None,
        description=(
            "Filter list/search by compute type: compute_optimized, memory_optimized, "
            "general_purpose, gpu_accelerated, custom_compute, storage_optimized"
        ),
    ),
    architecture_type: Optional[str] = Field(default=None, description="Filter list/search by architecture: x86_64 or aarch64"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a cluster configuration."""
    try:
        action = (action or "").strip().lower()
        if action not in _ACTIONS:
            return _bad_action(action, "manage_cluster_config")

        body: Dict[str, Any] = {}
        if action in ("create", "edit"):
            body = _body(
                name=name,
                description=description,
                machine_type_id=machine_type_id,
                disk_type_id=_int_or_null(disk_type_id),
                size=size,
                number_of_disks=number_of_disks,
                disk_iops=disk_iops,
                volume_conf_id=volume_conf_id,
            )
            if _provided(disk_throughput_mb):
                body["disk_throughput_MB"] = disk_throughput_mb
            if action == "create" and (not _provided(name) or not _provided(machine_type_id)):
                return (
                    "Error: create needs name and machine_type_id. Hint: Get "
                    "machine_type_id from list_compute_lookups(kind='machine_types', "
                    "cloud_provider_id=N)."
                )

        return await _crud(
            "cluster_config", action,
            selector_id=cluster_conf_id,
            selector_name=cluster_conf_name,
            body=body,
            filters={
                "cloud_provider": cloud_provider,
                "compute_type": compute_type,
                "architecture_type": architecture_type,
            },
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing cluster configuration: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


# ---------------------------------------------------------------------------
# Compute lookups
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Reads platform-seeded compute reference data needed to create cloud "
        "infrastructure. kind must be one of: cloud_providers, availability_zones, "
        "machine_types, disk_machine_types, credential_types, linux_distros, "
        "spark_infra_versions, spark_compute_types. "
        "availability_zones and machine_types require cloud_provider_id "
        "(0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci). "
        "This data is read-only — it cannot be created or edited. "
        "Call this before manage_credential_config, manage_network_config, "
        "manage_boot_disk_image or manage_cluster_config to get valid ids."
    ),
    annotations={"readOnlyHint": True},
)
async def list_compute_lookups(
    kind: str = Field(
        description=(
            "Which lookup table to read: cloud_providers, availability_zones, "
            "machine_types, disk_machine_types, credential_types, linux_distros, "
            "spark_infra_versions, spark_compute_types"
        )
    ),
    cloud_provider_id: Optional[int] = Field(
        default=None,
        description=(
            "Cloud provider id — required for availability_zones and machine_types, "
            "optional filter for credential_types. 0=gcp, 1=aws, 2=azure, 3=onprem, 4=oci"
        ),
    ),
) -> str:
    """Read compute lookup tables (providers, zones, machine types, versions)."""
    try:
        kind = (kind or "").strip().lower()
        if kind not in _LOOKUPS:
            return (
                f"Error: Invalid kind '{kind}'. "
                f"Hint: Use one of {', '.join(sorted(_LOOKUPS))}."
            )

        endpoint, needs_provider = _LOOKUPS[kind]

        if needs_provider:
            if cloud_provider_id is None:
                return (
                    f"Error: kind '{kind}' requires cloud_provider_id. "
                    "Hint: Call list_compute_lookups(kind='cloud_providers') to get "
                    "provider ids, then retry with cloud_provider_id set."
                )
            if kind == "availability_zones" and cloud_provider_id == 3:
                return (
                    "OnPrem has no availability zones — a zone is a cloud-provider "
                    "concept. OnPrem network configurations take bridge and model "
                    "instead of subnet and availability_zone_id, and an OnPrem cloud "
                    "environment takes endpoint and onprem_node_names. "
                    "Hint: Call manage_cloud_env(action='list_onprem_nodes') to "
                    "discover the nodes on an OnPrem endpoint."
                )
            endpoint = endpoint.format(provider=cloud_provider_id)

        params: Dict[str, Any] = {}
        if kind == "credential_types" and cloud_provider_id is not None:
            params["cloud_provider"] = CLOUD_PROVIDERS.get(
                cloud_provider_id, cloud_provider_id
            )

        async with YeeduClient() as client:
            data = await client.get(endpoint, params=params or None)

        err = _api_error(data)
        if err:
            return (
                f"Error: {err}. Hint: Confirm cloud_provider_id is valid via "
                "list_compute_lookups(kind='cloud_providers')."
            )

        rows = _rows(data)
        if not rows:
            return f"No {kind} found."
        return _csv(rows)

    except Exception as exc:
        if "404" in str(exc) and kind in ("availability_zones", "machine_types"):
            return (
                f"Error reading {kind} lookup: {exc}. "
                f"Hint: The platform seeds no {kind} for cloud provider "
                f"{cloud_provider_id}, which is a property of that provider, not a "
                "connection problem. Pick a different cloud_provider_id from "
                "list_compute_lookups(kind='cloud_providers')."
            )
        return (
            f"Error reading {kind} lookup: {exc}. "
            "Hint: Verify the API connection, then retry with "
            "kind='cloud_providers' to confirm lookups are reachable."
        )


# ---------------------------------------------------------------------------
# Cluster <-> workspace binding
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Associates or dissociates a cluster with a workspace, which is what makes "
        "a cluster selectable when creating jobs and notebooks in that workspace. "
        "action must be 'associate' or 'dissociate'. Both require cluster_id (a cluster "
        "INSTANCE id, not a cluster_conf_id) and workspace_id. "
        "Dissociating needs confirm=True, and is disruptive: jobs and notebooks in "
        "that workspace that target "
        "this cluster can no longer run. Confirm with the user before dissociating. "
        "To see current bindings use get_cluster_workspaces or get_workspace_clusters."
    ),
)
async def manage_cluster_workspace_access(
    action: str = Field(description="Operation: associate or dissociate"),
    cluster_id: int = Field(
        description="Cluster INSTANCE id (from list_clusters), not a cluster_conf_id"
    ),
    workspace_id: int = Field(description="Workspace id (from list_workspaces)"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for dissociate. Leave it out first to see what would break, "
            "then repeat with confirm=True once the user agrees"
        ),
    ),
) -> str:
    """Bind or unbind a cluster and a workspace."""
    try:
        action = (action or "").strip().lower()
        if action not in ("associate", "dissociate"):
            return (
                f"Error: Invalid action '{action}'. "
                "Hint: Use action='associate' or action='dissociate'."
            )

        if action == "dissociate" and not confirmed(confirm):
            return confirmation_required(
                "cluster-workspace binding",
                f"cluster {cluster_id} in workspace {workspace_id}",
                "manage_cluster_workspace_access",
                action="dissociate",
                impact=(
                    "jobs and notebooks in that workspace that target this cluster "
                    "can no longer run"
                ),
                retry=f"cluster_id={cluster_id}, workspace_id={workspace_id}",
            )

        endpoint = f"/api/v1/ac/workspace/{workspace_id}/cluster/{cluster_id}"

        async with YeeduClient() as client:
            if action == "associate":
                data = await client.post(endpoint)
            else:
                data = await client.delete(endpoint)

        err = _api_error(data)
        if err:
            hint = (
                "Confirm cluster_id is a cluster instance id from list_clusters and "
                "workspace_id is from list_workspaces. If the binding already exists, "
                "check it with get_workspace_clusters."
            )
            return f"Error: {err}. Hint: {hint}"

        verb = "associated with" if action == "associate" else "dissociated from"
        return (
            f"Cluster {cluster_id} {verb} workspace {workspace_id} successfully."
        )

    except Exception as exc:
        return (
            f"Error during {action}: {exc}. "
            "Hint: Verify both ids exist via list_clusters and list_workspaces."
        )


# ---------------------------------------------------------------------------
# Mounts — object storage attached into a cluster's filesystem
#
# A mount references a credential configuration by id, so nothing secret
# passes through here. Attaching a mount to a cluster is part of the cluster
# configuration; this tool manages the mount definitions and reads back which
# clusters use them.
# ---------------------------------------------------------------------------

_MOUNT_ACTIONS = ("create", "list", "get", "edit", "delete", "clusters")


@mcp.tool(
    description=(
        "Manages mounts — object storage (S3, ADLS, GCS, custom S3a) attached into a "
        "cluster's filesystem at a mount point. A mount references a credential "
        "configuration by id, so no key material passes through this tool. "
        "action: create, list, get, edit, delete, clusters. "
        "create needs name, storage_type_id, source, mount_point and "
        "credentials_config_id. list accepts mount_name as a substring filter, "
        "storage_types, and exclude_cluster_id to show only mounts a cluster does not "
        "already use. clusters lists which clusters use which mounts and accepts "
        "cluster_id or mount_id. "
        "delete requires confirm=True: the first call reads the mount back and does "
        "nothing, so ask the user before repeating the call with confirm=True."
    )
)
async def manage_cluster_mount(
    action: str = Field(description="Operation: create, list, get, edit, delete, clusters"),
    name: Optional[str] = Field(default=None, description="Mount name (required on create)"),
    storage_type_id: Optional[int] = Field(
        default=None,
        description="Storage type id for the backing store. Required on create",
    ),
    source: Optional[str] = Field(
        default=None,
        description="Storage path to mount, e.g. an S3, ADLS, GCS or custom S3a URI. Required on create",
    ),
    mount_point: Optional[str] = Field(
        default=None,
        description="Path inside the cluster where the storage is mounted; must be unique. Required on create",
    ),
    credentials_config_id: Optional[int] = Field(
        default=None,
        description="Credential configuration id from manage_credential_config(action='list'). Required on create",
    ),
    extra_configs: Optional[Any] = Field(
        default=None,
        description="Extra driver configuration as an object, or as \"key=value\" strings",
    ),
    is_read_only: Optional[bool] = Field(
        default=None, description="Mount read-only. Defaults to false on create"),
    mount_id: Optional[int] = Field(default=None, description="Identifier for get/edit/delete"),
    mount_name: Optional[str] = Field(
        default=None,
        description="Name for get/edit/delete, or a substring filter on list",
    ),
    storage_types: Optional[Any] = Field(
        default=None, description="Filter list by storage type name(s)"),
    exclude_cluster_id: Optional[int] = Field(
        default=None,
        description="Filter list to mounts this cluster instance does not already use",
    ),
    cluster_id: Optional[int] = Field(
        default=None, description="Cluster instance id filter for action='clusters'"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description=(
            "Required for delete. Leave it out first: the tool reads the record back "
            "and describes what would be lost. Only after the user confirms, repeat "
            "the call with confirm=True"
        ),
    ),
) -> str:
    """Create, read, update or delete a mount, or list cluster usage of mounts."""
    try:
        action = (action or "").strip().lower()
        if action not in _MOUNT_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"Hint: Use one of {', '.join(_MOUNT_ACTIONS)}."
            )

        if action == "clusters":
            params: Dict[str, Any] = {"pageNumber": page, "limit": limit}
            if _provided(cluster_id):
                params["cluster_id"] = cluster_id
            if _provided(mount_id):
                params["mount_id"] = mount_id
            types = _csv_filter(storage_types)
            if types:
                params["storage_types"] = types

            async with YeeduClient() as client:
                data = await client.get("/api/v1/cluster/mount/map", params=params)

            err = _api_error(data)
            if err:
                return (
                    f"Error: {err}. Hint: cluster_id is a cluster instance id from "
                    "list_clusters; mount_id comes from "
                    "manage_cluster_mount(action='list')."
                )
            rows = _rows(data)
            if not rows:
                return "No mounts are attached to any cluster for that filter."
            return _csv(rows) + _page_note(data, page)

        body: Dict[str, Any] = {}
        if action in ("create", "edit"):
            body = _body(
                name=name,
                storage_type_id=storage_type_id,
                source=source,
                mount_point=mount_point,
                credentials_config_id=credentials_config_id,
            )
            configs = _as_labels(extra_configs)
            if configs:
                body["extra_configs"] = configs
            if _provided(is_read_only):
                body["is_read_only"] = bool(is_read_only)
            if action == "create":
                missing = [
                    label for label, val in (
                        ("name", name),
                        ("storage_type_id", storage_type_id),
                        ("source", source),
                        ("mount_point", mount_point),
                        ("credentials_config_id", credentials_config_id),
                    ) if not _provided(val)
                ]
                if missing:
                    return (
                        f"Error: create needs {', '.join(missing)}. Hint: Get "
                        "credentials_config_id from "
                        "manage_credential_config(action='list')."
                    )
                body.setdefault("is_read_only", bool(is_read_only) if _provided(is_read_only) else False)

        filters: Dict[str, Any] = {}
        if action == "list":
            # /cluster/mounts has no /search variant — mount_name is a filter here.
            if _provided(mount_name):
                filters["mount_name"] = mount_name
            types = _csv_filter(storage_types)
            if types:
                filters["storage_types"] = types
            if _provided(exclude_cluster_id):
                filters["exclude_cluster_id"] = exclude_cluster_id

        return await _crud(
            "mount", action,
            selector_id=mount_id,
            selector_name=None if action == "list" else mount_name,
            body=body,
            filters=filters,
            page=page, limit=limit, confirm=confirmed(confirm),
        )
    except Exception as exc:
        return (
            f"Error managing mount: {exc}. "
            "Hint: Confirm the action and ids, then retry with action='list'."
        )


def _csv_filter(value: Any) -> Optional[str]:
    """Render a filter that accepts several values as a comma-separated string."""
    if not _provided(value):
        return None
    items = _as_list(value)
    if not items:
        return None
    return ",".join(items)
