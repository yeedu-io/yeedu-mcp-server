"""Choosing a cluster to run SQL on.

Three things have to line up before a SQL query can run, and each can fail
independently:

* the cluster's Spark version must have thrift support, or no HiveServer2
  endpoint exists at all;
* the workspace must be attached to the cluster, because the endpoint is
  published per (cluster, workspace) pair;
* the cluster must be bound to the metastore catalog the tables live in. A
  cluster has exactly one. Tables in any other catalog do not exist as far as
  its SQL session is concerned, and Spark reports that as an ordinary
  "table not found" — indistinguishable from a typo unless the caller knows
  which catalog was in scope.

This module reads those facts off the API so a caller can present the real
choices rather than guess at one. Connection coordinates stay inside the
returned record and are never rendered into tool output.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.errors import APIError

logger = logging.getLogger(__name__)

#: Page size for the list endpoints. They cap at 100.
_PAGE = 100

#: Statuses a cluster cannot come back from. A destroyed cluster can be neither
#: started nor attached, so offering it as an option would waste the user's
#: time. Most of a long-lived tenant's cluster list is in this state.
TERMINAL_STATUSES = {"DESTROYED", "DESTROYING", "TERMINATED", "ERROR"}


def is_usable(status: Optional[str]) -> bool:
    return (status or "").upper() not in TERMINAL_STATUSES


def _is_missing(exc: APIError) -> bool:
    """Whether an API error means "no such thing" rather than a failure.

    The status code is the reliable signal: the wording varies by endpoint
    ("No Metastore catalog found.", "not found in cluster instances"), so
    matching on the message misses cases.
    """
    return getattr(exc, "status_code", None) == 404


class SqlTargetError(Exception):
    """The requested cluster cannot serve SQL, with the reason in the message."""


async def _pages(client: YeeduClient, endpoint: str,
                 params: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Read every page of a paginated list endpoint."""
    rows: List[dict] = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"limit": _PAGE, "pageNumber": page})
        data = await client.get(endpoint, params=query)
        batch = (data or {}).get("data") or []
        rows.extend(batch)

        result_set = (data or {}).get("result_set") or {}
        total_pages = result_set.get("total_pages") or 1
        if not batch or page >= total_pages:
            return rows
        page += 1


def _catalog_of(cluster_info: dict) -> Dict[str, Any]:
    catalog = cluster_info.get("metastore_catalog") or {}
    catalog_type = catalog.get("metastore_catalog_type") or {}
    if not isinstance(catalog_type, dict):
        catalog_type = {"name": catalog_type}
    return {
        "catalog_id": catalog.get("metastore_catalog_id"),
        "catalog_name": catalog.get("metastore_catalog_name"),
        "catalog_type": catalog_type.get("name"),
    }


def catalog_label(record: Dict[str, Any]) -> str:
    """Render a catalog for a human, e.g. `hive (id 74, HIVE)`."""
    name = record.get("catalog_name")
    catalog_id = record.get("catalog_id")
    kind = record.get("catalog_type")
    if not name and catalog_id is None:
        return "none (this cluster is bound to no metastore catalog)"
    parts = [f"id {catalog_id}"] if catalog_id is not None else []
    if kind:
        parts.append(str(kind))
    suffix = f" ({', '.join(parts)})" if parts else ""
    return f"{name or 'unnamed'}{suffix}"


def _flatten(cluster_info: dict) -> Dict[str, Any]:
    spark = cluster_info.get("spark_infra_version") or {}
    record = {
        "cluster_id": cluster_info.get("cluster_id"),
        "name": cluster_info.get("name"),
        "status": cluster_info.get("cluster_status"),
        "cluster_type": cluster_info.get("cluster_type"),
        "spark_version": spark.get("spark_version"),
        "thrift_support": bool(spark.get("thrift_support")),
    }
    record.update(_catalog_of(cluster_info))
    return record


