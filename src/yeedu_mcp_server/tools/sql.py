"""Interactive SQL over the platform's HiveServer2 (Thrift) endpoint.

Spark SQL submitted as a job run returns its rows only through the run's
STDOUT, which lags the run reaching DONE by roughly half a minute, carries no
column names or types, and cannot distinguish an empty result from a log that
has not synced yet. The Thrift endpoint has none of those problems: it returns
a typed result set with column metadata, the platform starts the server on
first connect and releases it after an idle period, so there is no run to
start, poll or stop.

The endpoint's coordinates and the bearer token used to reach it stay inside
this module. They are never parameters, never returned, and are stripped from
any error text before it leaves here.
"""

import base64
import logging
import re
from typing import Optional

import anyio
from pydantic import Field

from yeedu_mcp_server.config import settings
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.confirm import confirmation_required, confirmed
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.utils.sql_targets import SqlTargetError, get_sql_target
from yeedu_mcp_server.utils.thrift_conn import (
    ThriftUnavailableError,
    build_thrift_url,
    get_connection_username,
    ssl_context_for,
)

logger = logging.getLogger(__name__)

#: Statements that only read. Anything else needs `confirm`.
_READ_ONLY = {"select", "with", "show", "describe", "desc", "explain"}

_COMMENT = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.S)


def _first_keyword(sql: str) -> str:
    stripped = _COMMENT.sub(" ", sql).strip()
    match = re.match(r"[a-zA-Z]+", stripped)
    return match.group(0).lower() if match else ""


def _is_multi_statement(sql: str) -> bool:
    """Whether anything follows the first `;` other than whitespace."""
    body = _COMMENT.sub(" ", sql)
    head, sep, tail = body.partition(";")
    return bool(sep) and bool(tail.strip())


#: Spark's wording for an object that is not in the session's catalog. The
#: cause is just as often that the cluster is bound to a different metastore
#: catalog than that the name is wrong, so the hint has to name the catalog.
_MISSING_OBJECT = re.compile(
    r"TABLE_OR_VIEW_NOT_FOUND|SCHEMA_NOT_FOUND|NoSuchDatabase|NoSuchTable"
    r"|Table or view not found|Database '.*' not found",
    re.I)


def _is_missing_object(exc: Exception) -> bool:
    return bool(_MISSING_OBJECT.search(str(exc)))


# Whether the Thrift route accepts a Bearer header. None until the first
# query settles it; see the negotiation comment in run_sql.
_bearer_supported: Optional[bool] = None


def _basic_required(exc: Exception) -> bool:
    """Whether the gateway refused Bearer and asked for the Basic envelope."""
    text = str(exc)
    return "Basic" in text and "required" in text


def _condense(detail: str, limit: int = 600) -> str:
    """Trim a Hive error down to its message.

    A failed Spark SQL statement comes back wrapped in a Thrift struct with the
    whole Java stack trace inline -- kilobytes of `at com.amazonaws...` frames
    that say nothing a caller can act on. Keep the part before the first frame
    and cap what is left; the untrimmed text still goes to the log.
    """
    for marker in ("\\n\\tat ", "\n\tat ", "\\n\tat ", "\n\\tat "):
        idx = detail.find(marker)
        if idx != -1:
            detail = detail[:idx]
            break
    detail = detail.strip()
    if len(detail) > limit:
        detail = detail[:limit].rstrip() + " [...]"
    return detail


