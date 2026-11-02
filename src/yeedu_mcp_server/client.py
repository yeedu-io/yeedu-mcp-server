"""Yeedu REST API Async HTTP Client and Connection Pool Manager.

Provides a thread-safe, high-performance connection pool (`YeeduConnectionPool`)
and context-managed async HTTP client (`YeeduClient`) for communicating with the
Yeedu RESTful API platform.

Authentication:
    `YeeduClient` securely reads `YEEDU_API_KEY` directly from system environment
    settings upon initialization, abstracting sensitive tokens entirely away from
    LLM invocation contexts and conversation parameters.
"""

import asyncio
import itertools
import json as json_module
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception

from yeedu_mcp_server.config import (
    YEEDU_API_BASE_URL,
    YEEDU_API_TIMEOUT,
    YEEDU_POOL_SIZE,
    YEEDU_POOL_MAX_CONNECTIONS,
    YEEDU_POOL_MAX_KEEPALIVE,
    YEEDU_POOL_MAX_CONCURRENT,
    SSL_VERIFY_YEEDU,
    settings,
)
from yeedu_mcp_server.utils.errors import APIError, AuthenticationError

logger = logging.getLogger(__name__)


def is_transient_error(exception: Exception) -> bool:
    """Check if exception is a transient network error or 5xx API response."""
    if isinstance(exception, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    if isinstance(exception, APIError) and exception.status_code in (502, 503, 504):
        return True
    return False


class YeeduConnectionPool:
    """Fixed-size pool of httpx.AsyncClient instances with round-robin selection."""

    def __init__(self):
        self._clients: List[httpx.AsyncClient] = []
        self._counter = itertools.count()
        self._initialized = False
        self._semaphore = asyncio.Semaphore(YEEDU_POOL_MAX_CONCURRENT)

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=YEEDU_API_BASE_URL.rstrip("/"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "yeedu-mcp-server/0.1.0",
            },
            timeout=YEEDU_API_TIMEOUT,
            verify=SSL_VERIFY_YEEDU,
            limits=httpx.Limits(
                max_connections=YEEDU_POOL_MAX_CONNECTIONS,
                max_keepalive_connections=YEEDU_POOL_MAX_KEEPALIVE,
            ),
        )

    async def initialize(self):
        """Called at MCP server startup — creates all shared clients."""
        self._clients = [self._build_client() for _ in range(YEEDU_POOL_SIZE)]
        self._initialized = True
        logger.info(
            "[YeeduConnectionPool] Initialized %d clients "
            "(max_connections=%d, max_keepalive=%d, max_concurrent=%d)",
            YEEDU_POOL_SIZE,
            YEEDU_POOL_MAX_CONNECTIONS,
            YEEDU_POOL_MAX_KEEPALIVE,
            YEEDU_POOL_MAX_CONCURRENT,
        )

    def get_client(self) -> httpx.AsyncClient:
        """Round-robin select a shared client."""
        if not self._initialized:
            raise RuntimeError(
                "YeeduConnectionPool not initialized. "
                "Ensure yeedu_pool.initialize() was called at server startup."
            )
        idx = next(self._counter) % YEEDU_POOL_SIZE
        return self._clients[idx]

    async def close_all(self):
        """Gracefully close all shared clients at shutdown."""
        logger.info("[YeeduConnectionPool] Shutting down %d clients", len(self._clients))
        for i, client in enumerate(self._clients, 1):
            try:
                await client.aclose()
                logger.debug("[YeeduConnectionPool] Closed client %d/%d", i, len(self._clients))
            except Exception as exc:
                logger.error(
                    "[YeeduConnectionPool] Error closing client %d/%d: %s",
                    i, len(self._clients), exc,
                )
        self._clients.clear()
        self._initialized = False
        logger.info("[YeeduConnectionPool] Shutdown complete")

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore


# Process-wide singleton — initialized in main.py before any tool executes
yeedu_pool = YeeduConnectionPool()


