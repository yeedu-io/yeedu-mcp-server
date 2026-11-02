"""Regression tests for the response shapes the platform actually returns.

Every case here is a defect that was found against the live platform and could
not have been found offline. A row helper that reads a key the response does
not have renders "N/A" instead of raising, so the tool runs, returns a
plausible-looking table, and is wrong. The fixtures below are trimmed from real
responses; the assertions are that no cell is N/A where the data was present.

The authoritative source for a shape is the backend's `json_build_object`
projection in `RESTful-API/query/*.js`, not the CLI, whose flags name request
parameters only (`--groupname` is a flag; the response field is `group_name`).
"""

from yeedu_mcp_server.tools.access import (
    _access_row,
    _directory_row,
    _holders,
    _role_rows,
)
from yeedu_mcp_server.tools.metastore import _linked_secret, _link_table
from yeedu_mcp_server.tools.notebook import _widget_rows
from yeedu_mcp_server.tools.secrets import _csv_cell, _token_table
from yeedu_mcp_server.tools.git import _scrub_log, _workspace_path
from yeedu_mcp_server.tools.spark_job import _app_info_rows, _function_path
from yeedu_mcp_server.tools.workspace import _candidate_rows
from yeedu_mcp_server.utils.run_metrics import _normalise


def _cells(row):
    return [str(c) for c in row]


# ---------------------------------------------------------------------------
# 1. Group directory records are named group_name / group_mail / group_type.
#    `groupname` and `email` are request parameters and never come back.
# ---------------------------------------------------------------------------
def test_directory_row_reads_group_fields():
    record = {
        "group_id": 1809,
        "group_name": "yeedu-demo-admins",
        "group_mail": "demo-admins@example.com",
        "group_type": "AZURE_AD",
    }
    assert _directory_row(record, "group") == [
        1809, "yeedu-demo-admins", "demo-admins@example.com", "AZURE_AD",
    ]


def test_directory_row_still_reads_user_fields():
    record = {
        "user_id": 21,
        "username": "person@example.com",
        "email": "person@example.com",
    }
    row = _directory_row(record, "user")
    assert row[:3] == [21, "person@example.com", "person@example.com"]


# ---------------------------------------------------------------------------
# 2. A principal's roles arrive nested, one list per source. Reading a flat
#    role_id off the record yields nothing, and a user with both direct and
#    inherited roles is several rows, not one.
# ---------------------------------------------------------------------------
def test_role_rows_expands_nested_user_and_group_roles():
    record = {
        "user_id": 21,
        "username": "person@example.com",
        "user_roles": [{"roles_id": 3, "user_role": "Platform Admin"}],
        "group_roles": [
            {"roles_id": 2, "group_role": "Admin", "group_name": "demo-admins"}
        ],
    }
    rows = _role_rows(record, "user")
    assert rows == [
        [21, "person@example.com", 3, "Platform Admin"],
        [21, "person@example.com", 2, "Admin (via group demo-admins)"],
    ]


def test_role_rows_marks_inherited_role_without_a_group_name():
    record = {
        "user_id": 7,
        "username": "other@example.com",
        "group_roles": [{"roles_id": 0, "group_role": "User"}],
    }
    assert _role_rows(record, "user")[0][3] == "User (via a group)"


def test_role_rows_renders_a_principal_holding_no_role():
    record = {"user_id": 9, "username": "newcomer@example.com"}
    assert _role_rows(record, "user") == [
        [9, "newcomer@example.com", "—", "—"]
    ]


def test_role_rows_uses_the_named_role_for_a_by_role_listing():
    # by_role answers with bare principals under a role the caller named, so
    # the role is not in the record and has to be carried in.
    record = {"user_id": 21, "username": "person@example.com"}
    assert _role_rows(record, "user", fallback_role=3)[0][2] == 3


def test_role_rows_accepts_the_tenant_wide_role_id_spelling():
    # The per-principal reads key a role `roles_id`; the tenant-wide ones
    # key it `role_id`.
    record = {
        "group_id": 1809,
        "group_name": "demo-admins",
        "roles": [{"role_id": 2, "role_name": "Admin"}],
    }
    assert _role_rows(record, "group") == [[1809, "demo-admins", 2, "Admin"]]