async def tenant_clusters() -> List[Dict[str, Any]]:
    """Every cluster in the tenant, flattened to the fields SQL routing needs."""
    async with YeeduClient() as client:
        rows = await _pages(client, "/api/v1/clusters")
    return [_flatten(row) for row in rows]


async def workspace_clusters(workspace_id: int) -> List[Dict[str, Any]]:
    """Clusters attached to a workspace, each with its Thrift endpoint if any.

    The endpoint is kept under `endpoint` and must not be rendered into output.
    """
    async with YeeduClient() as client:
        rows = await _pages(
            client, f"/api/v1/ac/workspace/{workspace_id}/clusters")

    clusters = []
    for row in rows:
        record = _flatten(row.get("cluster_info") or {})
        endpoint = row.get("thrift_connection") or None
        record["endpoint"] = endpoint if (
            endpoint and endpoint.get("host")) else None
        record["attached"] = True
        clusters.append(record)
    return clusters


async def cluster_workspace_ids(cluster_id: int) -> List[int]:
    """Workspace IDs attached to a cluster. Used to explain an attach failure."""
    async with YeeduClient() as client:
        rows = await _pages(
            client, f"/api/v1/ac/cluster/{cluster_id}/workspaces")
    ids = []
    for row in rows:
        workspace = row.get("workspace") or {}
        if workspace.get("workspace_id") is not None:
            ids.append(workspace["workspace_id"])
    return ids


