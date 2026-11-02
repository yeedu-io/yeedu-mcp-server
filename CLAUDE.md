# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server that exposes the Yeedu data platform (Spark clusters, jobs, notebooks, workspaces, metastores, billing) to AI agents. Published to PyPI as `yeedu-mcp-server` and normally run by users via `uvx yeedu-mcp-server`. Python >= 3.12, dependencies managed with `uv`.

## Commands

```bash
uv sync                                                # install dependencies
uv run mcp dev src/yeedu_mcp_server/main.py            # MCP Inspector GUI for manual tool testing
uv run --directory /abs/path/to/repo yeedu-mcp-server  # run from source (point your MCP client here while developing)
uvx yeedu-mcp-server --transport streamable-http       # HTTP mode instead of the default stdio
```

`YEEDU_API_KEY` and `YEEDU_API_BASE_URL` are required; the base URL must be the API host and port (typically `:8080`), not the UI — pointing at the UI yields HTML and 404s. The full environment variable table is in README.md. Logs rotate at `~/.yeedu-mcp/logs/yeedu-mcp-server.log`.

### Verifying a change

```bash
uv run --group dev pytest                               # the suite; needs no API key and reaches nothing
uv run python -m compileall -q src/yeedu_mcp_server/    # syntax only

uv run python -c "
import yeedu_mcp_server.main, asyncio
from yeedu_mcp_server.mcp_server import mcp
print(len(asyncio.run(mcp.list_tools())), 'tools')"
```

The registration check is the one `compileall` cannot replace: it catches a missing import in `main.py`, a malformed `@mcp.tool` decorator, or a signature FastMCP cannot build a schema from. `tests/test_registry.py` runs it as an assertion, along with the structural invariants — every destructive tool exposes a `confirm` parameter defaulting to false, no tool accepts a parameter that would be a secret value, `SYSTEM_PROMPT` still carries both policy sections.

`tests/test_record_shapes.py` is the regression net for the class of defect described under *Response shapes* below. Each case is a shape captured from the live platform; the assertion is that no cell is `N/A` where the response had data. **When you correct a field mapping, add the fixture here** — that is the only way the fix survives, since the wrong mapping does not fail, it renders `N/A`.

What the suite cannot cover: anything that needs the platform. URL shapes, HTTP status behaviour and request field names are still only verified by a live call through a connected client.

### Iterating against a live MCP client

A connected client runs the code as it was at connect time; editing source changes nothing until it reconnects. After every change the user must reconnect the server in their client (in Claude Code: `/mcp` → Reconnect `yeedu`) before new or changed tools are callable. Batch edits accordingly — each round trip costs a user action.

## Architecture

**Registration is by import side effect.** `mcp_server.py` constructs the single `FastMCP` instance named `mcp`; every tool module does `from yeedu_mcp_server.mcp_server import mcp` and attaches itself with decorators at import time. `main.py` then imports each `tools/*` module purely for that side effect. **A new tool module that is not imported in `main.py` does not exist at runtime.**

**`mcp_server.py` also holds `SYSTEM_PROMPT`**, the behavioural contract shipped to every client: ID discipline, costs in YCU only, confirm destructive operations, never reveal tokens or infrastructure details. Changing how agents behave toward this platform usually means editing that prompt, not tool code.

**`main.py` startup order matters**: logging is configured before any other import, then `load_dotenv()`, then `config`, then tool modules, then the transport. The httpx connection pool is opened and closed by a FastMCP lifespan hook in `mcp_server.py` rather than by `main.py`, so it works under `mcp dev` too.

**`client.py` is the only place credentials live.** `YeeduClient` is an async context manager over `yeedu_pool`, a shared set of `httpx.AsyncClient`s guarded by a global semaphore, with `tenacity` retries on transient errors. It injects `Authorization: Bearer <token>` from config. The token is never a tool parameter and never appears in a tool schema, so it stays out of the LLM's context. `get`/`post`/`put`/`delete` return parsed JSON; `get_raw` returns bytes for binary and zip endpoints.

