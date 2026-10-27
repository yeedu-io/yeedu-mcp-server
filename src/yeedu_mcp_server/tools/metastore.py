"""Metastore and Data Catalog tools for Yeedu MCP Server."""

from typing import Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp


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