class YeeduClient:
    """HTTP client for Yeedu API operations.

    Usage:
        async with YeeduClient() as client:
            data = await client.get("/api/v1/clusters")

    The token is never a parameter. It is read from YEEDU_API_KEY at
    instantiation time and injected into every request header internally.
    """

    def __init__(self):
        # Read from env — the LLM never sees or generates this value
        self.token = settings.yeedu_api_key
        if not self.token:
            raise AuthenticationError(
                "YEEDU_API_KEY is not set. "
                "Set it in your environment or .env file before starting the server."
            )
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = yeedu_pool.get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._client = None

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @retry(
        retry=retry_if_exception(is_transient_error),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
    ) -> Any:
        """Make an authenticated HTTP request to the Yeedu API."""
        try:
            request_headers = self._auth_headers()
            if headers:
                request_headers.update(headers)

            # Encode parameters: list values are formatted as comma-separated, percent-encoded strings.
            filtered_params: Dict[str, Any] = {}
            list_query_parts: List[str] = []

            if params:
                for k, v in params.items():
                    if v is None:
                        continue
                    if isinstance(v, dict) or (
                        isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
                    ):
                        # Complex objects → JSON-encoded string
                        filtered_params[k] = json_module.dumps(v, separators=(",", ":"))
                    elif isinstance(v, list):
                        # List of scalars → comma-separated, percent-encoded string (e.g. k=val1%2Cval2)
                        encoded_val = urllib.parse.quote(
                            ",".join(str(i) for i in v), safe=""
                        )
                        list_query_parts.append(
                            f"{urllib.parse.quote(k, safe='')}={encoded_val}"
                        )
                    elif isinstance(v, str):
                        # Strings → percent-encoded so spaces → %20, not +
                        encoded_val = urllib.parse.quote(v, safe="")
                        list_query_parts.append(
                            f"{urllib.parse.quote(k, safe='')}={encoded_val}"
                        )
                    else:
                        filtered_params[k] = v

            # When pre-encoded parts exist, fold filtered_params into the URL too
            if list_query_parts:
                for k, v in filtered_params.items():
                    encoded_val = urllib.parse.quote(str(v), safe="")
                    list_query_parts.append(
                        f"{urllib.parse.quote(str(k), safe='')}={encoded_val}"
                    )
                filtered_params = {}
                sep = "&" if "?" in endpoint else "?"
                endpoint = f"{endpoint}{sep}{'&'.join(list_query_parts)}"

            request_kwargs: Dict[str, Any] = {
                "method": method,
                "url": endpoint,
                "params": filtered_params or None,
                "headers": request_headers,
            }

            if data is not None:
                request_kwargs["content"] = data
            elif json is not None:
                request_kwargs["json"] = json

            async with yeedu_pool.semaphore:
                response = await self._client.request(**request_kwargs)

            logger.debug(
                "[YeeduClient] %s %s → %s", method, endpoint, response.status_code
            )

            if response.status_code == 401:
                raise AuthenticationError(
                    f"Invalid YEEDU_API_KEY — API returned 401: {response.text}"
                )
            if response.status_code == 403:
                raise AuthenticationError(
                    f"YEEDU_API_KEY lacks required permissions — API returned 403: {response.text}"
                )

            if response.status_code >= 400:
                error_detail = ""
                try:
                    error_data = response.json()
                    if isinstance(error_data, dict):
                        error_detail = (
                            # The API answers validation failures with
                            # "message" (plus a per-field "errors" array).
                            # Omitting that key reported those errors as a
                            # stringified dict, burying the one sentence that
                            # says what to fix.
                            error_data.get("error")
                            or error_data.get("error_message")
                            or error_data.get("message")
                            or str(error_data)
                        )
                    else:
                        error_detail = str(error_data)
                except Exception:
                    error_detail = response.text
                raise APIError(
                    f"Yeedu API error: {error_detail} (HTTP {response.status_code})",
                    status_code=response.status_code,
                )

            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                if not response.content:
                    return ""
                return response.json()
            return response.text

        except httpx.TimeoutException:
            raise APIError(
                f"Request timeout contacting Yeedu API (limit: {YEEDU_API_TIMEOUT}s)"
            )
        except httpx.ConnectError as exc:
            raise APIError(f"Cannot connect to Yeedu API: {exc}")
        except (APIError, AuthenticationError):
            raise
        except Exception as exc:
            raise APIError(f"Unexpected error calling Yeedu API: {exc}")

    async def get(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None
    ) -> Any:
        return await self._request("GET", endpoint, params=params, headers=headers)

    @retry(
        retry=retry_if_exception(is_transient_error),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def get_raw(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> bytes:
        """GET request returning raw bytes (for zip/binary download endpoints)."""
        filtered_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            async with yeedu_pool.semaphore:
                response = await self._client.request(
                    method="GET",
                    url=endpoint,
                    params=filtered_params or None,
                    headers=self._auth_headers(),
                )
            if response.status_code >= 400:
                error_detail = response.text
                try:
                    error_data = response.json()
                    if isinstance(error_data, dict):
                        error_detail = (
                            # The API answers validation failures with
                            # "message" (plus a per-field "errors" array).
                            # Omitting that key reported those errors as a
                            # stringified dict, burying the one sentence that
                            # says what to fix.
                            error_data.get("error")
                            or error_data.get("error_message")
                            or error_data.get("message")
                            or str(error_data)
                        )
                except Exception:
                    pass
                raise APIError(
                    f"Yeedu API error: {error_detail} (HTTP {response.status_code})",
                    status_code=response.status_code,
                )
            return response.content
        except httpx.HTTPError as exc:
            raise APIError(f"Network error: {exc}")

    async def post(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[str] = None,
    ) -> Any:
        return await self._request(
            "POST", endpoint, params=params, json=json, headers=headers, data=data
        )

    async def put(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return await self._request("PUT", endpoint, params=params, json=json)

    async def delete(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return await self._request("DELETE", endpoint, params=params, json=json)