**`tools/<domain>.py` is one module per platform domain.** Every tool is an `async def` under `@mcp.tool(description=...)`, takes Pydantic `Field` parameters, and returns a **string** — a table built with `utils/formatting.serialize_table`, or indented JSON.

**Write coverage is organised as one `manage_*` tool per resource**, with an `action` parameter rather than a tool per verb — that keeps the tool count down and puts every verb for a resource under one description. The chain is: infrastructure (`tools/infra.py`: credential → network → boot disk → volume → cloud env → cluster conf → cluster workspace access → cluster mount), workspaces (`tools/workspace.py`: `manage_workspace`, `import_workspace`, `manage_workspace_file`), access (`tools/access.py`: `manage_workspace_access`, `browse_directory`, `manage_role_assignment`), metastore (`tools/metastore.py`: `manage_metastore_catalog`, `manage_catalog_secret_link`), secrets (`tools/secrets.py`: `manage_secret`, `manage_git_credential`, `manage_token`), storage (`tools/storage.py`: `manage_osm_files`) and tenants (`tools/tenants.py`: `manage_tenant`).

**What cannot be created at all:** users, groups, roles and workspace permissions are synced from the identity provider or defined by the platform, so the ceiling is *assigning* them (`manage_role_assignment`, `manage_workspace_access`). Yeedu has no workspace DELETE either — `manage_workspace(action='disable')` is the reversible equivalent.

**Errors are returned, not raised.** The convention is `return f"Error: {e}. HINT: <concrete next step>"`. `SYSTEM_PROMPT` instructs agents to read the hint and act on it before retrying, so a hint must be actionable and name the tool to call next. Exception classes in `utils/errors.py` are for internal signalling, not for escaping a tool.

## Conventions and platform gotchas

- **IDs are never interchangeable**: `cluster_id` (a cluster instance) vs `job_id` (a job definition) vs `run_id` (one execution); notebooks add `notebook_id` (the config) vs `run_id` (one session). State which one a parameter takes in its description — agents substitute them otherwise.
- **MCP clients deserialize JSON-looking string arguments** into dicts or lists before a tool sees them. Any parameter accepting file content must be typed `Optional[Any]` and re-serialize with `json.dumps` (see `upload_workspace_file`). Byte-size headers such as `x-file-size` must count UTF-8 bytes, not characters.
- **Yeedu splits notebook endpoints by run type.** `GET notebook/{id}/run/{run}/download` and `POST notebook/{id}/update` serve **background/batch** runs only and return HTTP 400 for interactive runs. Interactive runs must read and write the notebook through the workspace file endpoints (`/workspace/file/download`, `POST /workspace/files` with `overwrite=true`); see `_save_notebook_outputs` in `tools/notebook.py`. Cell outputs use Yeedu's own shape, `{"output_type": "text", "Celloutput": "..."}`, not standard Jupyter output objects.
- **Notebook code execution runs over the Jupyter kernel websocket**: register the proxy with `GET .../kernel/ws?yeedu_session=<token>`, then connect to `wss://.../kernel/ws/yeedu_session/<token>` and exchange Jupyter 5.3 messages until `status` reports `execution_state: idle`. The session token sits in the URL path, so scrub it out of any error string before returning it.
- **A new notebook run is briefly `SUBMITTED`** before it reaches `RUNNING`; starting its kernel during that window returns HTTP 409 even though the run was created successfully.

### Response shapes: read them from the backend, never guess

`../RESTful-API/query/*.js` builds every response with explicit `json_build_object` projections. That SQL **is** the API contract, and it is the only place to settle a field name. Two cheaper-looking sources both mislead:

- **The CLI is no help.** It prints the raw JSON it is handed, so its source names only *request* parameters. `--groupname` is a flag; the field in a group record is `group_name`. Reading `groupname` off a response yields `N/A` on every row.
- **A plausible name is usually wrong.** Field mapping fails silently — a wrong key renders `N/A`, which looks like empty data rather than a bug, so it survives any amount of offline testing and only shows up against a live platform.

