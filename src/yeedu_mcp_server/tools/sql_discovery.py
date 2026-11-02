"""Working out *where* to run a SQL query before running it.

`run_sql` needs a (workspace, cluster) pair, and whether a pair can serve a
given table depends on facts a caller cannot see: thrift support, cluster
status, workspace attachment, and which metastore catalog the cluster is bound
to. These two tools surface those facts as a list of real options so the user
picks the target, instead of a cluster id being guessed and the query failing
with a misleading "table not found".
"""

import logging
from typing import Optional

from pydantic import Field

from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.utils.errors import APIError
from yeedu_mcp_server.utils.params import opt
from yeedu_mcp_server.utils.sql_targets import (
    SqlTargetError,
    catalog_label,
    cluster_workspace_ids,
    is_usable,
    list_catalogs,
    resolve_catalog,
    search_metastores,
    tenant_clusters,
    workspace_clusters,
)

logger = logging.getLogger(__name__)

#: Clusters to look up workspace attachments for when no workspace was given.
#: One API call each, so the answer stays bounded on a large tenant.
_ATTACHMENT_LOOKUP_CAP = 15


@mcp.tool(
    description=(
        "Lists the clusters that can serve SQL for a metastore catalog and/or a "
        "workspace, so the user can choose one. Give metastore_catalog_id or "
        "metastore_catalog_name when the user names a metastore, and "
        "workspace_id for the workspace they want to query from. Returns every "
        "thrift-capable cluster bucketed by readiness: READY (attached and "
        "RUNNING), START_REQUIRED (attached but stopped), ATTACH_REQUIRED "
        "(right metastore, not attached to that workspace). Present the list "
        "and ask the user which cluster_id to use — do not pick one for them. "
        "Clusters without thrift support are excluded and counted, because "
        "their Spark image publishes no JDBC endpoint."
    ),
    annotations={"readOnlyHint": True},
)
async def find_sql_clusters(
    metastore_catalog_id: Optional[int] = Field(
        default=None, description="Restrict to clusters bound to this metastore catalog"),
    metastore_catalog_name: Optional[str] = Field(
        default=None,
        description="Metastore catalog name, resolved to an id. An ambiguous "
                    "name is reported rather than guessed."),
    workspace_id: Optional[int] = Field(
        default=None,
        description="Workspace the query will run from. Supply it whenever it "
                    "is known — attachment is what decides whether a cluster "
                    "can serve SQL there at all."),
) -> str:
    """Report the clusters that can run SQL for a catalog and/or workspace."""
    try:
        catalog = None
        if metastore_catalog_id is not None or metastore_catalog_name:
            matches = await resolve_catalog(
                metastore_catalog_id, metastore_catalog_name)
            if not matches:
                known = await list_catalogs()
                listed = ", ".join(
                    f"{c['catalog_name']} (id {c['catalog_id']})" for c in known
                ) or "none"
                return (
                    f"Error: no metastore catalog matches "
                    f"{metastore_catalog_name or metastore_catalog_id}.\n"
                    f"Catalogs configured: {listed}.\n"
                    f"Hint: ask the user which of these they mean."
                )
            if len(matches) > 1:
                rows = [[c["catalog_id"], c["catalog_name"], c["catalog_type"]]
                        for c in matches]
                return (
                    f"More than one metastore catalog matches "
                    f"'{metastore_catalog_name}'. Ask the user which one, then "
                    f"call find_sql_clusters again with its "
                    f"metastore_catalog_id.\n\n"
                    + serialize_table(
                        rows, headers=["catalog_id", "catalog_name", "catalog_type"])
                )
            catalog = matches[0]

        # The workspace-scoped list is authoritative for attachment and is the
        # only one carrying the published endpoint; the tenant-wide list is what
        # reveals the clusters that are on the right metastore but not attached.
        try:
            attached = (await workspace_clusters(workspace_id)
                        if workspace_id is not None else [])
        except APIError as exc:
            if "not found" in str(exc).lower():
                return (
                    f"Error: workspace {workspace_id} does not exist, or this "
                    f"account cannot see it.\nHint: list_workspaces shows the "
                    f"ones available."
                )
            raise
        attached_ids = {c["cluster_id"] for c in attached}
        clusters = await tenant_clusters()

        wanted_catalog = catalog["catalog_id"] if catalog else None
        if wanted_catalog is not None:
            on_catalog = [c for c in clusters
                          if c["catalog_id"] == wanted_catalog]
            other_catalog_count = len(clusters) - len(on_catalog)
        else:
            on_catalog = clusters
            other_catalog_count = 0

        live = [c for c in on_catalog if is_usable(c["status"])]
        terminal_count = len(on_catalog) - len(live)

        capable = [c for c in live if c["thrift_support"]]
        no_thrift_count = len(live) - len(capable)

        if not capable:
            scope = (f" bound to metastore catalog {catalog_label(catalog)}"
                     if catalog else "")
            return (
                f"No usable cluster{scope} has thrift support, so none can "
                f"serve JDBC/Thrift SQL.\n"
                f"Checked {len(on_catalog)} cluster(s): {terminal_count} are "
                f"destroyed or errored, {no_thrift_count} of the rest lack "
                f"thrift support.\n"
                f"Hint: thrift support is a property of the cluster's Spark "
                f"image. Creating a cluster on a thrift-capable Spark version, "
                f"bound to this catalog, is the way forward — confirm with the "
                f"user before creating one."
            )

        # Attachment for unattached candidates, so an ATTACH_REQUIRED row can
        # say where the cluster is usable today.
        elsewhere = {}
        if workspace_id is None:
            for cluster in capable[:_ATTACHMENT_LOOKUP_CAP]:
                try:
                    elsewhere[cluster["cluster_id"]] = await cluster_workspace_ids(
                        cluster["cluster_id"])
                except Exception as exc:  # noqa: BLE001
                    logger.info("workspace lookup failed for cluster %s: %s",
                                cluster["cluster_id"], exc)

        rows = []
        buckets = {"READY": [], "START_REQUIRED": [], "ATTACH_REQUIRED": [],
                   "WORKSPACE_UNKNOWN": []}
        endpoint_by_id = {c["cluster_id"]: c.get("endpoint") for c in attached}

        for cluster in sorted(capable, key=lambda c: (c["status"] != "RUNNING",
                                                      c["cluster_id"])):
            cluster_id = cluster["cluster_id"]
            if workspace_id is None:
                readiness = "WORKSPACE_UNKNOWN"
                attachment = ",".join(
                    str(i) for i in elsewhere.get(cluster_id, [])) or "unknown"
            elif cluster_id not in attached_ids:
                readiness = "ATTACH_REQUIRED"
                attachment = "no"
            elif cluster["status"] != "RUNNING":
                readiness = "START_REQUIRED"
                attachment = "yes"
            elif not endpoint_by_id.get(cluster_id):
                readiness = "START_REQUIRED"
                attachment = "yes"
            else:
                readiness = "READY"
                attachment = "yes"

            buckets[readiness].append(cluster_id)
            rows.append([
                cluster_id,
                cluster["name"],
                cluster["status"],
                cluster["spark_version"],
                catalog_label(cluster),
                attachment,
                readiness,
            ])

        header = []
        if catalog:
            header.append(f"Metastore catalog: {catalog_label(catalog)}")
        header.append(
            f"Workspace: {workspace_id}" if workspace_id is not None
            else "Workspace: not specified — attachment not evaluated")

        table = serialize_table(
            rows,
            headers=["cluster_id", "name", "status", "spark_version",
                     "metastore", "attached" if workspace_id is not None
                     else "attached_workspaces", "readiness"],
        )

        notes = []
        if buckets["READY"]:
            notes.append(
                f"READY ({len(buckets['READY'])}): can run SQL now — "
                f"run_sql(workspace_id={workspace_id}, cluster_id=<choice>, sql=...)."
            )
        if buckets["START_REQUIRED"]:
            notes.append(
                f"START_REQUIRED ({len(buckets['START_REQUIRED'])}): attached "
                f"and thrift-capable but not RUNNING. Starting a cluster "
                f"consumes YCU — ask the user, then "
                f"cluster_action(cluster_id=<choice>, action='start')."
            )
        if buckets["ATTACH_REQUIRED"]:
            notes.append(
                f"ATTACH_REQUIRED ({len(buckets['ATTACH_REQUIRED'])}): on the "
                f"right metastore and thrift-capable, but not attached to "
                f"workspace {workspace_id}. Ask the user whether to attach one; "
                f"attaching grants the workspace access to the cluster, so it "
                f"is their decision, not yours. Then "
                f"manage_cluster_workspace_access(workspace_id={workspace_id}, "
                f"cluster_id=<choice>, action='create')."
            )
        if buckets["WORKSPACE_UNKNOWN"]:
            notes.append(
                f"WORKSPACE_UNKNOWN ({len(buckets['WORKSPACE_UNKNOWN'])}): no "
                f"workspace_id was given, so readiness is undetermined. The "
                f"attached_workspaces column lists where each is usable today"
                + (f" (first {_ATTACHMENT_LOOKUP_CAP} clusters only)"
                   if len(capable) > _ATTACHMENT_LOOKUP_CAP else "")
                + ". Ask the user which workspace they are querying from."
            )

        excluded = []
        if terminal_count:
            excluded.append(
                f"{terminal_count} cluster(s) are destroyed or errored and "
                f"cannot be started")
        if no_thrift_count:
            excluded.append(
                f"{no_thrift_count} cluster(s) have no thrift support")
        if other_catalog_count:
            excluded.append(
                f"{other_catalog_count} cluster(s) are bound to a different "
                f"metastore catalog")
        if excluded:
            notes.append("Excluded: " + "; ".join(excluded) + ".")

        notes.append(
            "Present these options to the user and ask which cluster_id to "
            "use. Do not choose on their behalf when more than one is READY, "
            "and do not start or attach a cluster without their answer."
        )

        return "\n".join(header) + "\n\n" + table + "\n\n" + "\n".join(notes)

    except SqlTargetError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("find_sql_clusters failed: %s", exc)
        return (
            f"Error finding SQL clusters: {exc}. Hint: verify the "
            f"workspace_id and metastore_catalog_id with list_workspaces and "
            f"list_metastore_catalogs."
        )