# ---------------------------------------------------------------------------
# 3. Workspace access holders nest under users / groups inside one workspace
#    object. Taking the outer object as a row yields one row of blanks.
# ---------------------------------------------------------------------------
_WORKSPACE_WITH_HOLDERS = {
    "data": [
        {
            "workspace_id": 348,
            "name": "mcp",
            "users": [
                {
                    "user_id": 21,
                    "username": "person@example.com",
                    "permission": {"permission_id": 1, "name": "MANAGE"},
                    "created_by": {"user_id": 21, "username": "person@example.com"},
                }
            ],
        }
    ]
}


def test_holders_unwraps_principals_nested_in_a_workspace():
    holders = _holders(_WORKSPACE_WITH_HOLDERS, "users", "user_id")
    assert [h["user_id"] for h in holders] == [21]


def test_access_row_reads_nested_permission_and_granted_by():
    holder = _holders(_WORKSPACE_WITH_HOLDERS, "users", "user_id")[0]
    assert _access_row(holder, "user") == [
        21, "person@example.com", 1, "MANAGE", "person@example.com",
    ]


def test_holders_reports_an_empty_container_as_no_rows():
    # A workspace nobody else can reach simply omits the nested list. That is
    # an empty answer, not a record — the outer object must not become a row.
    assert _holders({"data": [{"workspace_id": 348, "name": "mcp"}]},
                    "users", "user_id") == []


def test_holders_passes_through_an_already_flat_listing():
    flat = {"data": [{"user_id": 21, "username": "person@example.com"}]}
    assert _holders(flat, "users", "user_id") == flat["data"]


# ---------------------------------------------------------------------------
# 4. Tokens have no creation date, and their descriptions are free text.
# ---------------------------------------------------------------------------
def test_token_table_columns_match_what_the_platform_returns():
    data = {
        "data": [
            {
                "token_id": 7156,
                "description": "mcp",
                "created_by": {"user_id": 21, "username": "person@example.com"},
                "expiry_time": "infinity",
            }
        ]
    }
    table = _token_table(data)
    assert table.splitlines()[0] == "token_id,description,created_by,expiry"
    assert table.splitlines()[1] == "7156,mcp,person@example.com,never"


def test_token_table_quotes_a_description_containing_a_comma():
    data = {
        "data": [
            {
                "token_id": 1,
                "description": "ci runner, nightly\nbuilds",
                "created_by": {"username": "person@example.com"},
                "expiry_time": "2027-01-01T00:00:00.000Z",
            }
        ]
    }
    row = _token_table(data).splitlines()[1]
    # One record on one line, with the free text kept in a single cell.
    assert row.count("\n") == 0
    assert '"ci runner, nightly builds"' in row
    assert len(_token_table(data).splitlines()) == 2


def test_csv_cell_escapes_quotes_and_normalises_blanks():
    assert _csv_cell(None) == "N/A"
    assert _csv_cell("   ") == "N/A"
    assert _csv_cell('say "hi", please') == '"say ""hi"", please"'
    assert _csv_cell("plain") == "plain"


def test_token_table_reports_nothing_for_an_empty_response():
    assert _token_table({"data": []}) is None
    assert _token_table({"error_message": "boom"}) is None


# ---------------------------------------------------------------------------
# 5. A catalog secret link nests its secrets under
#    {kind}_secrets_{scope}, keyed secrets_{scope}_id — a different shape
#    from the flat {kind}_secrets_{scope}_id field the write side takes.
# ---------------------------------------------------------------------------
_LINK_ROW = {
    "metastore_catalog_secrets_user_id": 40,
    "metastore_catalog_id": 17,
    "metastore_catalog": {
        "metastore_catalog_id": 17,
        "name": "demo_unity",
        "catalog_type": "DATABRICKS UNITY",
    },
    "metastore_secrets_user": {"secrets_user_id": 29, "name": "unity-token"},
    "object_storage_secrets_user": None,
}


def test_linked_secret_reads_the_nested_secret_with_its_name():
    assert _linked_secret(_LINK_ROW, "metastore", "user") == "29 (unity-token)"


def test_linked_secret_reports_an_absent_secret_as_none():
    assert _linked_secret(_LINK_ROW, "object_storage", "user") == "none"


def test_link_table_renders_a_live_link_row():
    table = _link_table(
        {"data": [_LINK_ROW]}, "metastore_catalog_secrets_user_id", 1, "user")
    header, row = table.splitlines()[0], table.splitlines()[1]
    assert header.endswith("metastore_secret,object_storage_secret")
    assert row == "40,17,demo_unity,DATABRICKS UNITY,29 (unity-token),none"


