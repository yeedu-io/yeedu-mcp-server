"""Thrift (HiveServer2) transport helpers.

The platform serves a HiveServer2 endpoint in HTTP transport mode for every
(cluster, workspace) pair whose cluster has thrift support. Its coordinates
arrive as a `thrift_connection` object, which `sql_targets.get_sql_target`
reads along with the rest of the preflight; the helpers here turn that record
into a URL, an SSL context and a username.

Connection coordinates and the bearer token are internal. They are used to open
a transport and are never returned to a caller, logged at INFO, or rendered into
tool output.
"""

import ssl
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.config import settings


class ThriftUnavailableError(Exception):
    """No Thrift endpoint is published for this cluster/workspace pair."""


def build_thrift_url(conn: Dict[str, Any]) -> str:
    """Assemble the HiveServer2 HTTP endpoint URL from a `thrift_connection`.

    `ssl` is taken from the API record, which is authoritative. It is only
    inferred from the configured API base URL when the API omits the field, so
    a deployment that terminates TLS differently for the two is still handled.
    """
    use_ssl = conn.get("ssl")
    if use_ssl is None:
        base = settings.yeedu_api_base_url or ""
        use_ssl = urlparse(base).scheme == "https"

    scheme = "https" if use_ssl else "http"
    host = conn.get("host")
    port = conn.get("port")
    path = conn.get("http_path") or ""

    authority = f"{host}:{port}" if port else str(host)
    return f"{scheme}://{authority}{path}"


def ssl_context_for(url: str) -> Optional[ssl.SSLContext]:
    """Return an SSL context honouring the server's verify_ssl setting."""
    if not url.startswith("https://"):
        return None
    if settings.verify_ssl:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


#: The account name is stable for the life of the process; fetch it once.
_username_cache: Optional[str] = None


async def get_connection_username() -> str:
    """Return the account name the Thrift gateway expects as the Basic user.

    The gateway authenticates with HTTP Basic — username plus the API token as
    the password — rather than a bearer header, matching what the JDBC clients
    in the UI are told to use.
    """
    global _username_cache
    if _username_cache:
        return _username_cache

    async with YeeduClient() as client:
        data = await client.get("/api/v1/user/info")

    username = (data or {}).get("username") or (data or {}).get("email")
    if not username:
        raise ThriftUnavailableError(
            "Could not determine the account name required to authenticate "
            "to the Thrift endpoint."
        )
    _username_cache = username
    return username
