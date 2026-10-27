"""Metastore and Data Catalog tools for Yeedu MCP Server."""

import os
from typing import Any, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.params import opt


@mcp.tool(
    description="Lists all metastore catalog configurations (Hive, Glue, Unity) with optional name search. Returns CSV: catalog_id, catalog_name, catalog_type, description with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_metastore_catalogs(
    catalog_name: Optional[str] = Field(
        default=None, description="Search by catalog name"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
) -> str:
    """List all metastore catalogs configured in the system."""
    try:
        params = {"pageNumber": page, "limit": limit}

        async with YeeduClient() as client:
            if catalog_name:
                params["metastore_catalog_name"] = catalog_name
                data = await client.get("/api/v1/metastore/catalogs/search", params=params)
            else:
                data = await client.get("/api/v1/metastore/catalogs", params=params)

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            if not data or "data" not in data:
                return "No metastore catalogs found."

            lines = ["catalog_id,catalog_name,catalog_type,description"]
            for cat in data["data"]:
                lines.append(
                    f"{cat.get('metastore_catalog_id', 'N/A')},"
                    f"{cat.get('name', 'N/A')},"
                    f"{cat.get('catalog_type', 'N/A')},"
                    f"{cat.get('description') or 'None'}"
                )

            result_set = data.get("result_set", {})
            if result_set:
                lines.append(
                    f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} "
                    f"| Total: {result_set.get('total_objects', len(data['data']))}"
                )

            return "\n".join(lines)

    except Exception as e:
        return f"Error listing metastore catalogs: {str(e)}. HINT: Verify your API connection and ensure the server configuration is correct."