def test_link_table_reports_an_empty_scope():
    assert "No catalog secret links found" in _link_table(
        {"data": []}, "metastore_catalog_secrets_tenant_id", 1, "tenant")


# ---------------------------------------------------------------------------
# 6. There is no GET /workspace by id, so a workspace is read back from a
#    listing. The rows have to be found whatever the response wraps them in.
# ---------------------------------------------------------------------------
def test_candidate_rows_accepts_both_listing_shapes():
    row = {"workspace_id": 348, "name": "mcp"}
    assert _candidate_rows({"data": [row]}) == [row]
    assert _candidate_rows([row]) == [row]
    assert _candidate_rows({"result_set": {"total_pages": 0}}) == []
    assert _candidate_rows(None) == []


# ---------------------------------------------------------------------------
# 7. A run's timing sits at the top level but its identity does not: the job
#    and the cluster are nested under job_conf, and the single-run endpoint
#    names the job `name` where the listing names it `job_name`. Reading the
#    top level alone yields a run with no cluster, which does not raise -- it
#    silently makes the run uncostable and reports its job as N/A.
# ---------------------------------------------------------------------------
def test_normalise_reads_identity_from_job_conf():
    row = {
        "run_id": 1810365,
        "run_status": "DONE",
        "total_run_time_sec": 15.68,
        "execution_time_sec": 15.28,
        "approx_ycu": 0.01,
        "job_conf": {
            "job_id": 1801746,
            "name": "mcp-runsql-probe",
            "spark_job_type": {"job_type": "SPARK_SQL", "language": "SQL"},
            "cluster_info": {
                "cluster_id": 357, "name": "onprem-s", "cluster_status": "RUNNING",
            },
        },
    }
    run = _normalise(row)
    assert run["job_id"] == 1801746
    assert run["job_name"] == "mcp-runsql-probe"
    assert run["cluster_id"] == 357
    assert run["cluster_name"] == "onprem-s"
    assert (run["total_sec"], run["execution_sec"]) == (15.68, 15.28)
    assert run["status"] == "DONE"


def test_normalise_reads_the_listing_spelling_of_the_job_name():
    run = _normalise({"run_id": 1, "job_conf": {"job_name": "nightly-load"}})
    assert run["job_name"] == "nightly-load"


def test_normalise_falls_back_to_the_notebook_identity():
    run = _normalise({"job_conf": {"notebook_id": 42, "notebook_name": "explore"}},
                     fallback_run_id=7)
    assert (run["run_id"], run["job_id"], run["job_name"]) == (7, 42, "explore")


