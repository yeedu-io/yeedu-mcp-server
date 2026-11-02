"""Git operations and repository management tools for Yeedu MCP Server."""


import json
import re
from typing import List, Optional, Union
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.params import opt
from yeedu_mcp_server.utils.listing import is_empty_listing


def _parse_file_paths(fp: Optional[Union[List[str], str]]) -> Optional[List[str]]:
    """Parse file_paths parameter from list, string, JSON string, or comma-separated string."""
    if fp is None:
        return None
    if isinstance(fp, str):
        fp = fp.strip()
        if not fp:
            return None
        try:
            parsed = json.loads(fp)
            if isinstance(parsed, list):
                result = []
                for item in parsed:
                    if isinstance(item, str):
                        item = item.strip().strip('"').strip("'")
                    result.append(str(item))
                return result if result else None
            elif isinstance(parsed, str):
                s = parsed.strip().strip('"').strip("'")
                return [s] if s else None
        except Exception:
            pass
        res = [s.strip().strip('"').strip("'") for s in fp.split(",") if s.strip()]
        return res if res else None
    elif isinstance(fp, list):
        res = [str(item).strip().strip('"').strip("'") for item in fp if item]
        return res if res else None
    return None


#: The providers `express-openapi-validator` accepts for a clone, from the
#: `CloneGitRepoToWorkspace` enum. It is shorter than the list the platform
#: supports credentials for, and a value outside it fails the whole request.
_GIT_PROVIDERS = {
    "GitHub",
    "BitBucket Cloud",
    "GitLab",
    "Azure DevOps Services",
}


def _workspace_path(path: str) -> str:
    """Normalise a workspace path to what the clone endpoint accepts.

    `browse_workspace_files` reports paths as `file:///files/...`, and the
    backend strips exactly that prefix before requiring what is left to be
    absolute (`resolvePath` in `utils/Workspace.js`). The workspace root is
    therefore the one path that cannot be given the way it is displayed:
    `file:///files` strips to the empty string, which is not absolute, so the
    platform answers "The specified path,  is invalid" with the path blank --
    an error that names nothing for the caller to correct.
    """
    path = (path or "").strip()
    for prefix in ("file:///files", "/files"):
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
    return path or "/"