def _redact(text: str, *secrets: Optional[str]) -> str:
    """Remove endpoint coordinates and token material from error text."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _execute(url, auth_header, sql, database, fetch_n, timeout_sec):
    """Blocking HiveServer2 round trip. Runs in a worker thread."""
    from pyhive import hive
    from thrift.transport.THttpClient import THttpClient

    context = ssl_context_for(url)
    transport = (
        THttpClient(url, ssl_context=context) if context else THttpClient(url)
    )
    transport.setCustomHeaders({"Authorization": auth_header})
    transport.setTimeout(timeout_sec * 1000)

    connection = hive.Connection(thrift_transport=transport, database=database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)

        if cursor.description is None:
            return [], [], False

        headers = [str(col[0]) for col in cursor.description]
        rows = cursor.fetchmany(fetch_n + 1)
        truncated = len(rows) > fetch_n
        return headers, rows[:fetch_n], truncated
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing must never mask a result
            pass


@mcp.tool(
    description=(
        "Runs a SQL query against a cluster's Thrift (HiveServer2) endpoint and "
        "returns the result set with column names. Use this to query tables "
        "rather than submitting a Spark SQL job — it returns typed rows "
        "immediately instead of unlabelled text in a run's STDOUT. The cluster "
        "must be RUNNING, have thrift support, be attached to the workspace, "
        "and be bound to the metastore catalog the tables live in; all four are "
        "checked before connecting and reported individually. When the "
        "cluster_id is not already known, call find_sql_clusters first and let "
        "the user choose — and find_metastore_for_object before that if they "
        "named a table without saying which metastore. Read-only statements "
        "run directly; anything that writes requires confirm=True."
    ),
    annotations={"readOnlyHint": False},
)
async def run_sql(
    workspace_id: int = Field(description="Workspace ID to run the query in"),
    cluster_id: int = Field(description="Cluster ID whose Thrift endpoint to query"),
    sql: str = Field(description="A single SQL statement to execute"),
    limit: int = Field(
        default=200, ge=1, le=1000,
        description="Maximum rows to return. The query itself is not rewritten.",
    ),
    database: str = Field(
        default="default", description="Schema to connect to"),
    timeout_sec: int = Field(
        default=120, ge=10, le=900,
        description="Seconds to wait for the query. The first connect takes "
                    "around 10s while the platform readies the cluster.",
    ),
    confirm: bool = Field(
        default=False,
        description="Required for any statement that is not a read.",
    ),
) -> str:
    """Execute SQL over Thrift and return the rows."""
    statement = (sql or "").strip()
    if not statement:
        return "Error: sql is empty. Hint: pass a single SQL statement."

    if _is_multi_statement(statement):
        return (
            "Error: run_sql takes a single statement, and more than one was "
            "given. Hint: split them and call run_sql once per statement."
        )

    keyword = _first_keyword(statement)
    if keyword not in _READ_ONLY and not confirmed(confirm):
        return confirmation_required(
            label="SQL statement",
            target=f"{keyword.upper() or 'statement'} on cluster {cluster_id}",
            tool="run_sql",
            action="execute",
            record=statement,
            impact=(
                f"`{keyword.upper()}` is not a read. It may change data, "
                f"schema or session state in workspace {workspace_id}. Read "
                f"statements (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN) run "
                f"without confirmation."
            ),
            retry=f"workspace_id={workspace_id}, cluster_id={cluster_id}, sql=...",
            include_action=False,
        )

    # Preflight the pair rather than discovering a problem as a socket error:
    # thrift support, workspace attachment and RUNNING each fail for a
    # different reason and each has a different fix.
    try:
        target = await get_sql_target(cluster_id, workspace_id)
    except SqlTargetError as exc:
        return f"Error: {exc}"

    conn = target["endpoint"]
    catalog = target["catalog_label"]
    url = build_thrift_url(conn)
    token = settings.yeedu_api_key

    # Bearer is the scheme every Yeedu auth type resolves to, so it is what we
    # try first. The Thrift route's OpenAPI spec currently declares a single
    # http/basic security scheme, so express-openapi-validator rejects a Bearer
    # header ("Authorization header with scheme 'Basic' required") before
    # Authentication.js -- which parses both schemes quite happily -- is ever
    # reached. Until that spec lists bearer as an alternative, fall back to the
    # Basic envelope, whose password is the very same token and not a second
    # credential. The outcome is remembered for the life of the process, so
    # only the first query pays for the rejected attempt; once the spec is
    # fixed, Bearer succeeds on the first try and the /user/info lookup that
    # the envelope needs for a username stops happening altogether.
    global _bearer_supported
    secrets = [url, token, conn.get("host")]

    async def _attempt(auth_header: str):
        return await anyio.to_thread.run_sync(
            lambda: _execute(
                url, auth_header, statement, database, limit, timeout_sec)
        )

    async def _attempt_basic():
        username = await get_connection_username()
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
        secrets.append(credentials)
        return await _attempt(f"Basic {credentials}")

    try:
        if _bearer_supported is False:
            headers, rows, truncated = await _attempt_basic()
        else:
            try:
                headers, rows, truncated = await _attempt(f"Bearer {token}")
                _bearer_supported = True
            except Exception as bearer_exc:  # noqa: BLE001
                if not _basic_required(bearer_exc):
                    raise
                _bearer_supported = False
                logger.info(
                    "Thrift route refused Bearer on cluster %s; using the Basic "
                    "envelope around the same token for the rest of this process.",
                    cluster_id)
                headers, rows, truncated = await _attempt_basic()
    except ThriftUnavailableError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001 - message must be sanitised
        detail = _condense(_redact(str(exc), *secrets))
        logger.warning("run_sql failed on cluster %s: %s: %s",
                       cluster_id, type(exc).__name__,
                       _redact(str(exc), *secrets))
        if _is_missing_object(exc):
            hint = (
                f"Hint: cluster {cluster_id} is bound to metastore catalog "
                f"{catalog}, and a schema or table in any other catalog is "
                f"invisible to this session — the name being wrong is only one "
                f"of the two explanations. Use "
                f"find_metastore_for_object(name=..., "
                f"workspace_id={workspace_id}) to find which catalog holds it, "
                f"then find_sql_clusters(metastore_catalog_id=...) to pick a "
                f"cluster bound to that catalog."
            )
        else:
            hint = (
                f"Hint: the cluster was RUNNING with a live endpoint when the "
                f"query was submitted, so check that the statement is valid "
                f"Spark SQL for metastore catalog {catalog}."
            )
        return (
            f"Error: the SQL query did not complete ({type(exc).__name__}): "
            f"{detail}\n{hint}"
        )

    if not headers:
        return f"Statement completed on cluster {cluster_id}. No result set returned."

    if not rows:
        return f"Query returned 0 rows.\n\n{serialize_table([], headers=headers)}"

    table = serialize_table(
        [["NULL" if value is None else str(value) for value in row]
         for row in rows],
        headers=headers,
    )

    if truncated:
        table += (
            f"\n\nShowing the first {limit} rows. Raise `limit` or add a LIMIT "
            f"clause to the query for a different slice."
        )
    return table