def test_normalise_survives_a_run_with_no_job_conf():
    run = _normalise({"run_id": 9})
    assert run["cluster_id"] is None and run["job_id"] is None
    assert run["status"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Notebook widgets. The listing is a map keyed by widget name rather than an
# array, and each entry repeats itself three times over: `widgetInfo`,
# `typedWidgetInfo` and a bare `currentValue`. Reading the wrong one of those
# gets the label and the choices from a different schema than the type, so the
# fixtures below are the two shapes the platform documents -- a text widget and
# a multiselect -- taken together in one map, as a real notebook returns them.
_WIDGETS = {
    "param_2": {
        "currentValue": "",
        "nuid": "cf95595f-1b73-45ee-8419-7557b0d3cd7d",
        "typedWidgetInfo": {
            "autoCreated": False,
            "defaultValue": "a",
            "label": None,
            "name": "param_2",
            "options": {
                "widgetDisplayType": "Dropdown",
                "choices": ["a", "b", "c"],
                "fixedDomain": True,
                "multiselect": True,
            },
            "parameterDataType": "String",
        },
        "widgetInfo": {
            "widgetType": "multiselect",
            "defaultValue": "a",
            "label": None,
            "name": "param_2",
            "options": {
                "widgetType": "dropdown",
                "autoCreated": False,
                "choices": ["a", "b", "c"],
            },
        },
    },
    "param_1": {
        "currentValue": "status",
        "nuid": "532a0de5-53a2-49b3-b14c-662ad1a8e5dd",
        "typedWidgetInfo": {
            "autoCreated": False,
            "defaultValue": "status",
            "label": "Enter the status of job",
            "name": "param_1",
            "options": {"widgetDisplayType": "Text", "validationRegex": None},
            "parameterDataType": "String",
        },
        "widgetInfo": {
            "widgetType": "text",
            "defaultValue": "status",
            "label": "Enter the status of job",
            "name": "param_1",
            "options": {
                "widgetType": "text",
                "autoCreated": False,
                "validationRegex": None,
            },
        },
    },
}


def test_widget_rows_reads_one_row_per_named_widget():
    rows = {row[0]: row for row in _widget_rows(_WIDGETS)}
    assert set(rows) == {"param_1", "param_2"}
    assert "N/A" not in _cells(rows["param_1"])


def test_widget_rows_reads_type_default_and_current_value():
    rows = {row[0]: row for row in _widget_rows(_WIDGETS)}
    name, kind, label, default, current, choices = rows["param_1"]
    assert (kind, default, current) == ("text", "status", "status")
    assert label == "Enter the status of job"
    assert choices == ""


def test_widget_rows_reads_the_choices_of_a_multiselect():
    rows = {row[0]: row for row in _widget_rows(_WIDGETS)}
    name, kind, label, default, current, choices = rows["param_2"]
    assert kind == "multiselect"
    assert choices == "a; b; c"
    # A null label renders empty, not the string "None" and not "N/A": the
    # widget genuinely has no prompt.
    assert label == ""
    # An unset current value is empty for the same reason.
    assert current == ""


def test_widget_rows_reports_no_widgets_as_no_rows():
    assert _widget_rows({}) == []
    assert _widget_rows(None) == []


#: `GET /workspace/{ws}/spark/job/run/workflow_job_instance_details/{app_id}`
#: for run 1810365 of job 1801746, captured live. Note what is *not* here: the
#: run id is under `job_id`, and there is no `run_id` key, though the spec's
#: `WorkflowJobInstanceDetails` example shows one.
_APP_INFO = {
    "job_id": 1810365,
    "application_id": "local-1793262159123",
    "run_status": "DONE",
    "compute_engine": 345,
    "cluster_id": "357",
    "workflow_job_instance_details": {
        "workflow_job_instance_status": {
            "workflow_job_instance_id": 1819114,
            "workflow_job_id": 1819114,
            "status": "DONE",
            "previous_status": ["NONE", "INIT", "SENT", "RECEIVED", "EXECUTING"],
            "from_date": "2026-10-29T08:22:51.553031+00:00",
            "to_date": "infinity",
        },
        "workflow_execution_process": {
            "machine_pid_number": "1580",
            "machine_hostname": "yeedu13-c9b7944b-b5c1-33ad-6055-78cc47b1769a",
            "machine_id": "c9b7944b-b5c1-33ad-6055-78cc47b1769a",
            "machine_pid_user": "root",
            "machine_node_number": "0",
        },
    },
    "created_by": {"user_id": 3, "username": "sadhvik.chirunomula@modak.com"},
    "modified_by": {"user_id": 3, "username": "sadhvik.chirunomula@modak.com"},
    "last_update_date": "2026-10-29T08:22:35.868843+00:00",
    "from_date": "2026-10-29T08:22:35.868843+00:00",
    "to_date": "2026-10-29T08:22:51.553031+00:00",
}


def _fields(rows):
    """field,value rows -> a dict, dropping the header."""
    return dict(row.split(",", 1) for row in rows[1:])


def test_app_info_reads_the_run_id_from_the_job_id_key():
    """The whole point of the lookup: application_id in, run_id out.

    The response names the field `job_id`, so reading the key as named would
    label a run id as a job definition id, and reading `run_id` -- which the
    spec example shows and the response omits -- would drop it entirely.
    """
    cells = _fields(_app_info_rows(_APP_INFO))
    assert cells["run_id"] == "1810365"
    assert "job_id" not in cells


def test_app_info_reads_status_cluster_and_creator():
    cells = _fields(_app_info_rows(_APP_INFO))
    assert cells["application_id"] == "local-1793262159123"
    assert cells["run_status"] == "DONE"
    assert cells["cluster_id"] == "357"
    assert cells["compute_engine"] == "345"
    assert cells["created_by"] == "sadhvik.chirunomula@modak.com"
    assert "N/A" not in "".join(_app_info_rows(_APP_INFO))


def test_app_info_reads_the_workflow_status_history():
    cells = _fields(_app_info_rows(_APP_INFO))
    assert cells["workflow_status"] == "DONE"
    assert cells["workflow_job_instance_id"] == "1819114"
    assert cells["workflow_status_history"] == (
        "NONE -> INIT -> SENT -> RECEIVED -> EXECUTING")


def test_app_info_withholds_the_executing_node():
    """The response volunteers the node's hostname, machine id, OS user and
    pid. SYSTEM_PROMPT forbids handing back infrastructure detail, so the
    sibling block must stay unread however convenient it looks."""
    out = "\n".join(_app_info_rows(_APP_INFO))
    for leak in ("yeedu13-c9b7944b", "machine_hostname", "root", "1580",
                 "machine_id", "machine_pid"):
        assert leak not in out


def test_app_info_still_reads_a_run_id_key_if_one_ever_appears():
    """The spec documents `run_id`; if the API is corrected to match, the
    mapping must keep working rather than start returning no run id."""
    record = {k: v for k, v in _APP_INFO.items() if k != "job_id"}
    record["run_id"] = 1810365
    assert _fields(_app_info_rows(record))["run_id"] == "1810365"


#: stderr of a successful clone into workspace 348, captured live. git reports
#: progress on stderr, and names the server-side mount it is writing to.
_CLONE_STDERR = (
    "Cloning into '/nfs/yeedu/files/7e27253a-1ab8-4623-a789-56ae7e8939e9/348"
    "/mcp_git_probe'...\n"
    "Updating files:  40% (12/30)\n"
    "Updating files: 100% (30/30), done."
)


def test_clone_log_keeps_what_git_actually_said():
    """The log is the only account of a failed clone, so scrubbing must not
    cost any of git's own output."""
    out = _scrub_log(_CLONE_STDERR)
    assert "Updating files: 100% (30/30), done." in out
    assert "Cloning into" in out
    assert "mcp_git_probe" in out


def test_clone_log_withholds_the_mount_and_the_tenant():
    out = _scrub_log(_CLONE_STDERR)
    assert "/nfs/" not in out
    assert "7e27253a-1ab8-4623-a789-56ae7e8939e9" not in out
    assert "/yeedu/files/" not in out
    assert "file:///files/mcp_git_probe" in out


def test_clone_log_survives_an_empty_log():
    assert _scrub_log("") == ""
    assert _scrub_log(None) == ""


def test_workspace_root_is_slash_however_it_is_written():
    """`file:///files` is how browse_workspace_files renders the root, and the
    backend strips that prefix before demanding an absolute path -- so the
    root as displayed strips to the empty string and is rejected with the
    path blank in the message."""
    for given in ("file:///files", "/files", "/", ""):
        assert _workspace_path(given) == "/", given


def test_a_browsed_path_is_passed_through_as_a_workspace_path():
    assert _workspace_path("file:///files/notebooks") == "/notebooks"
    assert _workspace_path("/files/notebooks") == "/notebooks"
    assert _workspace_path("/notebooks") == "/notebooks"


# ---------------------------------------------------------------------------
# A Functions job conf, as GET /workspace/{id}/spark/job returns it, trimmed
# to what get_function_endpoint builds the HTTP path from.
# ---------------------------------------------------------------------------
_FUNCTION_JOB = {
    "job_id": 1801655,
    "job_name": "sentiment_analysis",
    "tenant_id": "7e27253a-1ab8-4623-a789-56ae7e8939e9",
    "yeedu_functions_function_name": "analyze",
    "yeedu_functions_script_path": "/functions/sentiment.py",
}


def test_function_path_is_built_from_ids_not_names():
    """The proxy route is /{tenant_id}/{workspace_id}/{job_id} and looks the
    request up in Redis under f"{tenant_id}.{workspace_id}.{job_id}", keyed on
    w.tenant_id, sj.workspace_id and sj.id. A path carrying the workspace name
    and the job name instead -- which the proxy's own parameter names and its
    local test harness both suggest -- gets "Job configuration not found"."""
    path = _function_path(_FUNCTION_JOB, 298, 1801655)
    assert path == "/7e27253a-1ab8-4623-a789-56ae7e8939e9/298/1801655"
    assert "sentiment_analysis" not in path


def test_function_path_falls_back_to_the_requested_job_id():
    """A record that came back without its id still yields a usable path when
    the caller named the job by id."""
    record = {k: v for k, v in _FUNCTION_JOB.items() if k != "job_id"}
    assert _function_path(record, 298, 1801655).endswith("/298/1801655")


def test_function_path_marks_a_segment_it_could_not_resolve():
    """Looked up by job_name on a record with neither tenant nor id, the path
    shows placeholders rather than rendering None into a URL."""
    path = _function_path({}, 298, None)
    assert path == "/<tenant-id>/298/<job-id>"
    assert "None" not in path