@mcp.tool(
    description="Browses objects within a metastore catalog: catalogs, schemas, tables, table_summaries, functions, or volumes. Requires metastore_catalog_id and workspace_id. To get the initial list of catalogs for Unity/Databricks metastores, set object_type='catalogs' (catalog_name is not needed). For other object types (schemas, tables, table_summaries, functions, volumes) in Unity/Databricks metastores, catalog_name is required. For Hive metastores, schema_name is required for tables/functions/volumes. Returns CSV with object-type-specific columns.",
    annotations={"readOnlyHint": True}
)
async def browse_catalog(
    metastore_catalog_id: int = Field(description="Metastore catalog ID"),
    workspace_id: int = Field(
        description="Workspace ID (required for metastore browse APIs)"),
    object_type: str = Field(
        description="What to browse: catalogs, schemas, tables, table_summaries, functions, or volumes"
    ),
    catalog_name: Optional[str] = Field(
        default=None, description="Catalog name (required for schemas/tables/functions/volumes in Unity/Databricks metastores; omit when object_type='catalogs' or for Hive)"),
    schema_name: Optional[str] = Field(
        default=None, description="Schema/database name filter (required for tables/functions/volumes in Hive)"),
    search_name: Optional[str] = Field(
        default=None, description="Name to search for (supported for catalogs and schemas)"),
) -> str:
    """Browse metastore catalog objects — catalogs, schemas, tables, functions, volumes."""
    try:
        valid_types = ["catalogs", "schemas", "tables", "table_summaries", "functions", "volumes"]
        object_type = object_type.lower().strip()
        if object_type not in valid_types:
            return f"Error: Invalid object_type '{object_type}'. Must be one of: {', '.join(valid_types)}"

        # Browse endpoints do not support pageNumber/limit — omit them
        params: dict = {"workspace_id": workspace_id}

        # catalog_name accepted by object types other than catalogs
        if catalog_name:
            params["catalog_name"] = catalog_name
        # schema_name accepted by tables, functions, volumes
        if schema_name and object_type in ("tables", "functions", "volumes"):
            params["schema_name"] = schema_name

        async with YeeduClient() as client:
            # Only /catalogs has a /search sub-endpoint
            if search_name and object_type == "catalogs":
                params["name"] = search_name
                endpoint = f"/api/v1/metastore/{metastore_catalog_id}/catalogs/search"
            else:
                endpoint = f"/api/v1/metastore/{metastore_catalog_id}/{object_type}"
                if search_name:
                    params["name"] = search_name

            data = await client.get(endpoint, params=params)

            if isinstance(data, dict) and "error_message" in data:
                err = data['error_message']
                if "Catalog name is required" in err:
                    return f"Error: {err}. HINT: Use browse_catalog with object_type='catalogs' first to get available catalog names for this Unity metastore."
                return f"Error: {err}"
            if isinstance(data, dict) and "error" in data:
                err = data['error']
                if "Catalog name is required" in err:
                    return f"Error: {err}. HINT: Use browse_catalog with object_type='catalogs' first to get available catalog names for this Unity metastore."
                return f"Error: {err}"

            # Browse endpoints return a plain list; catalog list/search returns {data: []}
            items = data if isinstance(data, list) else data.get("data", [])

            if not items:
                return f"No {object_type} found in catalog {metastore_catalog_id}."

            if isinstance(items, list):
                if object_type == "catalogs":
                    lines = ["catalog_name"]
                    for item in items:
                        if isinstance(item, dict):
                            lines.append(
                                item.get("catalog_name", item.get("name", item.get("NAME", item.get("full_name", "N/A")))))
                        else:
                            lines.append(str(item))
                elif object_type == "schemas":
                    lines = ["schema_name"]
                    for item in items:
                        if isinstance(item, dict):
                            # Hive JDBC returns NAME (uppercase); Unity returns name/schema_name
                            lines.append(
                                item.get("NAME", item.get("schema_name", item.get("name", item.get("full_name", "N/A")))))
                        else:
                            lines.append(str(item))
                elif object_type == "tables":
                    # Hive returns TBL_NAME/TBL_TYPE/full_name; Unity returns table_name/table_type
                    lines = ["table_name,table_type,full_name"]
                    for item in items:
                        if isinstance(item, dict):
                            lines.append(
                                f"{item.get('TBL_NAME', item.get('table_name', item.get('name', 'N/A')))},"
                                f"{item.get('TBL_TYPE', item.get('table_type', 'N/A'))},"
                                f"{item.get('full_name', 'N/A')}"
                            )
                        else:
                            lines.append(str(item))
                elif object_type == "table_summaries":
                    lines = ["full_name,table_type"]
                    for item in items:
                        if isinstance(item, dict):
                            lines.append(
                                f"{item.get('full_name', item.get('table_name', 'N/A'))},"
                                f"{item.get('table_type', item.get('TBL_TYPE', 'N/A'))}"
                            )
                        else:
                            lines.append(str(item))
                elif object_type == "functions":
                    # Hive returns FUNC_NAME/CLASS_NAME; Unity returns function_name
                    lines = ["function_name,class_name"]
                    for item in items:
                        if isinstance(item, dict):
                            lines.append(
                                f"{item.get('FUNC_NAME', item.get('function_name', item.get('name', 'N/A')))},"
                                f"{item.get('CLASS_NAME', item.get('class_name', 'N/A'))}"
                            )
                        else:
                            lines.append(str(item))
                elif object_type == "volumes":
                    lines = ["volume_name,volume_type,catalog_name,schema_name"]
                    for item in items:
                        if isinstance(item, dict):
                            lines.append(
                                f"{item.get('volume_name', item.get('name', 'N/A'))},"
                                f"{item.get('volume_type', 'N/A')},"
                                f"{item.get('catalog_name', 'N/A')},"
                                f"{item.get('schema_name', 'N/A')}"
                            )
                        else:
                            lines.append(str(item))
                else:
                    lines = [str(item) for item in items]

                return "\n".join(lines)

            return str(data)

    except Exception as e:
        return f"Error browsing catalog: {str(e)}. HINT: Double-check workspace_id and metastore_catalog_id. If using Unity Catalog, set object_type='catalogs' to fetch catalog names first."