@mcp.tool(
    description=(
        "Clones a Git repository (HTTPS or HTTP URL) into a workspace folder. "
        "git_url, git_provider and git_branch are all required by the platform. "
        "git_provider must be one of GitHub, BitBucket Cloud, GitLab or "
        "Azure DevOps Services. Give the parent location as file_id or "
        "file_path, not both; the workspace root is '/'. Private repos use the "
        "user's stored Git credential for that provider — add one with "
        "manage_git_credential if the clone is rejected for authentication. "
        "The clone runs in the background: this returns the new folder's "
        "workspace_file_id, which git_clone_status then polls: job_status goes "
        "INITIATED -> CLONE_STARTED -> COMPLETED, or FAILED."
    ),
    annotations={"idempotentHint": True}
)
async def git_clone_repo(
    workspace_id: int = Field(description="Workspace ID to clone into"),
    git_url: str = Field(description="Git repository URL (HTTPS or HTTP) to clone"),
    git_provider: str = Field(
        description="Git provider: GitHub, BitBucket Cloud, GitLab or Azure DevOps Services"),
    git_branch: str = Field(
        description="Branch to clone, e.g. main or master. The platform has no "
                    "default and rejects the request if this is missing"),
    git_folder_name: Optional[str] = Field(
        default=None, description="Folder name for cloned repo (defaults to repo name)"),
    file_id: Optional[int] = Field(
        default=None, description="Parent folder file ID where repo will be cloned"),
    file_path: Optional[str] = Field(
        default=None,
        description="Parent folder path where the repo will be cloned, as "
                    "browse_workspace_files reports it. The workspace root is '/'. "
                    "Give this or file_id, not both"),
) -> str:
    """Clone a Git repository into the workspace."""
    try:
        git_provider, git_branch = opt(git_provider), opt(git_branch)
        file_path = opt(file_path)

        if git_provider not in _GIT_PROVIDERS:
            return (
                f"Error: git_provider {git_provider!r} is not one of "
                f"{', '.join(sorted(_GIT_PROVIDERS))}. HINT: the platform "
                "validates this against a fixed list and rejects the whole "
                "request otherwise; list_git_credentials shows the providers "
                "this user already has a credential for."
            )
        if not git_branch:
            return (
                "Error: git_branch is required. HINT: the platform has no "
                "default branch for a clone and rejects the request without "
                "one — pass the branch you want, e.g. 'main' or 'master'."
            )
        if file_id is not None and file_path:
            return (
                "Error: give file_id or file_path, not both. HINT: the platform "
                "rejects a request carrying both."
            )

        params = {"workspace_id": workspace_id}
        if file_id is not None:
            params["file_id"] = file_id
        elif file_path:
            params["file_path"] = _workspace_path(file_path)
        else:
            params["file_path"] = "/"

        payload = {
            "git_url": git_url,
            "git_provider": git_provider,
            "git_branch": git_branch,
        }
        if git_folder_name:
            payload["git_folder_name"] = git_folder_name

        async with YeeduClient() as client:
            data = await client.post(
                "/api/v1/workspace/files/git/clone-repo",
                params=params,
                json=payload,
            )

            if isinstance(data, dict) and ("error_message" in data or "error" in data):
                return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Check the repository URL, git_provider, and credential ID."

            lines = ["field,value"]
            lines.append(f"workspace_id,{workspace_id}")
            lines.append(f"git_url,{git_url}")
            if git_provider:
                lines.append(f"git_provider,{git_provider}")
            lines.append(f"branch,{git_branch}")
            if isinstance(data, dict):
                for k, v in data.items():
                    if k not in ("workspace_id", "git_url", "git_provider") and v is not None:
                        lines.append(f"{k},{v}")
            else:
                lines.append("status,Clone initiated")
            return "\n".join(lines)

    except Exception as e:
        return f"Error cloning repo: {str(e)}. HINT: Ensure the git_url is valid, workspace_id exists, and you have correct permission/credentials."


@mcp.tool(
    description="Lists, creates, or switches Git branches in a workspace Git folder (identified by file_id or file_path). action=list returns CSV: branch_name, is_current. action=create or action=switch requires branch_name and returns CSV: action, branch_name, status.",
    annotations={"idempotentHint": True}
)
async def git_branches(
    workspace_id: int = Field(description="Workspace ID"),
    action: str = Field(description="Action: list, create, or switch"),
    file_id: Optional[int] = Field(
        default=None, description="File ID of the Git folder (must be GIT_FOLDER type)"),
    file_path: Optional[str] = Field(
        default=None, description="Full file path to the Git folder in workspace"),
    branch_name: Optional[str] = Field(
        default=None, description="Branch name (required for create and switch)"),
) -> str:
    """List, create, or switch Git branches. Identify folder via file_id or file_path."""
    try:
        valid_actions = ["list", "create", "switch"]
        if action not in valid_actions:
            return f"Error: Invalid action '{action}'. Must be: list, create, or switch"

        if file_id is None and not file_path:
            return "Error: Either file_id or file_path must be provided to identify the Git folder."

        if action in ("create", "switch") and not branch_name:
            return f"Error: branch_name is required for '{action}' action."

        params = {"workspace_id": workspace_id}
        if file_id is not None:
            params["file_id"] = file_id
        if file_path:
            params["file_path"] = file_path

        async with YeeduClient() as client:
            if action == "list":
                data = await client.get(
                    "/api/v1/workspace/files/git/list-branches", params=params
                )

                if isinstance(data, dict) and ("error_message" in data or "error" in data):
                    return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Verify the file_id/file_path is a GIT_FOLDER and workspace_id is correct."

                if isinstance(data, dict) and "data" in data:
                    branches = data["data"]
                elif isinstance(data, list):
                    branches = data
                else:
                    return f"Branches: {data}"

                lines = ["branch_name,is_current"]
                for b in branches:
                    if isinstance(b, dict):
                        lines.append(
                            f"{b.get('name', b.get('branch_name', 'N/A'))},{b.get('is_current', b.get('current', 'N/A'))}")
                    else:
                        lines.append(f"{b},N/A")
                return "\n".join(lines)

            elif action == "create":
                params["branch_name"] = branch_name

                data = await client.post(
                    "/api/v1/workspace/files/git/create-branch", params=params
                )

                if isinstance(data, dict) and ("error_message" in data or "error" in data):
                    return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Check if the branch already exists."

                lines = ["field,value"]
                lines.append("action,create")
                lines.append(f"branch_name,{branch_name}")
                lines.append("status,Branch created successfully")
                return "\n".join(lines)

            else:
                params["branch_name"] = branch_name

                data = await client.post(
                    "/api/v1/workspace/files/git/switch-branch", params=params
                )

                if isinstance(data, dict) and ("error_message" in data or "error" in data):
                    return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Check if the branch exists locally or remotely."

                lines = ["field,value"]
                lines.append("action,switch")
                lines.append(f"branch_name,{branch_name}")
                lines.append("status,Switched successfully")
                return "\n".join(lines)

    except Exception as e:
        return f"Error git branches: {str(e)}. HINT: Check workspace_id, file_id/file_path, action, and branch_name values."


