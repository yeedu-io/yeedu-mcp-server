"""Server configuration — Yeedu-only settings stripped from forgeai-agents/server/config.py."""

import os
from typing import Optional, Union
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    """Server settings with environment variable binding."""

    model_config = SettingsConfigDict(
        env_file=".env"
    )

    # --- Yeedu API ---
    yeedu_api_base_url: Optional[str] = Field(
        default=None,
        description="Yeedu API base URL (required)",
    )
    yeedu_api_key: Optional[str] = Field(
        default=None,
        description="Yeedu Personal Access Token — read from YEEDU_API_KEY env var. "
                    "Never passed to any tool or exposed to the LLM.",
    )
    yeedu_api_timeout: float = Field(
        default=60.0,
        description="Yeedu API request timeout in seconds",
    )

    # --- Yeedu Connection Pool ---
    yeedu_pool_size: int = Field(
        default=10,
        description="Number of shared httpx.AsyncClient instances",
    )
    yeedu_pool_max_connections: int = Field(
        default=100,
        description="Max TCP connections per pool client",
    )
    yeedu_pool_max_keepalive: int = Field(
        default=50,
        description="Max keep-alive connections per pool client",
    )
    yeedu_pool_max_concurrent: int = Field(
        default=500,
        description="Max in-flight requests across all pool clients",
    )

    # --- SSL ---
    verify_ssl: bool = Field(
        default=False,
        description="Enable SSL certificate verification",
    )
    yeedu_ssl_cert_path: str = Field(
        default="",
        description="Path to SSL certificate file when VERIFY_SSL=true",
    )

    # --- Transport ---
    yeedu_mcp_transport: str = Field(
        default="stdio",
        description="MCP transport mode: stdio or streamable-http",
    )
    yeedu_mcp_server_url: str = Field(
        default="http://127.0.0.1:8000/mcp",
        description="Server URL for streamable-http transport",
    )

    # --- Logging ---
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )


settings = ServerSettings()


# ---------------------------------------------------------------------------
# SSL resolution
# ---------------------------------------------------------------------------

def resolve_ssl_verify() -> Union[bool, str]:
    """Resolve SSL verify value.

    Returns False when VERIFY_SSL is disabled.
    Returns the cert path string when VERIFY_SSL is enabled and path is set.
    Raises RuntimeError if VERIFY_SSL is enabled but YEEDU_SSL_CERT_PATH is
    missing or points to a non-existent file.
    """
    if not settings.verify_ssl:
        return False
    cert_path = settings.yeedu_ssl_cert_path
    if not cert_path:
        raise RuntimeError(
            "VERIFY_SSL is enabled but YEEDU_SSL_CERT_PATH is not set. "
            "Provide the path to the SSL certificate file."
        )
    if not os.path.isfile(cert_path):
        raise RuntimeError(
            f"SSL certificate file not found: {cert_path} "
            "(from YEEDU_SSL_CERT_PATH)"
        )
    return cert_path


SSL_VERIFY_YEEDU = resolve_ssl_verify()

# ---------------------------------------------------------------------------
# Flat module-level exports (convenience imports for client / tools)
# ---------------------------------------------------------------------------
YEEDU_API_BASE_URL = settings.yeedu_api_base_url
YEEDU_API_KEY = settings.yeedu_api_key
YEEDU_API_TIMEOUT = settings.yeedu_api_timeout
YEEDU_POOL_SIZE = settings.yeedu_pool_size
YEEDU_POOL_MAX_CONNECTIONS = settings.yeedu_pool_max_connections
YEEDU_POOL_MAX_KEEPALIVE = settings.yeedu_pool_max_keepalive
YEEDU_POOL_MAX_CONCURRENT = settings.yeedu_pool_max_concurrent
YEEDU_MCP_TRANSPORT = settings.yeedu_mcp_transport
YEEDU_MCP_SERVER_URL = settings.yeedu_mcp_server_url
LOG_LEVEL = settings.log_level