@mcp.tool(
    description="Gets either the DDL (CREATE TABLE statement) or column definitions for a specific table in a metastore catalog. detail_type=ddl returns the raw DDL string. detail_type=columns returns CSV: column_name, data_type, is_nullable, comment. catalog_name is optional — omit for Hive (schema.table), provide for Unity (catalog.schema.table).",
    annotations={"readOnlyHint": True}
)
async def get_table_details(
    metastore_catalog_id: int = Field(description="Metastore catalog ID"),
    workspace_id: int = Field(
        description="Workspace ID (required for metastore APIs)"),
    detail_type: str = Field(description="Type of detail: ddl or columns"),
    schema_name: str = Field(
        description="Schema/database name containing the table"),
    table_name: str = Field(description="Table name to get details for"),
    catalog_name: Optional[str] = Field(
        default=None,
        description="Catalog name — required for Unity (catalog.schema.table), omit for Hive"),
) -> str:
    """Get DDL statement or column definitions for a table."""
    try:
        valid_types = ["ddl", "columns"]
        if detail_type not in valid_types:
            return f"Error: Invalid detail_type '{detail_type}'. Must be: ddl or columns"

        async with YeeduClient() as client:
            if detail_type == "ddl":
                # API expects an array of fully-qualified table name strings:
                # Hive:  ["schema_name.table_name"]
                # Unity: ["catalog_name.schema_name.table_name"]
                if catalog_name:
                    table_ref = f"{catalog_name}.{schema_name}.{table_name}"
                else:
                    table_ref = f"{schema_name}.{table_name}"

                data = await client.post(
                    f"/api/v1/metastore/{metastore_catalog_id}/table_ddl",
                    params={"workspace_id": workspace_id},
                    json=[table_ref],
                )

                if isinstance(data, dict) and "error_message" in data:
                    return f"Error: {data['error_message']}"

                # Response: [{"<table_ref>": "CREATE TABLE ..."}, ...]
                if isinstance(data, list) and data:
                    ddl_item = data[0]
                    if isinstance(ddl_item, dict):
                        # Key is the table full name; value is the DDL string
                        ddl_value = next(iter(ddl_item.values()), None)
                        if ddl_value:
                            return str(ddl_value)
                        return str(ddl_item)
                    return str(ddl_item)
                elif isinstance(data, dict):
                    ddl_value = next(iter(data.values()), None)
                    return str(ddl_value) if ddl_value else str(data)
                return str(data)

            else:  # columns
                params: dict = {
                    "workspace_id": workspace_id,
                    "schema_name": schema_name,
                    "table_name": table_name,
                }
                if catalog_name:
                    params["catalog_name"] = catalog_name

                data = await client.get(
                    f"/api/v1/metastore/{metastore_catalog_id}/columns",
                    params=params,
                )

                if isinstance(data, dict) and "error_message" in data:
                    return f"Error: {data['error_message']}"

                items = data if isinstance(
                    data, list) else data.get("data", [])

                # Hive returns COLUMN_NAME/TYPE_NAME/COMMENT; Unity returns column_name/data_type
                lines = ["column_name,data_type,is_nullable,comment"]
                for col in items:
                    if isinstance(col, dict):
                        lines.append(
                            f"{col.get('COLUMN_NAME', col.get('column_name', col.get('name', 'N/A')))},"
                            f"{col.get('TYPE_NAME', col.get('type_text', col.get('data_type', 'N/A')))},"
                            f"{col.get('nullable', col.get('is_nullable', 'N/A'))},"
                            f"{col.get('COMMENT', col.get('comment', '')) or ''}"
                        )
                    else:
                        lines.append(str(col))

                return "\n".join(lines)

    except Exception as e:
        return f"Error getting table details: {str(e)}. HINT: Ensure schema_name, table_name, detail_type, and workspace_id are correct."


# ---------------------------------------------------------------------------
# Catalog configuration
#
# Yeedu keeps one endpoint per catalog flavour, so catalog_type selects both
# the URL segment and which fields are accepted. Hive takes configuration
# files: the file is read here and its text is sent, which is why those fields
# are paths on the machine running this server.
#
# None of this carries a secret. A Unity token or a Kerberos keytab arrives
# later as a linked secret — see manage_catalog_secret_link.
# ---------------------------------------------------------------------------