@mcp.tool(
    description="Gets the Git status of a workspace repository folder (identified by file_id or file_path), showing the current branch and all file changes. Returns CSV with each row categorized as: modified, staged, or untracked.",
    annotations={"readOnlyHint": True}
)
async def git_status(
    workspace_id: int = Field(description="Workspace ID"),
    file_id: Optional[int] = Field(
        default=None, description="File ID of the Git folder (GIT_FOLDER type)"),
    file_path: Optional[str] = Field(
        default=None, description="Full file path to the Git folder in workspace"),
) -> str:
    """Get git status showing branch, modified, staged, and untracked files."""
    try:
        has_file_id = isinstance(file_id, int)
        has_file_path = isinstance(file_path, str) and bool(file_path)

        if not has_file_id and not has_file_path:
            return "Error: Either file_id or file_path must be provided to identify the Git folder."

        params = {"workspace_id": workspace_id}
        if has_file_id:
            params["file_id"] = file_id
        if has_file_path:
            params["file_path"] = file_path

        async with YeeduClient() as client:
            data = await client.get(
                "/api/v1/workspace/files/git/status", params=params
            )

            if isinstance(data, dict) and ("error_message" in data or "error" in data):
                return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Verify file_id/file_path references a valid Git repository folder."

            if isinstance(data, dict):
                status_obj = data.get("status", data) if isinstance(data.get("status"), dict) else data
                
                lines = []
                branch = status_obj.get("current") or status_obj.get("branch")
                if branch:
                    lines.append(f"branch,{branch}")
                tracking = status_obj.get("tracking")
                if tracking:
                    lines.append(f"tracking,{tracking}")
                head_commit = status_obj.get("head_commit_hash")
                if head_commit:
                    lines.append(f"head_commit_hash,{head_commit}")
                repo_url = status_obj.get("repo_url")
                if repo_url:
                    lines.append(f"repo_url,{repo_url}")

                if lines:
                    lines.append("")

                lines.append("category,file_path")

                for f in status_obj.get("modified", status_obj.get("changes", status_obj.get("not_staged", []))):
                    path = f.get("path", f.get("file", f)) if isinstance(f, dict) else f
                    lines.append(f"modified,{path}")
                for f in status_obj.get("staged", status_obj.get("staged_changes", [])):
                    path = f.get("path", f.get("file", f)) if isinstance(f, dict) else f
                    lines.append(f"staged,{path}")
                for f in status_obj.get("not_added", status_obj.get("untracked", [])):
                    path = f.get("path", f) if isinstance(f, dict) else f
                    lines.append(f"untracked,{path}")
                for f in status_obj.get("created", []):
                    path = f.get("path", f) if isinstance(f, dict) else f
                    lines.append(f"created,{path}")
                for f in status_obj.get("deleted", []):
                    path = f.get("path", f) if isinstance(f, dict) else f
                    lines.append(f"deleted,{path}")
                for f in status_obj.get("renamed", []):
                    path = f.get("path", f) if isinstance(f, dict) else f
                    lines.append(f"renamed,{path}")
                for f in status_obj.get("conflicted", []):
                    path = f.get("path", f) if isinstance(f, dict) else f
                    lines.append(f"conflicted,{path}")

                return "\n".join(lines)

            return str(data)

    except Exception as e:
        return f"Error getting git status: {str(e)}. HINT: Double-check that workspace_id and file_id/file_path are valid."