What the projections establish, each of which was mapped wrongly first:

| Record | Shape |
|---|---|
| Group | `group_id`, `group_name`, `group_mail`, `group_object_id`, `group_type` — not `groupname`/`email` |
| Role held by a principal | nested per source: `user_roles[]` granted directly, `group_roles[]` inherited from a group. One principal yields several rows; inherited rows carry `group_name` |
| Role id | `roles_id` in the per-principal reads, `role_id` in the tenant-wide ones. Both spellings are live |
| Who holds a role / workspace access / group membership | one object for the *thing*, holders nested under `users` or `groups`. Absent key = nobody, which is empty, not a record (`_holders` in `tools/access.py`) |
| API token | `token_id`, `description`, `created_by{username}`, `expiry_time` — there is **no** created date, and `expiry_time` is the string `infinity` for a non-expiring token |
| Catalog↔secret link | secrets nested under `metastore_secrets_{scope}` / `object_storage_secrets_{scope}`, keyed `secrets_{scope}_id`, null when unset — a *different* shape from the flat `{kind}_secrets_{scope}_id` the write side takes |
| Workspace | `workspace_id`, `name`, `job_count`, `notebook_count` from `/workspaces`. There is no `GET /workspace`, and `/workspace/stats` returns **job-run status counts**, not a workspace |

- **An empty collection is HTTP 404**, not an empty array, with a body like `No mounts found.` A listing path is fixed and correct, so a 404 there means "none exist" and must render as an empty result — surfaced as an error, it has the agent retrying a call that already worked. Handled centrally in `_crud` in `tools/infra.py`.
- **Free-text fields contain commas and newlines.** Descriptions are user-entered and really do arrive with both, which splits a CSV record across lines and shifts every later column. `_csv_cell` in `tools/secrets.py` collapses whitespace and quotes.

## Credentials and secrets are handed back to the user

**No tool ever takes a credential or secret value as a parameter.** A tool argument is produced by the model, so the value would pass through the model's context and the client transcript on its way to the platform — the one thing `client.py` is structured to avoid for the API token. Instead the tool returns the exact `yeedu` CLI command plus the UI screen, and the user performs that single step themselves; everything around it (list, get, rename, delete, reference by id) stays on MCP.

`utils/handoff.py` builds that message (`secret_handoff`). `ui_link` only emits a URL when the operator sets the optional `YEEDU_UI_BASE_URL`, which is deliberately separate from the API URL — the server knows the API host, not the UI host, and `SYSTEM_PROMPT` forbids leaking either. With no base URL set it prints the bare path fragment, which is enough for the user to recognise the screen.

Operations that must be handed off, with the v2.10.x CLI command and UI path (screen URLs are all relative to `/tenant/<tenant-id>/`):

| Operation | CLI | UI (as labelled) |
|---|---|---|
| Credential configuration | `yeedu resource create-credential-conf --name --credential_type_id --base64_encoded_credentials` | user button (top right) → Settings → ADMIN → Credentials → + Credential · `settings/create-credential-config` · Admin / Platform Admin only |
| User secret | `yeedu secret create-user-secret --name --secret_type <type> <type-specific flags>` | left rail → Secrets → User tab → + Secret · `secrets/create-user-secret` · no role gate; visible only to its creator |
| Workspace secret | `yeedu secret create-workspace-secret --name --secret_type <type> --workspace_id <id> <type-specific flags>` | left rail → Secrets → Workspace tab → + Secret (or Workspaces → \<ws\> → Secrets tab) · `secrets/create-workspace-secret` · MANAGE to create/delete, EDIT to edit |
| Tenant secret | `yeedu secret create-tenant-secret --name --secret_type <type> <type-specific flags>` | left rail → Secrets → Tenant tab → + Secret · `secrets/create-tenant-secret` · tenant Admin / Platform Admin |
| Git credentials | `yeedu git-credentials create --git_provider --username --token` | user button → Settings → USER → Linked Accounts · `settings/linked-accounts` · every role; only GitHub enabled in 2.10.1 |
| API token | `yeedu token create --description --timeout=<24h\|2 days\|infinity>` | user button → Settings → USER → Tokens → + Token · `settings/tokens` · every role; the value is shown once and the token carries its creator's roles |