_CATALOG_ACTIONS = ("create", "edit", "delete")

#: Per catalog type: URL segment, display name.
_CATALOG_TYPES = {
    "hive": ("hive", "Hive"),
    "unity": ("unity", "Databricks Unity"),
    "glue": ("glue", "AWS Glue"),
}


def _catalog_record(data: Any) -> str:
    """Render the catalog record an action returned."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    return (
        f"catalog_id={data.get('metastore_catalog_id', 'N/A')} "
        f"name={data.get('name', 'N/A')} "
        f"type={data.get('catalog_type', 'N/A')}"
    )


@mcp.tool(description=(
    "Creates, edits or deletes a metastore catalog configuration. "
    "catalog_type is hive, unity or glue and decides which fields apply. "
    "unity create needs name, endpoint, default_catalog and storage_path, and accepts "
    "additional_catalogs. hive create needs name and accepts the four configuration "
    "file paths, which are read from the filesystem where this MCP server runs and "
    "sent as their contents. glue takes name and description only. "
    "edit needs metastore_catalog_id plus the fields to change. "
    "delete needs metastore_catalog_id and requires confirm=True: the first call "
    "returns the catalog it would delete and does nothing, so ask the user before "
    "repeating the call with confirm=True. "
    "No secret is passed here — a Unity token or a Kerberos keytab is attached "
    "afterwards with manage_catalog_secret_link. "
    "Use list_metastore_catalogs to read existing catalogs."
))
async def manage_metastore_catalog(
    action: str = Field(description="Action: create, edit, or delete"),
    catalog_type: str = Field(description="Catalog type: hive, unity, or glue"),
    metastore_catalog_id: Optional[int] = Field(
        default=None, description="Catalog ID (required for edit and delete)"),
    name: Optional[str] = Field(
        default=None, description="Catalog name (required on create)"),
    description: Optional[str] = Field(
        default=None, description="Catalog description"),
    endpoint: Optional[str] = Field(
        default=None, description="Databricks workspace endpoint URL (unity)"),
    default_catalog: Optional[str] = Field(
        default=None, description="Default catalog name (unity)"),
    additional_catalogs: Optional[Any] = Field(
        default=None,
        description="Further catalog names to expose, as a list or a comma-separated string (unity)"),
    storage_path: Optional[str] = Field(
        default=None, description="Storage path, e.g. an S3 or ADLS URI (unity)"),
    hive_site_xml_file_path: Optional[str] = Field(
        default=None, description="Path to hive-site.xml on the machine running this server (hive)"),
    core_site_xml_file_path: Optional[str] = Field(
        default=None, description="Path to core-site.xml on the machine running this server (hive)"),
    hdfs_site_xml_file_path: Optional[str] = Field(
        default=None, description="Path to hdfs-site.xml on the machine running this server (hive)"),
    krb5_conf_file_path: Optional[str] = Field(
        default=None, description="Path to krb5.conf on the machine running this server (hive)"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='delete'. Set it only after the user has confirmed this specific catalog.",
    ),
) -> str:
    """Create, edit or delete a metastore catalog configuration."""
    try:
        action = (opt(action) or "").strip().lower()
        kind = (opt(catalog_type) or "").strip().lower()
        metastore_catalog_id = opt(metastore_catalog_id)
        name = opt(name)
        description = opt(description)
        endpoint = opt(endpoint)
        default_catalog = opt(default_catalog)
        additional_catalogs = opt(additional_catalogs)
        storage_path = opt(storage_path)
        hive_site_xml_file_path = opt(hive_site_xml_file_path)
        core_site_xml_file_path = opt(core_site_xml_file_path)
        hdfs_site_xml_file_path = opt(hdfs_site_xml_file_path)
        krb5_conf_file_path = opt(krb5_conf_file_path)

        if action not in _CATALOG_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_CATALOG_ACTIONS)}. "
                "Use list_metastore_catalogs to read catalogs."
            )
        if kind not in _CATALOG_TYPES:
            return (
                f"Error: Invalid catalog_type '{catalog_type}'. "
                f"HINT: Must be one of: {', '.join(_CATALOG_TYPES)}."
            )

        segment, label = _CATALOG_TYPES[kind]
        url = f"/api/v1/metastore/catalog/{segment}"

        if action in ("edit", "delete") and metastore_catalog_id is None:
            return (
                f"Error: metastore_catalog_id is required for action='{action}'. "
                "HINT: Call list_metastore_catalogs to find it."
            )

        if action == "delete":
            # Refuse before opening a connection, so an unreachable API cannot
            # turn a refusal into an error the agent retries its way past.
            if not confirmed(confirm):
                record = None
                try:
                    async with YeeduClient() as client:
                        data = await client.get(
                            "/api/v1/metastore/catalogs",
                            params={"metastore_catalog_id": metastore_catalog_id},
                        )
                    rows = data.get("data") if isinstance(data, dict) else data
                    record = _catalog_record(rows)
                except Exception:
                    record = None
                return confirmation_required(
                    f"{label} metastore catalog",
                    f"metastore_catalog_id={metastore_catalog_id}",
                    "manage_metastore_catalog",
                    record=record,
                    impact=(
                        "Workspaces and notebooks that browse this catalog lose access to it, "
                        "and every secret linked to it is unlinked. The metastore itself is "
                        "untouched."
                    ),
                    retry=f"catalog_type='{kind}', metastore_catalog_id={metastore_catalog_id}",
                )

            async with YeeduClient() as client:
                await client.delete(
                    url, params={"metastore_catalog_id": metastore_catalog_id})
            return f"{label} metastore catalog {metastore_catalog_id} deleted successfully."

        body: dict = {}
        if name:
            body["name"] = name
        if description is not None:
            body["description"] = description

        if kind == "unity":
            if endpoint:
                body["endpoint"] = endpoint
            if default_catalog:
                body["default_catalog"] = default_catalog
            if storage_path:
                body["storage_path"] = storage_path
            extra = additional_catalogs
            if isinstance(extra, str):
                extra = [p.strip() for p in extra.split(",") if p.strip()]
            if extra:
                body["additional_catalogs"] = [str(c) for c in extra]
            if action == "create":
                missing = [
                    field for field, value in (
                        ("name", name),
                        ("endpoint", endpoint),
                        ("default_catalog", default_catalog),
                        ("storage_path", storage_path),
                    ) if not value
                ]
                if missing:
                    return (
                        f"Error: create needs {', '.join(missing)} for catalog_type='unity'. "
                        "HINT: endpoint is the Databricks workspace URL; the access token is "
                        "attached afterwards as a linked secret."
                    )

        elif kind == "hive":
            # Each path's contents go into the body under the platform's own
            # field name; "null" clears the field on edit.
            hive_files = (
                ("hive_site_xml_file_path", "hiveSiteXml", hive_site_xml_file_path),
                ("core_site_xml_file_path", "coreSiteXml", core_site_xml_file_path),
                ("hdfs_site_xml_file_path", "hdfsSiteXml", hdfs_site_xml_file_path),
                ("krb5_conf_file_path", "krb5Conf", krb5_conf_file_path),
            )
            for arg, field, path in hive_files:
                if not path:
                    continue
                if str(path).strip().lower() == "null":
                    body[field] = None
                    continue
                if not os.path.isfile(path):
                    return (
                        f"Error: No file at {path} (for {arg}). "
                        "HINT: The path is read on the machine running this MCP server, "
                        "not on the Yeedu cluster."
                    )
                with open(path, "r", encoding="utf-8") as handle:
                    body[field] = handle.read()
            if action == "create" and not name:
                return "Error: create needs name for catalog_type='hive'."

        else:  # glue
            rejected = [
                field for field, value in (
                    ("endpoint", endpoint),
                    ("default_catalog", default_catalog),
                    ("storage_path", storage_path),
                    ("additional_catalogs", additional_catalogs),
                ) if value
            ]
            if rejected:
                return (
                    f"Error: catalog_type='glue' does not accept {', '.join(rejected)}. "
                    "HINT: A Glue catalog takes name and description only; its AWS "
                    "credentials are attached as a linked secret."
                )
            if action == "create" and not name:
                return "Error: create needs name for catalog_type='glue'."

        if not body:
            return (
                f"Error: No fields supplied to {action}. "
                "HINT: Check this tool's description for the fields this catalog_type takes."
            )

        async with YeeduClient() as client:
            if action == "create":
                data = await client.post(url, json=body)
            else:
                data = await client.put(
                    url,
                    params={"metastore_catalog_id": metastore_catalog_id},
                    json=body,
                )

        if isinstance(data, dict) and data.get("error_message"):
            return (
                f"Error: {data['error_message']}. "
                "HINT: Confirm the catalog name is not already taken and that every "
                "field belongs to this catalog_type."
            )

        verb = "created" if action == "create" else "updated"
        record = _catalog_record(data)
        return (
            f"{label} metastore catalog {verb} successfully."
            + (f"\n\n{record}" if record else "")
        )

    except Exception as e:
        return (
            f"Error running action '{action}' on metastore catalog: {str(e)}. "
            "HINT: Call list_metastore_catalogs to check what exists, and confirm "
            "catalog_type matches the catalog you are editing."
        )


# ---------------------------------------------------------------------------
# Catalog ↔ secret links
#
# Linking takes secret *ids*, never secret values, so the whole lifecycle
# belongs here; the secret itself is created by the user in the UI or CLI.
#
# A link binds a catalog to a metastore secret (the credential the metastore
# needs, such as a Unity token or a Hive principal) and optionally to an object
# storage secret (the credential for the data files behind it). The scope
# decides who the link applies to: one user, a workspace, or the tenant.
# ---------------------------------------------------------------------------

_LINK_ACTIONS = ("list", "link", "update", "unlink")

#: Per scope: URL segment, link id field, secret id field infix.
_LINK_SCOPES = {
    "user": ("user", "metastore_catalog_secret_user_id", "user"),
    "workspace": ("workspace", "metastore_catalog_secret_workspace_id", "workspace"),
    "tenant": ("tenant", "metastore_catalog_secret_tenant_id", "tenant"),
}


@mcp.tool(description=(
    "Manages the link between a metastore catalog and the secrets used to reach it. "
    "scope is user, workspace or tenant and decides who the link applies to. "
    "action=list returns the existing links, filtered by metastore_catalog_id, "
    "metastore_catalog_name, catalog_type, link_id, or user_id/workspace_id. "
    "action=link needs metastore_catalog_id and metastore_secret_id, and accepts "
    "object_storage_secret_id for the credential to the data files behind the catalog. "
    "action=update needs link_id plus the secret ids to change. "
    "action=unlink needs link_id and requires confirm=True: the first call returns the "
    "link it would remove and does nothing, so ask the user before repeating the call "
    "with confirm=True. "
    "Secret ids come from list_user_secrets and list_workspace_secrets; this tool never "
    "takes a secret value."
))
async def manage_catalog_secret_link(
    scope: str = Field(description="Link scope: user, workspace, or tenant"),
    action: str = Field(description="Action: list, link, update, or unlink"),
    metastore_catalog_id: Optional[int] = Field(
        default=None, description="Catalog ID (required for link, filter for list)"),
    metastore_secret_id: Optional[int] = Field(
        default=None,
        description="Secret ID holding the metastore credential, at the chosen scope (required for link)",
    ),
    object_storage_secret_id: Optional[int] = Field(
        default=None,
        description="Secret ID holding the object storage credential, at the chosen scope (optional)",
    ),
    link_id: Optional[int] = Field(
        default=None, description="Existing link ID (required for update and unlink)"),
    user_id: Optional[int] = Field(
        default=None, description="Filter list by user ID (scope='user')"),
    workspace_id: Optional[int] = Field(
        default=None, description="Filter list by workspace ID (scope='workspace')"),
    metastore_catalog_name: Optional[str] = Field(
        default=None, description="Filter list by catalog name"),
    catalog_type: Optional[str] = Field(
        default=None,
        description="Filter list by catalog type: HIVE, DATABRICKS UNITY, or AWS GLUE"),
    limit: int = Field(default=20, ge=1, le=100, description="Items per page"),
    page: int = Field(default=1, ge=1, description="Page number"),
    confirm: bool = Field(
        default=False,
        description="Must be True for action='unlink'. Set it only after the user has confirmed this specific link.",
    ),
) -> str:
    """List, create, update or remove a catalog-to-secret link."""
    try:
        scope = (opt(scope) or "").strip().lower()
        action = (opt(action) or "").strip().lower()
        metastore_catalog_id = opt(metastore_catalog_id)
        metastore_secret_id = opt(metastore_secret_id)
        object_storage_secret_id = opt(object_storage_secret_id)
        link_id = opt(link_id)
        user_id = opt(user_id)
        workspace_id = opt(workspace_id)
        metastore_catalog_name = opt(metastore_catalog_name)
        catalog_type = opt(catalog_type)
        limit = opt(limit) or 20
        page = opt(page) or 1

        if scope not in _LINK_SCOPES:
            return (
                f"Error: Invalid scope '{scope}'. "
                f"HINT: Must be one of: {', '.join(_LINK_SCOPES)}."
            )
        if action not in _LINK_ACTIONS:
            return (
                f"Error: Invalid action '{action}'. "
                f"HINT: Must be one of: {', '.join(_LINK_ACTIONS)}."
            )

        segment, link_id_field, infix = _LINK_SCOPES[scope]
        url = f"/api/v1/metastore/catalog/secret/{segment}"
        metastore_field = f"metastore_secrets_{infix}_id"
        storage_field = f"object_storage_secrets_{infix}_id"

        if action in ("update", "unlink") and link_id is None:
            return (
                f"Error: link_id is required for action='{action}'. "
                f"HINT: Call manage_catalog_secret_link(scope='{scope}', action='list') "
                f"and use the {link_id_field} column."
            )

        if action == "list":
            params: dict = {"pageNumber": page, "limit": limit}
            if link_id is not None:
                params[link_id_field] = link_id
            if metastore_catalog_id is not None:
                params["metastore_catalog_id"] = metastore_catalog_id
            if metastore_catalog_name:
                params["metastore_catalog_name"] = metastore_catalog_name
            if catalog_type:
                params["catalog_type"] = catalog_type
            if scope == "user" and user_id is not None:
                params["user_id"] = user_id
            if scope == "workspace" and workspace_id is not None:
                params["workspace_id"] = workspace_id

            async with YeeduClient() as client:
                data = await client.get(url, params=params)

            return _link_table(data, link_id_field, page, scope)

        if action == "unlink":
            # Refuse before opening a connection, so an unreachable API cannot
            # turn a refusal into an error the agent retries its way past.
            if not confirmed(confirm):
                record = None
                try:
                    async with YeeduClient() as client:
                        data = await client.get(url, params={link_id_field: link_id})
                    record = _link_table(data, link_id_field, page, scope)
                except Exception:
                    record = None
                return confirmation_required(
                    f"{scope.capitalize()} catalog secret link",
                    f"{link_id_field}={link_id}",
                    "manage_catalog_secret_link",
                    action="unlink",
                    record=record,
                    impact=(
                        f"The catalog stops authenticating for this {scope}, so browsing it "
                        "and jobs reading through it fail until a secret is linked again. "
                        "The secret itself is not deleted."
                    ),
                    retry=f"scope='{scope}', link_id={link_id}",
                )

            async with YeeduClient() as client:
                await client.delete(url, params={link_id_field: link_id})
            return f"Catalog secret link {link_id} removed for scope '{scope}'."

        body: dict = {}
        if metastore_catalog_id is not None:
            body["metastore_catalog_id"] = metastore_catalog_id
        if metastore_secret_id is not None:
            body[metastore_field] = metastore_secret_id
        if object_storage_secret_id is not None:
            body[storage_field] = object_storage_secret_id

        if action == "link":
            missing = [
                field for field, value in (
                    ("metastore_catalog_id", metastore_catalog_id),
                    ("metastore_secret_id", metastore_secret_id),
                ) if value is None
            ]
            if missing:
                return (
                    f"Error: action='link' needs {', '.join(missing)}. "
                    "HINT: Catalog ids come from list_metastore_catalogs; secret ids from "
                    f"list_{'user' if scope == 'user' else 'workspace'}_secrets."
                )
            async with YeeduClient() as client:
                await client.post(url, json=body)
            return (
                f"Catalog {metastore_catalog_id} linked to secret {metastore_secret_id} "
                f"for scope '{scope}'."
            )

        # update
        if metastore_secret_id is None and object_storage_secret_id is None:
            return (
                "Error: action='update' needs metastore_secret_id and/or "
                "object_storage_secret_id to change."
            )
        body[link_id_field] = link_id
        async with YeeduClient() as client:
            await client.put(url, json=body)
        return f"Catalog secret link {link_id} updated for scope '{scope}'."

    except Exception as e:
        return (
            f"Error running action '{action}' on catalog secret link: {str(e)}. "
            "HINT: Confirm the catalog with list_metastore_catalogs and the secret ids "
            "with list_user_secrets or list_workspace_secrets. A secret has to be at the "
            "same scope as the link."
        )


def _linked_secret(row: dict, kind: str, scope: str) -> str:
    """Describe the secret of one kind attached to a link row.

    A listed link nests each secret under `{kind}_secrets_{scope}`, keyed by
    `secrets_{scope}_id`, and leaves it null when none is attached — a
    different shape from the flat `{kind}_secrets_{scope}_id` field the write
    side takes, so reading the write field here finds nothing. The name comes
    along with it, which is what makes the row readable.
    """
    nested = row.get(f"{kind}_secrets_{scope}")
    if isinstance(nested, dict):
        secret_id = nested.get(f"secrets_{scope}_id", "N/A")
        name = nested.get("name")
        return f"{secret_id} ({name})" if name else str(secret_id)
    flat = row.get(f"{kind}_secrets_{scope}_id")
    return str(flat) if flat is not None else "none"


def _link_table(data: Any, link_id_field: str, page: int, scope: str) -> str:
    """Render catalog-secret link rows as CSV."""
    if isinstance(data, dict) and data.get("error_message"):
        return f"Error: {data['error_message']}"
    rows = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return f"No catalog secret links found for scope '{scope}'."

    lines = [
        f"{link_id_field},metastore_catalog_id,catalog_name,catalog_type,"
        "metastore_secret,object_storage_secret"
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        catalog = row.get("metastore_catalog")
        if not isinstance(catalog, dict):
            catalog = {}
        lines.append(
            f"{row.get(link_id_field, 'N/A')},"
            f"{row.get('metastore_catalog_id', catalog.get('metastore_catalog_id', 'N/A'))},"
            f"{catalog.get('name', row.get('metastore_catalog_name', 'N/A'))},"
            f"{catalog.get('catalog_type', row.get('catalog_type', 'N/A'))},"
            f"{_linked_secret(row, 'metastore', scope)},"
            f"{_linked_secret(row, 'object_storage', scope)}"
        )

    if isinstance(data, dict):
        result_set = data.get("result_set")
        if isinstance(result_set, dict):
            lines.append(
                f"\nPage {result_set.get('current_page', page)} of "
                f"{result_set.get('total_pages', 1)} | "
                f"Total: {result_set.get('total_objects', len(rows))}"
            )

    return "\n".join(lines)