async def list_catalogs(name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Metastore catalogs configured for the tenant, optionally filtered by name."""
    endpoint = "/api/v1/metastore/catalogs"
    params: Dict[str, Any] = {}
    if name:
        endpoint = "/api/v1/metastore/catalogs/search"
        params["metastore_catalog_name"] = name

    try:
        async with YeeduClient() as client:
            rows = await _pages(client, endpoint, params=params)
    except APIError as exc:
        # The search endpoint 404s on no match instead of returning an empty
        # page; that is an empty result, not a failure.
        if _is_missing(exc):
            return []
        raise

    return [
        {
            "catalog_id": row.get("metastore_catalog_id"),
            "catalog_name": row.get("name"),
            "catalog_type": row.get("catalog_type"),
            "description": row.get("description"),
        }
        for row in rows
    ]


async def resolve_catalog(catalog_id: Optional[int] = None,
                          catalog_name: Optional[str] = None
                          ) -> List[Dict[str, Any]]:
    """Resolve a catalog by id or name. Returns every match, so an ambiguous
    name can be put back to the user rather than silently resolved."""
    if catalog_id is not None:
        catalogs = await list_catalogs()
        return [c for c in catalogs if c["catalog_id"] == catalog_id]

    if catalog_name:
        matches = await list_catalogs(name=catalog_name)
        exact = [c for c in matches
                 if (c["catalog_name"] or "").lower() == catalog_name.lower()]
        return exact or matches

    return []


async def get_sql_target(cluster_id: int, workspace_id: int) -> Dict[str, Any]:
    """Preflight a (cluster, workspace) pair and return its SQL target record.

    Raises SqlTargetError naming the one thing that is wrong, so the caller
    never has to infer it from a socket timeout.
    """
    try:
        clusters = await workspace_clusters(workspace_id)
    except APIError as exc:
        if _is_missing(exc):
            raise SqlTargetError(
                f"Workspace {workspace_id} does not exist, or this account "
                f"cannot see it. Use list_workspaces to find the right id."
            ) from exc
        raise

    record = next(
        (c for c in clusters if c["cluster_id"] == cluster_id), None)

    if record is None:
        try:
            attached_to = await cluster_workspace_ids(cluster_id)
        except APIError as exc:
            if _is_missing(exc):
                raise SqlTargetError(
                    f"Cluster {cluster_id} does not exist. Use "
                    f"find_sql_clusters(workspace_id={workspace_id}) to list "
                    f"the clusters that can serve SQL here."
                ) from exc
            raise

        where = (f" It is attached to workspace(s) "
                 f"{', '.join(str(i) for i in attached_to)}."
                 if attached_to else
                 " It is not attached to any workspace.")
        raise SqlTargetError(
            f"Cluster {cluster_id} is not attached to workspace "
            f"{workspace_id}, so no Thrift endpoint is published for that "
            f"pair.{where} Use find_sql_clusters(workspace_id="
            f"{workspace_id}) to see which clusters can serve SQL here. "
            f"Attaching a cluster changes access control — ask the user "
            f"before calling manage_cluster_workspace_access."
        )

    if not is_usable(record["status"]):
        raise SqlTargetError(
            f"Cluster {cluster_id} ({record['name']}) is {record['status']} "
            f"and cannot be used or restarted. Use find_sql_clusters("
            f"metastore_catalog_id={record['catalog_id']}, workspace_id="
            f"{workspace_id}) to see the clusters that can serve SQL here."
        )

    if not record["thrift_support"]:
        raise SqlTargetError(
            f"Cluster {cluster_id} ({record['name']}) runs Spark "
            f"{record['spark_version']}, whose image has no thrift support, so "
            f"it publishes no JDBC/HiveServer2 endpoint. Use "
            f"find_sql_clusters(metastore_catalog_id="
            f"{record['catalog_id']}, workspace_id={workspace_id}) to find one "
            f"on the same metastore catalog that does."
        )

    if record["status"] != "RUNNING":
        raise SqlTargetError(
            f"Cluster {cluster_id} ({record['name']}) is {record['status']}, "
            f"not RUNNING, so its Thrift endpoint is down. Start it with "
            f"cluster_action(cluster_id={cluster_id}, action='start') and wait "
            f"for RUNNING, or pick another cluster with find_sql_clusters("
            f"metastore_catalog_id={record['catalog_id']}, workspace_id="
            f"{workspace_id})."
        )

    if not record["endpoint"]:
        raise SqlTargetError(
            f"Cluster {cluster_id} ({record['name']}) is RUNNING with thrift "
            f"support but publishes no endpoint for workspace {workspace_id} "
            f"yet. This is usually transient while the Thrift server starts — "
            f"retry shortly, or check get_cluster_details({cluster_id})."
        )

    record["catalog_label"] = catalog_label(record)
    return record


# --- metastore object lookup -------------------------------------------------
#
# When a caller names a bare table or schema, the catalog it lives in has to be
# established before a cluster can be chosen. The browse endpoints answer that
# without a running cluster: they reach the metastore directly, and the
# workspace only scopes authorisation.

def _schema_name_of(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return str(item) if item else None
    return item.get("NAME") or item.get("schema_name") or item.get("name")


def _table_name_of(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return str(item) if item else None
    return item.get("TBL_NAME") or item.get("table_name") or item.get("name")


async def _browse(client: YeeduClient, catalog_id: int, object_type: str,
                  workspace_id: int, catalog_name: Optional[str] = None,
                  schema_name: Optional[str] = None) -> List[Any]:
    """One metastore browse call. Browse endpoints take no pagination."""
    params: Dict[str, Any] = {"workspace_id": workspace_id}
    if catalog_name:
        params["catalog_name"] = catalog_name
    if schema_name:
        params["schema_name"] = schema_name

    data = await client.get(
        f"/api/v1/metastore/{catalog_id}/{object_type}", params=params)

    if isinstance(data, dict):
        message = data.get("error_message") or data.get("error")
        if message:
            raise SqlTargetError(str(message))
        return data.get("data") or []
    return data or []


#: How a nested metastore reports that a schema listing needs a catalog name.
_NEEDS_CATALOG_NAME = re.compile(r"catalog name is required", re.I)


async def _schema_listing(client: YeeduClient, catalog: Dict[str, Any],
                          workspace_id: int
                          ) -> List[Tuple[Optional[str], List[str]]]:
    """Return (namespace, schema names) pairs for a catalog.

    Unity and Databricks metastores nest schemas under a catalog name and
    reject a listing without one; Hive and Glue are flat. The catalog type is
    spelled differently per metastore — `DATABRICKS UNITY` here, not `unity` —
    so the label is used as a first guess only, and the API's own complaint
    decides it when the guess is wrong.
    """
    catalog_id = catalog["catalog_id"]
    kind = (catalog["catalog_type"] or "").lower()
    nested = "unity" in kind or "databricks" in kind

    if not nested:
        try:
            items = await _browse(client, catalog_id, "schemas", workspace_id)
            return [(None, [s for s in map(_schema_name_of, items) if s])]
        except (SqlTargetError, APIError) as exc:
            if not _NEEDS_CATALOG_NAME.search(str(exc)):
                raise

    listed = await _browse(client, catalog_id, "catalogs", workspace_id)
    names = [
        (item.get("catalog_name") or item.get("name"))
        if isinstance(item, dict) else str(item)
        for item in listed
    ]

    pairs: List[Tuple[Optional[str], List[str]]] = []
    for namespace in [n for n in names if n] or [None]:
        items = await _browse(client, catalog_id, "schemas", workspace_id,
                              catalog_name=namespace)
        pairs.append(
            (namespace, [s for s in map(_schema_name_of, items) if s]))
    return pairs


async def search_metastores(name: str, workspace_id: int,
                            max_schemas_per_catalog: int = 100
                            ) -> List[Dict[str, Any]]:
    """Find which metastore catalogs hold a schema or table called `name`.

    `name` may be `schema.table`, in which case only that schema is read, or a
    bare name, which is matched against schema names and — for up to
    `max_schemas_per_catalog` schemas per catalog — against table names.
    """
    head, sep, tail = name.strip().partition(".")
    schema_filter = head if sep else None
    wanted = (tail if sep else head).strip().lower()

    catalogs = await list_catalogs()
    findings: List[Dict[str, Any]] = []

    async with YeeduClient() as client:
        for catalog in catalogs:
            entry: Dict[str, Any] = {
                **catalog,
                "schema_hits": [],
                "table_hits": [],
                "schemas_seen": 0,
                "schemas_scanned": 0,
                "truncated": False,
                "error": None,
            }
            try:
                listing = await _schema_listing(
                    client, catalog, workspace_id)

                for namespace, schemas in listing:
                    prefix = f"{namespace}." if namespace else ""
                    entry["schemas_seen"] += len(schemas)

                    if schema_filter:
                        to_scan = [s for s in schemas
                                   if s.lower() == schema_filter.lower()]
                    else:
                        entry["schema_hits"].extend(
                            f"{prefix}{s}" for s in schemas
                            if s.lower() == wanted)
                        to_scan = schemas[:max_schemas_per_catalog]
                        if len(schemas) > max_schemas_per_catalog:
                            entry["truncated"] = True

                    for schema in to_scan:
                        entry["schemas_scanned"] += 1
                        try:
                            tables = await _browse(
                                client, catalog["catalog_id"], "tables",
                                workspace_id, catalog_name=namespace,
                                schema_name=schema)
                        except APIError as exc:
                            # An empty schema 404s ("No tables found in the
                            # schema ..."). That is not a reason to abandon the
                            # remaining schemas in this catalog.
                            if not _is_missing(exc):
                                raise
                            tables = []
                        for item in tables:
                            table = _table_name_of(item)
                            if table and table.lower() == wanted:
                                entry["table_hits"].append(
                                    f"{prefix}{schema}.{table}")
            except Exception as exc:  # noqa: BLE001 - one catalog must not
                # break the search across the others
                detail = str(exc)
                if "No Secrets found" in detail:
                    detail = "no credentials are linked to this catalog"
                entry["error"] = detail
                logger.info("metastore search failed on catalog %s: %s",
                            catalog["catalog_id"], exc)

            findings.append(entry)

    return findings
