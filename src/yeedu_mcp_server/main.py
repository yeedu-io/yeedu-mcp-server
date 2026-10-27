"""Yeedu MCP Server Main Entry Point.

Initialization and lifecycle execution workflow:
  1. Configure application logger and output format
  2. Load environment settings from system / .env file
  3. Validate required credentials (YEEDU_API_KEY, YEEDU_API_BASE_URL)
  4. Register tool, prompt, and resource modules with FastMCP
  5. Execute server transport (stdio or streamable-http)

Transport priority: CLI argument (--transport) -> YEEDU_MCP_TRANSPORT env var -> stdio (default)
"""

import argparse
import sys
from pathlib import Path

# Logging must be configured before any other module imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from yeedu_mcp_server.logging_setup import configure_logging

logger, log_path = configure_logging(service_name="yeedu-mcp-server")
logger.info("Yeedu MCP Server logging configured at: %s", log_path)

import anyio
from dotenv import load_dotenv

load_dotenv()

# Config is imported after dotenv so env vars are available
from yeedu_mcp_server.config import settings
from yeedu_mcp_server.mcp_server import mcp

# Import tool & prompt modules — each registers decorators at import time
import yeedu_mcp_server.tools.billing  # noqa: F401
import yeedu_mcp_server.tools.cluster       # noqa: F401
import yeedu_mcp_server.tools.workspace     # noqa: F401
import yeedu_mcp_server.tools.spark_job     # noqa: F401
import yeedu_mcp_server.tools.metastore     # noqa: F401
import yeedu_mcp_server.tools.git           # noqa: F401
import yeedu_mcp_server.tools.storage       # noqa: F401
import yeedu_mcp_server.tools.secrets       # noqa: F401
import yeedu_mcp_server.tools.tenants       # noqa: F401
import yeedu_mcp_server.tools.reference     # noqa: F401
import yeedu_mcp_server.prompts             # noqa: F401



def _validate_env() -> None:
    """Assert required environment variables are set. Exit with a clear message if not."""
    missing = []
    if not settings.yeedu_api_key:
        missing.append("YEEDU_API_KEY")
    if not settings.yeedu_api_base_url:
        missing.append("YEEDU_API_BASE_URL")
    if missing:
        for var in missing:
            logger.error("Required environment variable not set: %s", var)
        sys.exit(
            f"Error: Required environment variable(s) not set: {', '.join(missing)}.\n"
            f"Copy .env.example to .env and fill in the values."
        )
    logger.info(
        "Environment validated. API base URL: %s", settings.yeedu_api_base_url
    )


def _get_transport() -> str:
    """Resolve transport type: CLI arg → env var → default stdio."""
    parser = argparse.ArgumentParser(
        description="Yeedu MCP Server — Yeedu data platform via Model Context Protocol"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        help="Transport mode: 'stdio' for Claude Desktop, 'streamable-http' for remote",
    )
    args, _ = parser.parse_known_args()
    transport = args.transport or settings.yeedu_mcp_transport or "stdio"
    if transport not in ("stdio", "streamable-http"):
        logger.warning("Unknown transport '%s', defaulting to stdio", transport)
        transport = "stdio"
    return transport


async def _run_server(transport: str) -> None:
    """Run MCP server on the configured transport.
    Pool init/shutdown is handled by FastMCP's lifespan hook in mcp_server.py.
    """
    logger.info("Starting Yeedu MCP server with '%s' transport", transport)
    if transport == "stdio":
        await mcp.run_stdio_async()
    else:
        await mcp.run_streamable_http_async()


def main() -> None:
    _validate_env()
    transport = _get_transport()
    try:
        anyio.run(_run_server, transport)
    except Exception as exc:
        logger.error("Fatal error running Yeedu MCP server: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