@mcp.tool(
    description="Stages files in a workspace Git folder for the next commit. Requires file_id of the Git folder (find it with browse_workspace_files). Use file_paths to stage specific files, or all_files=true to stage all changes. Returns CSV: action, file_id, files_staged, status.",
    annotations={"idempotentHint": True}
)
async def git_stage_files(
    workspace_id: int = Field(description="Workspace ID"),
    file_id: int = Field(
        description="File ID of the Git folder (GIT_FOLDER type)"),
    file_paths: Optional[Union[List[str], str]] = Field(
        default=None, description="List of file paths to stage. If not provided, all changes are staged."),
    all_files: bool = Field(
        default=False, description="Stage all files in the git folder"),
) -> str:
    """Stage files for the next git commit. Use file_paths for specific files or all_files=true for all."""
    try:
        parsed_file_paths = _parse_file_paths(file_paths)

        params = {"workspace_id": workspace_id, "file_id": file_id}
        if parsed_file_paths:
            params["file_paths"] = parsed_file_paths
        else:
            params["all"] = "true"

        async with YeeduClient() as client:
            data = await client.post(
                "/api/v1/workspace/files/git/stage", params=params
            )

            if isinstance(data, dict) and ("error_message" in data or "error" in data):
                return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Verify the file paths exist and are modified/untracked."

            lines = ["field,value"]
            lines.append("action,stage")
            lines.append(f"file_id,{file_id}")
            if parsed_file_paths:
                lines.append(f"files_staged,{len(parsed_file_paths)}")
            else:
                lines.append("files_staged,all")
            lines.append("status,Files staged successfully")
            return "\n".join(lines)

    except Exception as e:
        return f"Error staging files: {str(e)}. HINT: Verify the file paths exist in status and are not ignored."


@mcp.tool(
    description="Gets the diff (changed lines) for a specific file in a workspace Git folder. Requires workspace_id, file_id of the Git folder (find it with browse_workspace_files), and file_path within the repo. Returns raw diff text.",
    annotations={"readOnlyHint": True}
)
async def git_file_diff(
    workspace_id: int = Field(description="Workspace ID"),
    file_id: int = Field(
        description="File ID of the Git folder (GIT_FOLDER type)"),
    file_path: str = Field(
        description="File path within the git folder to get diff for"),
) -> str:
    """Get the diff of a file showing what has changed."""
    try:
        params = {
            "workspace_id": workspace_id,
            "file_id": file_id,
            "file_path": file_path,
        }

        async with YeeduClient() as client:
            data = await client.get(
                "/api/v1/workspace/files/git/file-diff", params=params
            )

            if isinstance(data, dict) and ("error_message" in data or "error" in data):
                return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Check if file_path is relative to the git repository root."

            if isinstance(data, dict) and "diff" in data:
                return data["diff"]
            if isinstance(data, str):
                return data
            return str(data)

    except Exception as e:
        return f"Error getting file diff: {str(e)}. HINT: Verify the file_path is valid and modified."


