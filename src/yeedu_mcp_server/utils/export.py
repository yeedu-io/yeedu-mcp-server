"""Fetching a single job or notebook export, and putting it where it belongs.

The export endpoints are declared `application/json` in the spec and are not.
`exportWorkspaceZip` in the backend branches on whether the payload carries
notebooks: with none it sets `text/plain` and streams the `.yeedu` JSON
document, and with any it streams a `zip` archive holding that document plus
the `.ipynb` files. So a job export arrives as text and a notebook export
almost always arrives as binary, from endpoints that look identical.

That distinction is the whole reason this module exists. Reading either one
through `client.get` would hand back `response.text` — fine for the job, and a
mojibake transcription of a zip for the notebook, written to disk as a corrupt
archive with no error anywhere. So the bytes are taken raw, and what to do with
them is decided from the content type the server actually sent.
"""

import os
from typing import Any, Dict, Optional, Tuple

from yeedu_mcp_server.client import YeeduClient


def _filename_from(disposition: str) -> Optional[str]:
    """The server's own filename, which carries the real extension."""
    for part in disposition.split(";"):
        part = part.strip()
        if part.startswith("filename"):
            return part.split("=", 1)[1].strip().strip('"') or None
    return None


async def fetch_export(
    endpoint: str, params: Dict[str, Any]
) -> Tuple[bytes, Optional[str], bool]:
    """GET an export endpoint, returning (content, server filename, is_text).

    Raises on an HTTP error so the calling tool can phrase the hint; the error
    body is included in the message.
    """
    sent = {k: v for k, v in params.items() if v is not None}
    async with YeeduClient() as client:
        response = await client._client.request(
            method="GET", url=endpoint, params=sent or None,
            headers=client._auth_headers(),
        )
    if response.status_code >= 400:
        try:
            detail: Any = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"{detail} (HTTP {response.status_code})")

    content_type = response.headers.get("content-type", "")
    is_text = not ("zip" in content_type or "octet-stream" in content_type)
    filename = _filename_from(response.headers.get("content-disposition", ""))
    return response.content, filename, is_text


def deliver(
    content: bytes,
    filename: Optional[str],
    is_text: bool,
    output_path: Optional[str],
    *,
    subject: str,
    fallback_name: str,
    inline_hint: str,
) -> str:
    """Write the export to disk, or return it inline when it is text.

    A path that names no extension, or ends in a separator, is treated as a
    directory and the server's filename appended — the caller cannot know in
    advance whether the result is a `.yeedu` document or a `.zip`.
    """
    if output_path:
        name = filename or fallback_name
        _, ext = os.path.splitext(output_path)
        save_path = (os.path.join(output_path, name)
                     if not ext or output_path.endswith(os.sep)
                     else output_path)
        with open(save_path, "wb") as handle:
            handle.write(content)
        return (f"Exported {subject}.\nFile saved to: {save_path}\n"
                f"Size: {len(content)} bytes")

    if not is_text:
        return (
            f"Error: the export of {subject} is a zip archive, which cannot be "
            "returned inline. HINT: call again with output_path set to a file or "
            "directory on the machine running this server."
        )

    return content.decode("utf-8", errors="replace") + f"\n\n{inline_hint}"