**Rotation is handed off for the same reason as creation**, and is a distinct `action` so the message names the right command: `manage_secret(action='rotate')` prints `yeedu secret edit-<scope>-secret` with the type's value flags, and `manage_git_credential(action='rotate')` prints `yeedu git-credentials edit --token`. The `edit` action on both tools therefore accepts only the non-secret fields — name and description for a secret, username for a git credential — and refuses with a pointer to `rotate` when asked for more. A secret `edit` must also carry `secret_type`: the platform requires the type on every update, even when only the name changes.

Secret-type-specific flags, from the CLI's `SECRET_TYPE_ARGS`: `HIVE KERBEROS` → `--principal --keytab_file_path`; `HIVE BASIC` → `--username --password`; `DATABRICKS UNITY TOKEN` → `--token`; `ENVIRONMENT VARIABLE` → `--value`; `AWS ACCESS SECRET KEY PAIR` → `--aws_access_key_id --aws_secret_access_key --aws_default_region`; `AZURE SERVICE PRINCIPAL` → `--tenant_id --subscription_id --client_id --client_secret`; `GOOGLE SERVICE ACCOUNT` → `--type --project_id --private_key_id --private_key --client_email --client_id --auth_uri --token_uri --auth_provider_x509_cert_url --client_x509_cert_url` (+ optional `--access_key --secret_key --universe_domain`).

What does **not** need a handoff: linking a catalog to a secret takes secret **ids**, not values, so all twelve catalog↔secret link operations belong on MCP. Metastore catalog create likewise takes no secret — Hive takes XML/krb5 **file paths** and Unity takes `--endpoint --default_catalog --storage_path`; the PAT arrives later as a linked secret. Hive file paths are read on the machine running this server, not on the cluster, so the tool checks `os.path.isfile` and says which argument the missing path came from.

## Destructive operations are gated twice

`SYSTEM_PROMPT` carries a **Confirmation Gate** and a **Credential Handoff** section stating both policies to the client directly, so an agent reads a refusal or a handoff as the designed outcome rather than a tool failure to work around. Keep those in step with the code below.

`utils/confirm.py` turns that gate from advice into a mechanism. Every delete, revoke, unlink, unassign and disable takes a `confirm` parameter and **refuses before opening a connection**, returning `confirmation_required(...)`: the record read back from the platform, the impact in one sentence, and the exact call to repeat with `confirm=True`. The user is then asked twice — once by the agent relaying that text, once by the agent having to re-issue the call. Refusing before the client opens means an unreachable API cannot turn a refusal into an error the agent retries its way past; the read-back is best-effort and wrapped in its own `try`.

`confirmed(value)` is deliberately strict and never `bool(value)`: MCP clients coerce argument types loosely, and `bool("false")` is `True`, which would turn a refusal into a deletion. Only `True`, `"true"/"yes"/"y"/"1"/"on"` and the integer `1` count.

`utils/params.py` exists for the offline check. FastMCP fills unset arguments from their `Field` defaults, but a direct Python call (`await manage_secret(...)`, how a tool is verified without a live platform) delivers the bare `FieldInfo`, which is truthy and not `None`, so every `if x is None` guard silently inverts. `opt(value)` maps a bare `FieldInfo` to `None`; management tools normalise their optional parameters through it on entry, so a tool behaves identically whether called over MCP or straight from Python. `tools/infra.py` has the same logic as its local `_provided`.

## Publishing

`.github/workflows/push-to-pypi.yml` publishes to PyPI; bump `version` in `pyproject.toml`. Note that a client configured with `uvx yeedu-mcp-server` runs the **published** build, not this checkout — if the same server name is registered in two scopes (for example local and user), the narrower scope silently shadows the other. Register only one while developing.