@mcp.tool(
    description="Commits staged changes and pushes to the remote Git repository. Requires file_id of the Git folder (find it with browse_workspace_files) and a commit_message. Optionally specify description, file_paths, or all_files=true. Returns CSV: action, commit_message, commit_hash (if returned by API), branch, status.",
    annotations={"idempotentHint": True}
)
async def git_commit_push(
    workspace_id: int = Field(description="Workspace ID"),
    commit_message: str = Field(description="Commit message"),
    file_id: int = Field(
        description="File ID of the Git folder (GIT_FOLDER type)"),
    description: Optional[str] = Field(
        default=None, description="Optional description for the commit"),
    file_paths: Optional[Union[List[str], str]] = Field(
        default=None, description="Specific file paths to commit. If not set, commits all staged."),
    all_files: bool = Field(
        default=False, description="Commit and push all files"),
) -> str:
    """Commit staged changes and push to remote. Requires commit_message."""
    try:
        parsed_file_paths = _parse_file_paths(file_paths)

        params = {
            "workspace_id": workspace_id,
            "file_id": file_id,
            "commit_message": commit_message,
        }
        if isinstance(description, str) and description:
            params["description"] = description

        if parsed_file_paths:
            params["file_paths"] = parsed_file_paths
        else:
            params["all"] = "true"

        async with YeeduClient() as client:
            data = await client.post(
                "/api/v1/workspace/files/git/commit-push", params=params
            )

            if isinstance(data, dict) and ("error_message" in data or "error" in data):
                return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Ensure you have files staged or set all_files=True."

            lines = ["field,value"]
            lines.append("action,commit-push")
            lines.append(f"commit_message,{commit_message}")
            if isinstance(data, dict):
                if "commit_hash" in data:
                    lines.append(f"commit_hash,{data['commit_hash']}")
                if "branch" in data:
                    lines.append(f"branch,{data['branch']}")
            lines.append("status,Committed and pushed successfully")
            return "\n".join(lines)

    except Exception as e:
        return f"Error commit-push: {str(e)}. HINT: Check if credentials are set up or merge conflicts exist."


@mcp.tool(
    description="Syncs a workspace Git folder with the remote: action=pull fetches and merges the latest remote changes (Git folder identified by file_id or file_path); action=discard reverts local uncommitted changes (all files, or specific file_paths) and requires file_id. Returns CSV: action, file_id/file_path, status."
)
async def git_sync(
    workspace_id: int = Field(description="Workspace ID"),
    action: str = Field(
        description="Action: pull (get remote changes) or discard (revert local changes)"),
    file_id: Optional[int] = Field(
        default=None, description="File ID of the Git folder (GIT_FOLDER type)"),
    file_path: Optional[str] = Field(
        default=None, description="Full file path to the Git folder in workspace"),
    file_paths: Optional[Union[List[str], str]] = Field(
        default=None, description="File paths to discard (for discard action)"),
    all_files: bool = Field(
        default=False, description="Discard all changes (for discard action)"),
) -> str:
    """Pull remote changes or discard local modifications."""
    try:
        valid_actions = ["pull", "discard"]
        if action not in valid_actions:
            return f"Error: Invalid action '{action}'. Must be: pull or discard"

        has_file_id = isinstance(file_id, int)
        has_file_path = isinstance(file_path, str) and bool(file_path)

        if not has_file_id and not has_file_path:
            return "Error: Either file_id or file_path must be provided to identify the Git folder."
        # The discard API only identifies the Git folder by file_id
        if action == "discard" and not has_file_id:
            return "Error: file_id is required for action=discard. HINT: Find the Git folder's file_id with browse_workspace_files."

        parsed_file_paths = _parse_file_paths(file_paths)

        params = {"workspace_id": workspace_id}
        if has_file_id:
            params["file_id"] = file_id
        if has_file_path and action == "pull":
            params["file_path"] = file_path

        if action == "discard":
            if parsed_file_paths:
                params["file_paths"] = parsed_file_paths
            else:
                params["all"] = "true"

        async with YeeduClient() as client:
            if action == "pull":
                data = await client.post(
                    "/api/v1/workspace/files/git/pull", params=params
                )
            else:
                data = await client.post(
                    "/api/v1/workspace/files/git/discard", params=params
                )

            if isinstance(data, dict) and ("error_message" in data or "error" in data):
                return f"Error: {data.get('error_message', data.get('error', 'Unknown'))}. HINT: Check for uncommitted changes or merge conflicts if pulling."

            lines = ["field,value"]
            lines.append(f"action,{action}")
            lines.append(f"file_id,{file_id or file_path}")
            if isinstance(data, dict):
                for k, v in data.items():
                    if k not in ("workspace_id", "file_id", "file_path") and v is not None:
                        lines.append(f"{k},{v}")
            lines.append(
                f"status,{action.capitalize()} completed successfully")
            return "\n".join(lines)

    except Exception as e:
        return f"Error git {action}: {str(e)}. HINT: Check for local modifications, conflicts, or network issues."




