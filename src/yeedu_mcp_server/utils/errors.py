"""Custom exception hierarchy. Verbatim from forgeai-agents/server/utils/errors.py."""


class MCPError(Exception):
    """Base exception for MCP server errors."""
    pass


class AuthenticationError(MCPError):
    """Invalid or missing credentials."""
    pass


class APIError(MCPError):
    """API call failed."""

    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


class ValidationError(MCPError):
    """Invalid parameters provided."""
    pass


class ResourceNotFoundError(APIError):
    """Requested resource was not found."""

    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, 404)
