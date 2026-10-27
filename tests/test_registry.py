"""Structural invariants across the whole tool registry.

Tools register by import side effect — `main` imports each module in
`tools/`, and the decorator does the rest — so a module that is written but
never imported is silently absent. These tests walk the registry FastMCP
actually built.
"""

import asyncio

import pytest

import yeedu_mcp_server.main  # noqa: F401  — imports every tool module
from yeedu_mcp_server.mcp_server import SYSTEM_PROMPT, mcp


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(mcp.list_tools())


@pytest.fixture(scope="module")
def by_name(tools):
    return {t.name: t for t in tools}


def test_every_tool_module_is_imported(tools):
    # One tool per domain module, so an unimported module fails here rather
    # than at the first call a user makes.
    expected = {
        "get_user_info",          # billing
        "list_clusters",          # cluster
        "git_status",             # git
        "list_metastore_catalogs",  # metastore
        "list_notebooks",         # notebook
        "get_job_types",          # reference
        "list_user_secrets",      # secrets
        "run_job",                # spark_job
        "list_storage_managers",  # storage
        "list_tenants",           # tenants
        "list_workspaces",        # workspace
        "browse_directory",       # access
        "manage_cloud_env",       # infra
    }
    assert expected <= {t.name for t in tools}


def test_tool_names_are_unique(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_every_tool_describes_itself(tools):
    undescribed = [t.name for t in tools if not (t.description or "").strip()]
    assert undescribed == []


def _params(tool):
    schema = tool.inputSchema or {}
    return schema.get("properties") or {}


#: Parameter names that would carry a secret value through the model's context.
#: Identifiers and names are fine — `token_id`, `secret_name`, `secret_type`
#: all reference a secret without being one, and `keytab` on a job
#: configuration is a path handed to `spark-submit --keytab`, not key
#: material (that lives in a HIVE KERBEROS secret).
_FORBIDDEN_PARAMS = {
    "value", "secret_value", "secret", "token", "token_value", "password",
    "passphrase", "api_key", "apikey", "access_key", "secret_key",
    "private_key", "service_account_key", "pat", "keytab_content",
    "keytab_bytes", "credential", "credentials", "client_secret",
}


def test_no_tool_accepts_a_secret_value(tools):
    offenders = [
        (t.name, p) for t in tools for p in _params(t)
        if p.lower() in _FORBIDDEN_PARAMS
    ]
    assert offenders == [], (
        "A secret passed as a tool parameter is produced by the model and sits "
        "in the transcript. Hand the operation to the user instead — see "
        "utils/handoff.py."
    )


def test_a_tool_that_demands_confirmation_exposes_a_confirm_parameter(tools):
    # The description is what the agent reads; the parameter is what enforces
    # it. A description promising a gate that does not exist is worse than no
    # gate, because the agent reports a refusal that never happened.
    missing = [
        t.name for t in tools
        if "confirm=True" in (t.description or "") and "confirm" not in _params(t)
    ]
    assert missing == []


def test_every_confirm_parameter_defaults_to_refusing(tools):
    wrong = [
        (t.name, _params(t)["confirm"].get("default"))
        for t in tools
        if "confirm" in _params(t)
        and _params(t)["confirm"].get("default") not in (False, None)
    ]
    assert wrong == []


def test_a_tool_with_a_confirm_parameter_explains_it(tools):
    # Otherwise an agent passes confirm=True on the first call to get past
    # what looks like a missing argument.
    silent = [
        t.name for t in tools
        if "confirm" in _params(t) and "confirm" not in (t.description or "")
    ]
    assert silent == []


def test_the_destructive_actions_are_gated(by_name):
    # Spot-check the tools whose gate was verified against the live platform.
    for name in ("manage_token", "manage_workspace", "manage_tenant",
                 "manage_secret", "manage_role_assignment",
                 "manage_workspace_access", "manage_cluster_mount",
                 "manage_cloud_env", "manage_metastore_catalog"):
        assert "confirm" in _params(by_name[name]), name


def test_system_prompt_states_both_policies():
    # The tools enforce these; the prompt is how the agent learns not to fight
    # them. The two have to stay in step.
    assert "## Confirmation Gate" in SYSTEM_PROMPT
    assert "never set it on your own reasoning" in SYSTEM_PROMPT
    assert "## Credential Handoff" in SYSTEM_PROMPT
    assert "designed behaviour" in SYSTEM_PROMPT


def test_system_prompt_keeps_the_original_guardrails():
    for clause in ("## ID Discipline", "YCU", "## Security Guardrails",
                   "Never expose host names"):
        assert clause in SYSTEM_PROMPT