# ---------------------------------------------------------------------------
# Reading an asynchronous clone back
#
# git_clone_repo submits the clone and returns immediately, so its success
# response only means the request was accepted. A clone that then fails leaves
# an empty or half-populated GIT_FOLDER behind, and every later call against
# that folder -- branches, status, diff -- fails for a reason that has nothing
# to do with the call being made. These three reads are the only way to tell
# the two apart.
# ---------------------------------------------------------------------------

_CLONE_ACTIONS = ("status", "log", "errors")


def _clone_target(
    workspace_id: Optional[int],
    workspace_name: Optional[str],
    file_id: Optional[int],
    file_path: Optional[str],
) -> dict:
    """The query params that identify a Git folder, by either id or name/path."""
    params: dict = {}
    if workspace_id is not None:
        params["workspace_id"] = workspace_id
    elif workspace_name:
        params["workspace_name"] = workspace_name
    if file_id is not None:
        params["file_id"] = file_id
    elif file_path:
        params["file_path"] = file_path
    return params


#: The server-side mount the clone runs against:
#: `${YEEDU_SYSTEM_NFS_LOCAL_MOUNT_DIR}/yeedu/files/<tenant uuid>/<workspace id>`
#: (`cloneGitRepoToWorkspace` in `services/GitService.js`). git writes that
#: absolute path into its own progress output, so the clone log hands back the
#: mount point and the tenant uuid unless they are rewritten.
_MOUNT_PATH = re.compile(
    r"\S*/yeedu/files/[0-9a-fA-F-]{36}/\d+(?=[/'\"\s]|$)")


def _scrub_log(text: str) -> str:
    """Rewrite host filesystem paths in a clone log as workspace paths.

    The log is git's own output, so it is worth returning verbatim apart from
    this: a failed clone explains itself there and nowhere else. But it opens
    with `Cloning into '/nfs/yeedu/files/<tenant>/<workspace>/<folder>'...`,
    which is the mount layout and the tenant id -- infrastructure detail
    SYSTEM_PROMPT forbids handing back, and of no use to a caller who
    addresses the same folder as `file:///files/<folder>`.
    """
    return _MOUNT_PATH.sub("file:///files", text or "")