@mcp.tool(
    description=(
        "Finds which metastore catalog a schema or table lives in, for when the "
        "user names one without saying where it is. Accepts 'table', 'schema' "
        "or 'schema.table'. Searches every configured catalog through the "
        "metastore browse API — no cluster has to be running. Returns the "
        "catalogs that matched; when more than one did, ask the user which "
        "metastore they mean rather than assuming, then pass the chosen "
        "metastore_catalog_id to find_sql_clusters."
    ),
    annotations={"readOnlyHint": True},
)
async def find_metastore_for_object(
    name: str = Field(
        description="Schema, table, or 'schema.table' to locate"),
    workspace_id: Optional[int] = Field(
        default=None,
        description="Workspace to browse as. Optional — supply it only when a "
                    "catalog's credentials are linked at workspace scope. It "
                    "does not restrict which catalogs are searched."),
    max_schemas_per_catalog: int = Field(
        default=100, ge=1, le=1000,
        description="Cap on schemas scanned per catalog when matching a bare "
                    "table name. Raise it if the answer says a catalog was "
                    "truncated — a catalog cut short can hide a match and look "
                    "like a clean miss."),
) -> str:
    """Locate the metastore catalogs holding a named schema or table."""
    workspace_id = opt(workspace_id)
    max_schemas_per_catalog = opt(max_schemas_per_catalog) or 100

    target = (name or "").strip()
    if not target:
        return "Error: name is empty. Hint: pass a table, schema, or 'schema.table'."

    try:
        findings = await search_metastores(
            target, workspace_id, max_schemas_per_catalog)
    except Exception as exc:  # noqa: BLE001
        logger.warning("find_metastore_for_object failed: %s", exc)
        return (
            f"Error searching metastores: {exc}. Hint: check workspace_id "
            f"with list_workspaces."
        )

    if not findings:
        return (
            "No metastore catalogs are configured, so there is nothing to "
            "search. Hint: list_metastore_catalogs confirms this."
        )

    matched = [f for f in findings if f["schema_hits"] or f["table_hits"]]
    matched_ids = {f["catalog_id"] for f in matched}
    # Truncation matters most in a catalog that did NOT match: that is the one
    # whose miss may be an artefact of the cap rather than a real absence.
    truncated = [f for f in findings if f["truncated"]]
    empty = [f for f in findings
             if not f["error"] and not f["schemas_seen"]]
    # A catalog that produced a hit before erroring was searched in part; only
    # one that produced nothing at all was genuinely skipped.
    errors = [f for f in findings
              if f["error"] and f["catalog_id"] not in matched_ids]
    partial = [f for f in matched if f["error"]]

    rows = []
    for finding in matched:
        rows.append([
            finding["catalog_id"],
            finding["catalog_name"],
            finding["catalog_type"],
            "; ".join(finding["schema_hits"]) or "-",
            "; ".join(finding["table_hits"]) or "-",
        ])

    lines = [f"Searched {len(findings)} metastore catalog(s) for '{target}'."]

    if not matched:
        lines.append("")
        lines.append(f"No schema or table named '{target}' was found.")
        if truncated:
            lines.append(
                f"{len(truncated)} catalog(s) had more schemas than the scan "
                f"limit of {max_schemas_per_catalog}, so the match may have "
                f"been missed — retry with a higher max_schemas_per_catalog, "
                f"or with 'schema.table' to search one schema directly: "
                + "; ".join(
                    f"{f['catalog_name']} ({f['schemas_seen']} schemas)"
                    for f in truncated)
            )
        if empty:
            lines.append(
                f"{len(empty)} catalog(s) returned no schemas at all, so "
                f"nothing in them was searched: "
                + "; ".join(f["catalog_name"] for f in empty)
            )
        if errors:
            lines.append(
                f"{len(errors)} catalog(s) could not be browsed: "
                + "; ".join(
                    f"{f['catalog_name']} ({f['error']})" for f in errors))
        lines.append(
            "Hint: browse_catalog(object_type='schemas') on a specific "
            "catalog shows what is actually there."
        )
        return "\n".join(lines)

    lines.append("")
    lines.append(serialize_table(
        rows,
        headers=["catalog_id", "catalog_name", "catalog_type",
                 "matching_schemas", "matching_tables"]))
    lines.append("")

    if len(matched) > 1:
        lines.append(
            f"'{target}' exists in {len(matched)} metastore catalogs. Ask the "
            f"user which one they mean — the catalog decides which clusters "
            f"can see the data, and querying the wrong one returns different "
            f"rows, not an error."
        )
    else:
        only = matched[0]
        # workspace_id is optional here, so it must not be echoed into the
        # suggested call as the literal "None" — an agent would pass it back.
        ws_arg = (f", workspace_id={workspace_id}"
                  if workspace_id is not None else "")
        lines.append(
            f"Only metastore catalog {catalog_label(only)} holds it. Next: "
            f"find_sql_clusters(metastore_catalog_id={only['catalog_id']}"
            f"{ws_arg}) to see which clusters can query it, "
            f"then ask the user to pick one."
        )

    if errors:
        lines.append(
            f"Not searched: {len(errors)} catalog(s) could not be browsed ("
            + "; ".join(
                f"{f['catalog_name']}: {f['error']}" for f in errors)
            + "), so this list may be incomplete."
        )
    if partial:
        lines.append(
            f"Searched only in part: "
            + "; ".join(
                f"{f['catalog_name']} ({f['error']})" for f in partial)
            + ". Further matches may exist in those catalogs."
        )
    if truncated:
        lines.append(
            f"Hit the scan limit of {max_schemas_per_catalog} schemas, so "
            f"further matches may exist in: "
            + "; ".join(
                f"{f['catalog_name']} ({f['schemas_seen']} schemas)"
                for f in truncated)
            + ". Retry with a higher max_schemas_per_catalog to be sure."
        )
    if empty:
        lines.append(
            f"Returned no schemas, so nothing in them was searched: "
            + "; ".join(f["catalog_name"] for f in empty) + "."
        )

    return "\n".join(lines)