@mcp.tool(
    description=(
        "Reads back an asynchronous Git clone started by git_clone_repo. "
        "action='status' returns job_status, created_by, start_time and end_time; "
        "action='log' returns the clone's stdout or stderr (log_type); "
        "action='errors' returns the workflow errors recorded against it. "
        "git_clone_repo only submits the clone, so its response means 'accepted', "
        "not 'finished' — this is the only way to learn whether it succeeded, and "
        "a failed clone leaves an empty or partial folder whose later branch, "
        "status and diff calls then fail for an unrelated-looking reason. "
        "Identify the Git folder by file_id or file_path, inside workspace_id or "
        "workspace_name."
    ),
    annotations={"readOnlyHint": True},
)
async def git_clone_status(
    workspace_id: Optional[int] = Field(
        default=None, description="Workspace ID holding the Git folder"),
    workspace_name: Optional[str] = Field(
        default=None, description="Workspace name, if workspace_id is not given"),
    action: str = Field(
        default="status", description="Action: status, log, or errors"),
    file_id: Optional[int] = Field(
        default=None, description="File ID of the Git folder (GIT_FOLDER type)"),
    file_path: Optional[str] = Field(
        default=None, description="Full path of the Git folder in the workspace"),
    log_type: str = Field(
        default="stdout",
        description="Which log to read when action='log': stdout or stderr"),
    last_n_lines: Optional[int] = Field(
        default=None, ge=1,
        description="Return only the last N lines when action='log'"),
) -> str:
    """Read the status, log or errors of a submitted Git clone."""
    action = (action or "status").strip().lower()
    if action not in _CLONE_ACTIONS:
        return (
            f"Error: Invalid action '{action}'. Hint: use one of "
            f"{', '.join(_CLONE_ACTIONS)}."
        )
    if workspace_id is None and not workspace_name:
        return "Error: Either workspace_id or workspace_name is required."
    if file_id is None and not file_path:
        return (
            "Error: Either file_id or file_path is required to identify the Git "
            "folder. Hint: browse_workspace_files lists the folders in a "
            "workspace with their file ids."
        )

    params = _clone_target(workspace_id, workspace_name, file_id, file_path)
    target = file_path or f"file_id {file_id}"

    try:
        async with YeeduClient() as client:
            if action == "log":
                log = (log_type or "stdout").strip().lower()
                if log not in ("stdout", "stderr"):
                    return (
                        f"Error: Invalid log_type '{log_type}'. "
                        "Hint: use stdout or stderr."
                    )
                if last_n_lines is not None:
                    params["last_n_lines"] = last_n_lines
                raw = await client.get_raw(
                    f"/api/v1/workspace/files/git/clone/log/{log}", params=params)
                text = raw.decode("utf-8", errors="replace").strip() if raw else ""

                # The endpoint is declared text/plain for every variant, and is
                # not: asked for the last N lines it sets application/json and
                # wraps the log as {"last_n_lines": "..."} (the branch in
                # `getGitCloneJobLogs`, services/GitService.js). Returned
                # unwrapped, an empty log reads back as the literal text
                # `{"last_n_lines":""}` -- which looks like a log line.
                if last_n_lines is not None and text.startswith("{"):
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict) and "last_n_lines" in payload:
                        text = str(payload["last_n_lines"] or "").strip()

                text = _scrub_log(text)
                if not text:
                    return (
                        f"No {log} output recorded for the clone of {target}. "
                        "The query ran and the log is empty; this is an empty "
                        f"result, not a failed call. git writes its progress to "
                        "stderr, so a successful clone usually has no stdout at "
                        "all — try log_type='stderr'. An empty stderr too means "
                        "the clone may not have started yet; check "
                        "git_clone_status(action='status')."
                    )
                return text

            if action == "errors":
                data = await client.get(
                    "/api/v1/workspace/files/git/clone/workflow/errors",
                    params=params)
                rows = data.get("data") if isinstance(data, dict) else data
                if not rows:
                    return (
                        f"No errors recorded against the clone of {target}. The "
                        "query ran and returned 0 rows; this is an empty result, "
                        "not a failed call."
                    )
                lines = ["error"]
                lines += [str(r.get("error", r) if isinstance(r, dict) else r)
                          for r in rows]
                return "\n".join(lines)

            data = await client.get(
                "/api/v1/workspace/files/git/clone/status", params=params)
            record = data[0] if isinstance(data, list) and data else data
            if not isinstance(record, dict) or not record:
                return f"No clone job found for {target}."
            lines = ["field,value"]
            for key in ("job_status", "created_by", "start_time", "end_time"):
                if key in record:
                    lines.append(f"{key},{record[key]}")
            for key, value in record.items():
                if key not in ("job_status", "created_by", "start_time", "end_time"):
                    lines.append(f"{key},{value}")
            return "\n".join(lines)

    except Exception as e:
        # A clone with nothing wrong with it has no workflow errors, and the
        # API answers that with 404 "No workflow errors were found ...". Left
        # to raise, the one call that asks "did this clone fail?" answers with
        # an error of its own, and the hint sends the caller to re-check a
        # file_id that was right -- a false alarm about a clean clone, and the
        # inverse of the mistake worth guarding against.
        if action == "errors" and is_empty_listing(e):
            return (
                f"No errors recorded against the clone of {target}. The query "
                "ran and returned 0 rows; this is an empty result, not a "
                "failed call. Read job_status with "
                "git_clone_status(action='status') to see whether it finished."
            )
        return (
            f"Error reading clone {action} for {target}: {str(e)}. HINT: Confirm "
            "the file_id or file_path points at a GIT_FOLDER created by "
            "git_clone_repo — browse_workspace_files lists them."
        )
