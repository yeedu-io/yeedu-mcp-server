"""Spark job definitions, execution control, and log monitoring tools for Yeedu MCP Server."""

import json
from typing import List, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.job_type_utils import (
    display_job_type,
    display_job_lang,
)


@mcp.tool(
    description="Lists job and notebook execution run history for a workspace with optional filters (name, status, type, language, cluster IDs, creator/modifier user IDs). Returns a table: run_id, status, job_name, job_id, type, language, cluster_name, execution_time, created_by_username, modified_by_username with pagination.",
    annotations={"readOnlyHint": True}
)
async def workspace_job_runs(
    workspace_id: int = Field(
        description="Workspace ID for job/notebook runs"),
    job_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the job/notebook to filter"
    ),
    cluster_ids: Optional[List[int]] = Field(
        default=None, description="Set of cluster instance IDs to filter on"
    ),
    job_ids: Optional[List[int]] = Field(
        default=None, description="Set of job/notebook configuration IDs to filter on"
    ),
    run_status: Optional[str] = Field(
        default=None,
        description="Filter by run status: SUBMITTED, RUNNING, DONE, ERROR, TERMINATED, STOPPING, STOPPED",
    ),
    job_type: Optional[List[str]] = Field(
        default=None,
        description="Filter by job types: Custom Code, Notebook, Thrift SQL, Functions, JAR",
    ),
    job_type_langs: Optional[List[str]] = Field(
        default=None,
        description="Filter by languages: Raw Scala, PySpark, JAR, Python3, Scala, Spark SQL, Python, Thrift SQL",
    ),
    created_by_ids: Optional[List[int]] = Field(
        default=None, description="Set of user IDs to filter by creator"
    ),
    modified_by_ids: Optional[List[int]] = Field(
        default=None, description="Set of user IDs to filter by modifier"
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of job runs to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """List job/notebook execution runs with filtering by name, status, type, cluster, and user - shows run history with pagination"""
    try:
        params = {}

        # Only add list parameters if they have values
        if cluster_ids:
            params["cluster_ids"] = cluster_ids
        if job_ids:
            params["job_ids"] = job_ids
        if job_type:
            params["job_type"] = job_type
        if job_type_langs:
            params["job_type_langs"] = job_type_langs
        if created_by_ids:
            params["created_by_ids"] = created_by_ids
        if modified_by_ids:
            params["modified_by_ids"] = modified_by_ids

        # Only add run_status if it has a value
        if run_status:
            params["run_status"] = run_status

        # Always add pagination parameters
        params["pageNumber"] = page
        params["limit"] = limit

        async with YeeduClient() as client:
            if job_name:
                params["job_name"] = job_name
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/spark/job/runs/search",
                    params=params
                )
            else:
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/spark/job/runs",
                    params=params
                )

            if not data or "data" not in data:
                return f"No job runs found for workspace {workspace_id} or error in API response."

            # Client-side filter: API may not honour job_type filter reliably
            from yeedu_mcp_server.utils.job_type_utils import JOB_TYPE_DISPLAY_TO_CODE
            job_type_codes_filter = (
                {JOB_TYPE_DISPLAY_TO_CODE.get(t, t) for t in job_type}
                if job_type else None
            )
            # Use a set of requested lang display names for context-aware matching
            job_type_langs_display_filter = set(
                job_type_langs) if job_type_langs else None

            # Extract relevant fields
            job_runs = []
            for job in data["data"]:
                job_type_info = job.get("job_conf", {}).get(
                    "spark_job_type", {})
                cluster_name = (
                    job.get("job_conf", {}).get(
                        "cluster_info", {}).get("name", "N/A")
                )
                job_type_code = job_type_info.get("job_type", "N/A")
                lang_code = job_type_info.get("language", "N/A")

                # Skip rows that don't match the requested job_type filter
                if job_type_codes_filter and job_type_code not in job_type_codes_filter:
                    continue
                # Skip rows that don't match the requested job_type_langs filter
                # Compare using display_job_lang so PySpark != Python (both Python3 internally)
                if job_type_langs_display_filter and display_job_lang(job_type_code, lang_code) not in job_type_langs_display_filter:
                    continue

                job_runs.append(
                    [
                        job.get("run_id", "N/A"),
                        job.get("run_status", "N/A"),
                        job.get("job_conf", {}).get("job_name", "N/A"),
                        job.get("job_conf", {}).get("job_id", "N/A"),
                        display_job_type(job_type_code),
                        display_job_lang(job_type_code, lang_code),
                        cluster_name,
                        f"{job.get('execution_time_sec') or 0:.2f}s",
                        job.get("created_by", {}).get("username", "N/A"),
                        job.get("modified_by", {}).get("username", "N/A"),
                    ]
                )

            # Create table
            table = serialize_table(
                job_runs,
                headers=[
                    "run_id",
                    "status",
                    "job_name",
                    "job_id",
                    "type",
                    "language",
                    "cluster_name",
                    "execution_time",
                    "created_by_username",
                    "modified_by_username",
                ],
            )

            # Only add pagination info if result_set is present and not null
            result_set = data.get("result_set")
            if result_set:
                pagination_info = (
                    f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} | "
                    f"Total Items: {result_set.get('total_objects', 0)} | "
                    f"Items Per Page: {limit}"
                )
                table += pagination_info

            return table

    except Exception as e:
        return f"Error listing job runs: {str(e)}. HINT: Double-check the workspace_id and any filter arrays (e.g. cluster_ids, job_ids)."


@mcp.tool(
    description="Gets aggregated job execution counts for a workspace broken down by status. Returns a table: Status (SUBMITTED, RUNNING, DONE, ERROR, TERMINATED, STOPPING, STOPPED, TOTAL) and Count.",
    annotations={"readOnlyHint": True}
)
async def get_workspace_stats(
    workspace_id: int = Field(description="Workspace ID for statistics"),
) -> str:
    """Get job execution statistics (submitted, running, done, error, terminated) for workspace"""
    try:
        params = {}
        params["workspace_id"] = workspace_id

        async with YeeduClient() as client:
            data = await client.get("/api/v1/workspace/stats", params=params)

            # Create a list of status counts, excluding TOTAL_JOB_COUNT
            status_counts = []
            total_jobs = data.get("TOTAL_JOB_COUNT", 0)

            # Define the order of statuses for display
            status_order = [
                "SUBMITTED",
                "RUNNING",
                "DONE",
                "ERROR",
                "TERMINATED",
                "STOPPING",
                "STOPPED",
            ]

            # Add each status count in the defined order
            for status in status_order:
                count = data.get(status, 0)
                status_counts.append([status, count])

            # Add total at the end
            status_counts.append(["TOTAL", total_jobs])

            # Create table
            return serialize_table(status_counts, headers=["Status", "Count"])

    except Exception as e:
        return f"Error getting workspace stats: {str(e)}. HINT: Verify that workspace_id exists and you have permission to view stats."


@mcp.tool(
    description="Gets the full configuration of a job by job_id, including job type, language, code/command/arguments, cluster info (ID, name, status, Spark version), resource settings (driver/executor memory, cores, num_executors), dependencies (conf, packages, jars, archives), and timeout. Returns a field-value table.",
    annotations={"readOnlyHint": True}
)
async def get_job_configuration(
    workspace_id: int = Field(description="Workspace ID containing the job"),
    job_id: int = Field(description="Job ID to retrieve (preferred)"),
) -> str:
    """Get comprehensive job configuration including Spark settings, cluster info, and resource allocation"""
    try:
        params = {}
        params["job_id"] = job_id

        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/spark/job",
                params=params
            )

            # Helper function to handle null/None values
            def safe_get(value, default="N/A"):
                return value if value is not None else default

            # Extract all relevant fields
            job_conf = [
                # Basic Job Information
                ["Job ID", safe_get(data.get("job_id"))],
                ["Job Name", safe_get(data.get("job_name"))],
                ["Max Concurrency", safe_get(data.get("max_concurrency"))],
                ["Job Class Name", safe_get(data.get("job_class_name"))],
                ["Job Command", safe_get(data.get("job_command"))],
                ["Job Arguments", safe_get(data.get("job_arguments"))],
                ["Job SQL Code" if data.get("spark_job_type", {}).get("language", "N/A").lower(
                ) == "sql" else "Job Raw Scala Code", safe_get(data.get("job_rawScalaCode"))],
                ["Job Timeout (min)", safe_get(data.get("job_timeout_min"))],
                # Job Type Information
                ["Job Type", display_job_type(safe_get(
                    data.get("spark_job_type", {}).get("job_type")))],
                ["Language", display_job_lang(
                    data.get("spark_job_type", {}).get("job_type", ""),
                    data.get("spark_job_type", {}).get("language", ""),
                )],
                # Cluster Information
                ["Cluster ID", safe_get(
                    data.get("cluster_info", {}).get("cluster_id"))],
                ["Cluster Name", safe_get(
                    data.get("cluster_info", {}).get("name"))],
                ["Cluster Status", safe_get(
                    data.get("cluster_info", {}).get("cluster_status"))],
                ["Cluster Type", safe_get(
                    data.get("cluster_info", {}).get("cluster_type"))],
                ["Instance Size", safe_get(
                    data.get("cluster_info", {}).get("instance_size"))],
                # Spark Infrastructure Version
                [
                    "Spark Version",
                    safe_get(data.get("cluster_info", {})
                             .get("spark_infra_version", {})
                             .get("spark_version")),
                ],
                # Spark Configuration
                ["Driver Memory", safe_get(data.get("driver_memory"))],
                ["Driver Java Options", safe_get(
                    data.get("driver_java_options"))],
                ["Driver Library Path", safe_get(
                    data.get("driver_library_path"))],
                ["Driver Class Path", safe_get(data.get("driver_class_path"))],
                ["Executor Memory", safe_get(data.get("executor_memory"))],
                ["Driver Cores", safe_get(data.get("driver_cores"))],
                ["Total Executor Cores", safe_get(
                    data.get("total_executor_cores"))],
                ["Executor Cores", safe_get(data.get("executor_cores"))],
                ["Number of Executors", safe_get(data.get("num_executors"))],
                # Additional Resources
                ["Files", safe_get(data.get("files"))],
                ["Properties File", safe_get(data.get("properties_file"))],
                ["Configuration", safe_get(data.get("conf"))],
                ["Packages", safe_get(data.get("packages"))],
                ["Repositories", safe_get(data.get("repositories"))],
                ["JARs", safe_get(data.get("jars"))],
                ["Archives", safe_get(data.get("archives"))],
            ]

            # Create table
            return serialize_table(job_conf, headers=["Field", "Value"])

    except Exception as e:
        return f"Error getting job configuration: {str(e)}. HINT: Ensure job_id exists and is within the workspace_id."


@mcp.tool(
    description="Retrieves the last N lines of STDOUT or STDERR logs for a specific job run. Essential for debugging failures and monitoring job output. Returns raw log text.",
    annotations={"readOnlyHint": True}
)
async def get_job_run_logs(
    workspace_id: int = Field(
        description="Workspace ID containing the job run"),
    run_id: int = Field(description="Job run ID for logs"),
    log_type: str = Field(
        description="Type of logs to retrieve: STDOUT or STDERR"),
    last_n_lines: int = Field(
        ge=1, le=1000, description="Number of lines to retrieve from the end of the file")
) -> str:
    """Retrieve job run execution logs (STDOUT/STDERR) - essential for debugging job failures and monitoring output"""
    try:
        params = {"last_n_lines": last_n_lines}

        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/spark/job/run/{run_id}/log/{log_type.lower()}",
                params=params,
                headers={"Accept": "text/plain"},
            )

            if not data:
                return f"No {log_type} logs available for run {run_id}."

            if isinstance(data, str):
                return data

            if isinstance(data, dict):
                for key in ("last_n_lines", "output", "logs", "log"):
                    value = data.get(key)
                    if isinstance(value, str):
                        return value
                return json.dumps(data)

            return str(data)

    except Exception as e:
        return f"Error getting job logs: {str(e)}. HINT: Verify that log_type is STDOUT or STDERR and the run has started/finished."


@mcp.tool(
    description="Retrieves system-level error messages from the workflow engine for a failed job run by run_id. Returns a table of error messages with pagination.",
    annotations={"readOnlyHint": True}
)
async def get_job_errors(
    workspace_id: int = Field(
        description="Workspace ID containing the job run"),
    run_id: int = Field(description="Job run ID for getting system errors"),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of errors to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """Get system error messages for failed job runs - critical for troubleshooting job execution issues"""
    try:
        params = {"limit": limit, "pageNumber": page}

        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/spark/job/run/{run_id}/workflow/errors",
                params=params
            )

            if "data" not in data:
                return f"No workflow errors found for job run {run_id} in workspace {workspace_id} or error in API response."

            # Extract relevant fields
            errors = []
            for error in data["data"]:
                errors.append([error])

            # Create table
            table = serialize_table(errors, headers=[
                             "System Error Messages"])

            # Add pagination info if available
            result_set = data.get("result_set")
            if result_set:
                pagination_info = (
                    f"\nPage {result_set.get('current_page', page)} of {result_set.get('total_pages', 1)} | "
                    f"Total Items: {result_set.get('total_objects', 0)} | "
                    f"Items Per Page: {limit}"
                )
                table += pagination_info

            return table

    except Exception as e:
        return f"Error getting job errors: {str(e)}. HINT: Only jobs with error status have workflow engine logs; verify run_id is correct."


@mcp.tool(
    description="Lists job configurations in a workspace with optional filters (name, type, language, cluster IDs, enabled state, last run status, has_run flag, creator/modifier). Returns a table: job_id, name, type, language, cluster, last_run_status, last_run_id, created_by, modified_by with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_job_configurations(
    workspace_id: int = Field(description="Workspace ID containing the job"),
    job_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the job to filter"
    ),
    enable: Optional[bool] = Field(
        default=True,
        description="Filter by state: True=active, False=disabled, None=all",
    ),
    cluster_ids: Optional[List[int]] = Field(
        default=None, description="Set of cluster instance IDs to filter on"
    ),
    job_type: Optional[List[str]] = Field(
        default=None,
        description="Filter by job types: Custom Code, Thrift SQL, Functions, JAR",
    ),
    job_type_langs: Optional[List[str]] = Field(
        default=None, description="Filter by languages: Raw Scala, PySpark, JAR, Python3, Spark SQL, Thrift SQL"
    ),
    has_run: Optional[bool] = Field(
        default=None,
        description="Filter by whether the job has run: True implies that the job was run previously, False implies that the job was never run, None implies all the jobs",
    ),
    last_run_status: Optional[List[str]] = Field(
        default=None,
        description="Filter by run status: SUBMITTED, RUNNING, DONE, ERROR, TERMINATED, STOPPING, STOPPED",
    ),
    created_by_ids: Optional[List[int]] = Field(
        default=None, description="Set of user IDs to filter by creator"
    ),
    modified_by_ids: Optional[List[int]] = Field(
        default=None, description="Set of user IDs to filter by modifier"
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of runs to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """List or Search job configurations for workspace (use pagination for more results)"""
    try:
        # build params, omitting any that are None or empty
        params = {}

        # Only add enable if it's explicitly set (not None)
        if isinstance(enable, bool):
            params["enable"] = str(enable).lower()

        # Only add list parameters if they have values
        if cluster_ids:
            params["cluster_ids"] = cluster_ids
        if job_type:
            params["job_type"] = job_type
        if job_type_langs:
            params["job_type_langs"] = job_type_langs
        if last_run_status:
            params["last_run_status"] = last_run_status
        if created_by_ids:
            params["created_by_ids"] = created_by_ids
        if modified_by_ids:
            params["modified_by_ids"] = modified_by_ids
        if isinstance(has_run, bool):
            params["has_run"] = str(has_run).lower()
        # Always add pagination parameters
        params["pageNumber"] = page
        params["limit"] = limit

        async with YeeduClient() as client:
            if job_name:
                params["job_name"] = job_name
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/spark/jobs/search",
                    params=params
                )
            else:
                data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/spark/jobs",
                    params=params
                )

            if not data or "data" not in data:
                return f"No job configurations found for workspace ID {workspace_id} or error in API response."

            # Client-side filter: API may not honour job_type filter reliably
            from yeedu_mcp_server.utils.job_type_utils import JOB_TYPE_DISPLAY_TO_CODE
            job_type_codes_filter = (
                {JOB_TYPE_DISPLAY_TO_CODE.get(t, t) for t in job_type}
                if job_type else None
            )
            # Use a set of requested lang display names for context-aware matching
            job_type_langs_display_filter = set(
                job_type_langs) if job_type_langs else None

            # Extract relevant fields
            job_confs = []
            for job in data["data"]:
                job_type_info = job.get("spark_job_type", {})
                cluster_info = job.get("cluster_info") or {
                }  # Handle None case
                last_job_run = job.get("last_job_run") or {
                }  # Handle None case
                job_type_code = job_type_info.get("job_type", "N/A")
                lang_code = job_type_info.get("language", "N/A")

                # Skip rows that don't match the requested job_type filter
                if job_type_codes_filter and job_type_code not in job_type_codes_filter:
                    continue
                # Skip rows that don't match the requested job_type_langs filter
                # Compare using display_job_lang so PySpark != Python (both Python3 internally)
                if job_type_langs_display_filter and display_job_lang(job_type_code, lang_code) not in job_type_langs_display_filter:
                    continue

                job_confs.append(
                    [
                        job.get("job_id", "N/A"),
                        job.get("job_name", "N/A"),
                        display_job_type(job_type_code),
                        display_job_lang(job_type_code, lang_code),
                        cluster_info.get("name", "N/A"),
                        last_job_run.get("run_id", "N/A"),
                        last_job_run.get("run_status", "N/A"),
                        job.get("from_date", "N/A"),
                        job.get("to_date", "N/A"),
                        job.get("created_by", {}).get("username", "N/A"),
                        job.get("modified_by", {}).get("username", "N/A"),
                    ]
                )

            # Create table
            return serialize_table(
                job_confs,
                headers=[
                    "job_id",
                    "job_name",
                    "type",
                    "language",
                    "cluster_name",
                    "latest_job_run_id",
                    "latest_job_run_status",
                    "start_time",
                    "end_time",
                    "created_by",
                    "modified_by",
                ],
            )

    except Exception as e:
        return f"Error listing job configurations: {str(e)}. HINT: Verify that workspace_id is correct and filter array values exist."


@mcp.tool(
    description="Submits a job configuration for execution on its attached cluster. The cluster must be RUNNING and enabled. Optionally pass runtime_arguments and runtime_conf (Spark conf overrides for this run only). Returns the API response with the new run details.",
    annotations={"idempotentHint": True}
)
async def run_job(
    workspace_id: int = Field(
        description="Workspace ID of the workspace where the job is present"),
    job_id: int = Field(description="ID of the job to run"),
    runtime_arguments: Optional[str] = Field(
        default=None, description="Runtime arguments for the job. example: '<any value/credential>'"),
    runtime_conf: Optional[List[str]] = Field(
        default=None, description="Runtime Spark configurations. example: ['spark.executor.memory=2g', 'spark.executor.cores=2']")
) -> str:
    """Execute a job configuration - cluster must be RUNNING and enabled"""
    try:
        json_payload = {
            "job_id": job_id
        }
        if runtime_arguments:
            json_payload["arguments"] = runtime_arguments
        if runtime_conf:
            json_payload["conf"] = runtime_conf

        async with YeeduClient() as client:
            data = await client.post(
                f"/api/v1/workspace/{workspace_id}/spark/job/run",
                json=json_payload
            )
            return f"Spark job started successfully: {data}"

    except Exception as e:
        return f"Error running job: {str(e)}. HINT: Check if the attached cluster is active and running."


@mcp.tool(
    description="Generates a structured analysis prompt to help diagnose a failing or slow job run. Does NOT call any APIs directly — returns a guided investigation checklist covering: run history, job config review, code analysis, cluster health, log inspection, and root-cause reporting.",
    annotations={"readOnlyHint": True}
)
async def analyze_job_issue(
    workspace_id: int = Field(description="Workspace ID containing the job"),
    run_id: int = Field(description="Job run ID to analyze"),
) -> str:
    """Analyze and fix issues with a job run. This tool helps with finding and fixing job errors, debugging failed jobs, analyzing job performance issues, and providing solutions for job problems."""
    prompt = f"""Please help analyze job {run_id} in workspace {workspace_id}. Follow these steps:

1. First, gather the job's basic information:
   - Use the workspace_job_runs tool to get all runs for this job configuration, including status, timing, and execution details
   - Look for patterns in job failures or performance issues across multiple runs
   - Compare execution times and resource usage across successful and failed runs

2. Review the job's configuration:
   - Use the get_job_configuration tool to examine the job's settings
   - Look for any misconfigurations in Spark parameters
   - Check if resource allocations (memory, cores) are appropriate
   - Verify the job type and language settings match the requirements
   - Check if all required dependencies are properly configured

3. Analyze the code for potential issues:
   - Check for proper error handling
   - Verify data processing logic
   - Look for resource-intensive operations
   - Check for proper Spark session management
   - Review any external API calls or data source connections
   - Look for potential memory leaks or infinite loops
   - Check for proper cleanup of resources
   - If it's a Python job:
     * Use get_workspace_file to get the content of a file and check the source code
     * Review Python-specific patterns and best practices
     * Check for proper Python package management
     * Verify Python version compatibility

4. Examine the cluster environment:
   - Use get_cluster_stats to check the cluster's resource utilization
   - Look for any resource constraints or bottlenecks
   - Verify the cluster's health and status
   - Check if the cluster type and configuration match the job's needs

5. Analyze the job's logs:
   - Use get_job_run_logs with stdout as log_type to check the application's output
   - Use get_job_run_logs with stderr as log_type to identify any error messages
   - Look for patterns in the logs that might indicate issues
   - Check for any timeout or resource-related errors
   - Look for any data access or permission issues

6. Based on your findings, provide:
   - A summary of all identified issues
   - The most likely root cause of the problems
   - Specific, actionable solutions for each issue
   - Steps to prevent similar issues in the future
   - Any additional information that would be helpful for debugging

Please proceed with your analysis and provide a detailed report. If you need any additional information, please ask for it."""

    return prompt


# Non-Custom Code job types have a fixed default language (display names sent directly to API)
CREATE_JOB_TYPE_DEFAULT_LANG = {
    'Thrift SQL': 'Thrift SQL',
    'Functions':  'Python',
    'JAR':        'JAR',
}

@mcp.tool(
    description="Creates a new Spark job configuration. Required: workspace_id, name, job_type (Custom Code/Thrift SQL/Functions/JAR). For Custom Code, specify job_language (Raw Scala/PySpark/Spark SQL). Optional: cluster_id, Spark conf/packages/jars, driver/executor resources (STANDALONE/CLUSTER clusters only), Functions-specific params, file paths (must start with file:///files/). Always call create_job_instructions prompt first. Returns the created job details.",
    annotations={"idempotentHint": True}
)
async def create_job(
    workspace_id: int = Field(
        description="Workspace ID of the workspace where the job will be created"),
    name: str = Field(
        description="Job name (1-64 chars, allowed: a-z, 0-9, ., @, _, -)"),
    job_type: str = Field(
        description="The job type to create, must be one of: Custom Code, Thrift SQL, Functions, JAR"),
    job_language: Optional[str] = Field(
        default=None, description="Programming language for Custom Code jobs. One of: Raw Scala, PySpark, Spark SQL"),
    cluster_id: Optional[int] = Field(
        default=None, description="ID of the cluster to run the job on"),
    max_concurrency: Optional[int] = Field(
        default=None, description="Maximum number of concurrent runs"),

    # File configurations
    files: Optional[List[str]] = Field(
        default=None, description="List of file paths to be used in job"),
    properties_file: Optional[List[str]] = Field(
        default=None, description="List of Spark properties files"),
    conf: Optional[List[str]] = Field(
        default=None, description="Spark conf key-value pairs"),
    packages: Optional[List[str]] = Field(
        default=None, description="List of Spark packages"),
    repositories: Optional[List[str]] = Field(
        default=None, description="List of Maven repositories"),
    jars: Optional[List[str]] = Field(
        default=None, description="List of jar files"),
    archives: Optional[List[str]] = Field(
        default=None, description="List of archive files"),

    # Driver configurations
    driver_memory: Optional[str] = Field(
        default=None, description="Memory allocated to Spark driver"),
    driver_java_options: Optional[str] = Field(
        default=None, description="Java options for driver"),
    driver_library_path: Optional[str] = Field(
        default=None, description="Driver native library path"),
    driver_class_path: Optional[str] = Field(
        default=None, description="Classpath for the driver"),
    driver_cores: Optional[int] = Field(
        default=None, description="Number of cores for driver"),

    # Executor configurations
    executor_memory: Optional[str] = Field(
        default=None, description="Memory allocated to executors"),
    executor_cores: Optional[int] = Field(
        default=None, description="Number of cores per executor"),
    num_executors: Optional[int] = Field(
        default=None, description="Total number of executors"),
    total_executor_cores: Optional[int] = Field(
        default=None, description="Total number of executor cores"),

    # Security configurations
    principal: Optional[str] = Field(
        default=None, description="Kerberos principal"),
    keytab: Optional[str] = Field(
        default=None, description="Kerberos keytab file"),
    queue: Optional[str] = Field(
        default=None, description="YARN queue to submit the job to"),

    # Job-specific configurations
    job_class_name: Optional[str] = Field(
        default=None, description="Main class name for Spark job"),
    job_command: Optional[str] = Field(
        default=None, description="Path to the job's script file, e.g., 'file:///files/script.jar'"),
    job_arguments: Optional[str] = Field(
        default=None, description="Command-line arguments for job"),
    job_rawScalaCode: Optional[str] = Field(
        default=None, description="Raw Scala code for Spark job"),
    job_sqlCode: Optional[str] = Field(
        default=None, description="SQL code for Spark SQL job"),
    job_timeout_min: Optional[int] = Field(
        default=None, description="Timeout (in minutes) for job execution"),

    should_append_params: Optional[bool] = Field(
        default=None, description="Whether to append to existing parameters"),

    # Yeedu Functions specific configurations
    yeedu_functions_project_path: Optional[str] = Field(
        default=None, description="Yeedu function project path"),
    yeedu_functions_script_path: Optional[str] = Field(
        default=None, description="Script path inside function project"),
    yeedu_functions_function_name: Optional[str] = Field(
        default=None, description="Function name to execute"),
    yeedu_functions_requirements: Optional[str] = Field(
        default=None, description="Requirements for Yeedu function (space/line separated)"),
    yeedu_functions_max_request_concurrency: Optional[int] = Field(
        default=None, description="Max concurrent requests to function"),
    yeedu_functions_idle_timeout_sec: Optional[int] = Field(
        default=None, description="Idle timeout in seconds"),
    yeedu_functions_request_timeout_sec: Optional[int] = Field(
        default=None, description="Request timeout in seconds")
) -> str:
    """Create a job"""
    try:
        # API accepts display names directly — resolve language for non-Custom-Code types
        if job_type == 'Custom Code':
            job_language_code = job_language
        else:
            job_language_code = CREATE_JOB_TYPE_DEFAULT_LANG.get(
                job_type, job_language)

        body = {
            "name": name,
            "job_type": job_type,
            "language": job_language_code,
            "cluster_id": cluster_id,
            "max_concurrency": max_concurrency,
            "files": files,
            "properties_file": properties_file,
            "conf": conf,
            "packages": packages,
            "repositories": repositories,
            "jars": jars,
            "archives": archives,
            "driver_memory": driver_memory,
            "driver_java_options": driver_java_options,
            "driver_library_path": driver_library_path,
            "driver_class_path": driver_class_path,
            "executor_memory": executor_memory,
            "principal": principal,
            "keytab": keytab,
            "queue": queue,
            "job_class_name": job_class_name,
            "job_command": job_command,
            "job_arguments": job_arguments,
            "job_rawScalaCode": job_rawScalaCode if job_rawScalaCode else job_sqlCode,
            "job_timeout_min": job_timeout_min,
            "driver_cores": driver_cores,
            "total_executor_cores": total_executor_cores,
            "executor_cores": executor_cores,
            "num_executors": num_executors,
            "should_append_params": should_append_params,
            "yeedu_functions_project_path": yeedu_functions_project_path,
            "yeedu_functions_script_path": yeedu_functions_script_path,
            "yeedu_functions_function_name": yeedu_functions_function_name,
            "yeedu_functions_requirements": yeedu_functions_requirements,
            "yeedu_functions_max_request_concurrency": yeedu_functions_max_request_concurrency,
            "yeedu_functions_idle_timeout_sec": yeedu_functions_idle_timeout_sec,
            "yeedu_functions_request_timeout_sec": yeedu_functions_request_timeout_sec
        }

        # Filter out None values
        body = {k: v for k, v in body.items() if v is not None}

        async with YeeduClient() as client:
            data = await client.post(f"/api/v1/workspace/{workspace_id}/spark/job", json=body)
            return f"Spark job created successfully: {data}"

    except Exception as e:
        return f"Error creating Spark job: {str(e)}. HINT: Ensure files, jars, or commands are prefixed with 'file:///files/' and parameters match requirements."


@mcp.tool(
    description="Updates an existing Spark job configuration by job_id. Only provided fields are changed. Editable fields include: name, cluster_id/cluster_name, Spark resources (driver/executor memory, cores), conf/packages/jars, code/command/arguments, Functions-specific params, timeout, concurrency, should_append_params. Always call edit_job_instructions prompt first. Returns the updated job details.",
    annotations={"idempotentHint": True}
)
async def edit_job(
    workspace_id: int = Field(
        description="Workspace ID of the workspace containing the job"),
    job_id: int = Field(description="ID of the job to update"),

    name: Optional[str] = Field(default=None, description="Updated job name"),
    cluster_id: Optional[int] = Field(
        default=None, description="ID of the cluster to run the job on"),
    cluster_name: Optional[str] = Field(
        default=None, description="Name of the cluster (alternative to cluster_id)"),
    max_concurrency: Optional[int] = Field(
        default=None, description="Maximum number of concurrent runs"),

    # File configurations
    files: Optional[List[str]] = Field(
        default=None, description="List of file paths to be used in job"),
    properties_file: Optional[List[str]] = Field(
        default=None, description="List of Spark properties files"),
    conf: Optional[List[str]] = Field(
        default=None, description="Spark conf key-value pairs"),
    packages: Optional[List[str]] = Field(
        default=None, description="List of Spark packages"),
    repositories: Optional[List[str]] = Field(
        default=None, description="List of Maven repositories"),
    jars: Optional[List[str]] = Field(
        default=None, description="List of jar files"),
    archives: Optional[List[str]] = Field(
        default=None, description="List of archive files"),

    # Driver configurations
    driver_memory: Optional[str] = Field(
        default=None, description="Memory allocated to Spark driver"),
    driver_java_options: Optional[str] = Field(
        default=None, description="Java options for driver"),
    driver_library_path: Optional[str] = Field(
        default=None, description="Driver native library path"),
    driver_class_path: Optional[str] = Field(
        default=None, description="Classpath for the driver"),
    driver_cores: Optional[int] = Field(
        default=None, description="Number of cores for driver"),

    # Executor configurations
    executor_memory: Optional[str] = Field(
        default=None, description="Memory allocated to executors"),
    executor_cores: Optional[int] = Field(
        default=None, description="Number of cores per executor"),
    num_executors: Optional[int] = Field(
        default=None, description="Total number of executors"),
    total_executor_cores: Optional[int] = Field(
        default=None, description="Total number of executor cores"),

    # Security configurations
    principal: Optional[str] = Field(
        default=None, description="Kerberos principal"),
    keytab: Optional[str] = Field(
        default=None, description="Kerberos keytab file"),
    queue: Optional[str] = Field(
        default=None, description="YARN queue to submit the job to"),

    # Job-specific configurations
    job_class_name: Optional[str] = Field(
        default=None, description="Main class name for Spark job"),
    job_command: Optional[str] = Field(
        default=None, description="Path to the job's script file, e.g., 'file:///files/script.jar'"),
    job_arguments: Optional[str] = Field(
        default=None, description="Command-line arguments for job"),
    job_rawScalaCode: Optional[str] = Field(
        default=None, description="Raw Scala code for Spark job"),
    job_sqlCode: Optional[str] = Field(
        default=None, description="SQL code for Spark SQL job"),
    job_timeout_min: Optional[int] = Field(
        default=None, description="Timeout (in minutes) for job execution"),

    should_append_params: Optional[bool] = Field(
        default=None, description="Whether to append to existing parameters"),

    # Yeedu Functions specific configurations
    yeedu_functions_project_path: Optional[str] = Field(
        default=None, description="Yeedu function project path"),
    yeedu_functions_script_path: Optional[str] = Field(
        default=None, description="Script path inside function project"),
    yeedu_functions_function_name: Optional[str] = Field(
        default=None, description="Function name to execute"),
    yeedu_functions_requirements: Optional[str] = Field(
        default=None, description="Requirements for Yeedu function (space/line separated)"),
    yeedu_functions_max_request_concurrency: Optional[int] = Field(
        default=None, description="Max concurrent requests to function"),
    yeedu_functions_idle_timeout_sec: Optional[int] = Field(
        default=None, description="Idle timeout in seconds"),
    yeedu_functions_request_timeout_sec: Optional[int] = Field(
        default=None, description="Request timeout in seconds")
) -> str:
    """Edit a job configuration"""
    try:
        body = {
            "name": name,
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "max_concurrency": max_concurrency,
            "files": files,
            "properties_file": properties_file,
            "conf": conf,
            "packages": packages,
            "repositories": repositories,
            "jars": jars,
            "archives": archives,
            "driver_memory": driver_memory,
            "driver_java_options": driver_java_options,
            "driver_library_path": driver_library_path,
            "driver_class_path": driver_class_path,
            "executor_memory": executor_memory,
            "principal": principal,
            "keytab": keytab,
            "queue": queue,
            "job_class_name": job_class_name,
            "job_command": job_command,
            "job_arguments": job_arguments,
            "job_rawScalaCode": job_rawScalaCode if job_rawScalaCode else job_sqlCode,
            "job_timeout_min": job_timeout_min,
            "driver_cores": driver_cores,
            "total_executor_cores": total_executor_cores,
            "executor_cores": executor_cores,
            "num_executors": num_executors,
            "should_append_params": should_append_params,
            "yeedu_functions_project_path": yeedu_functions_project_path,
            "yeedu_functions_script_path": yeedu_functions_script_path,
            "yeedu_functions_function_name": yeedu_functions_function_name,
            "yeedu_functions_requirements": yeedu_functions_requirements,
            "yeedu_functions_max_request_concurrency": yeedu_functions_max_request_concurrency,
            "yeedu_functions_idle_timeout_sec": yeedu_functions_idle_timeout_sec,
            "yeedu_functions_request_timeout_sec": yeedu_functions_request_timeout_sec
        }

        # Filter out None values
        body = {k: v for k, v in body.items() if v is not None}

        params = {"job_id": job_id}
        async with YeeduClient() as client:
            data = await client.put(f"/api/v1/workspace/{workspace_id}/spark/job", params=params, json=body)
            return f"Spark job updated successfully: {data}"

    except Exception as e:
        return f"Error updating Spark job: {str(e)}. HINT: Double-check the workspace_id and job_id values."

"""Job run control tools — get run details/status, stop a running job."""

from typing import Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Gets detailed status and configuration of a specific Spark job run by run_id. Optionally includes the full status transition history (include_status_history=true). Returns CSV field-value pairs: run_id, application_id, run_status, execution_time_sec, total_run_time_sec, approx_ycu, is_background, job_id, job_name, job_type, language, cluster_id, cluster_name, cluster_status, runtime_arguments, runtime_conf.",
    annotations={"readOnlyHint": True}
)
async def get_job_run_details(
    workspace_id: int = Field(
        description="Workspace ID containing the job run"),
    run_id: int = Field(description="Job run ID to get details for"),
    include_status_history: bool = Field(
        default=False, description="Include full status transition history"
    ),
) -> str:
    """Get job run details and optionally status history. Returns run info, timing, cluster, and config."""
    try:
        async with YeeduClient() as client:
            # Get run detail
            data = await client.get(
                f"/api/v1/workspace/{workspace_id}/spark/job/run/{run_id}"
            )

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            # CSV output - only essential fields
            job_conf = data.get("job_conf", {}) or {}
            spark_type = job_conf.get("spark_job_type", {}) or {}
            cluster_info = job_conf.get("cluster_info", {}) or {}
            runtime_args = data.get("runtime_arguments", {}) or {}

            lines = ["field,value"]
            lines.append(f"run_id,{data.get('run_id', 'N/A')}")
            lines.append(f"application_id,{data.get('application_id', 'N/A')}")
            lines.append(f"run_status,{data.get('run_status', 'N/A')}")
            lines.append(
                f"execution_time_sec,{data.get('execution_time_sec', 'N/A')}")
            lines.append(
                f"total_run_time_sec,{data.get('total_run_time_sec', 'N/A')}")
            lines.append(f"approx_ycu,{data.get('approx_ycu', 'N/A')}")
            lines.append(f"is_background,{data.get('is_background', 'N/A')}")
            lines.append(
                f"job_id,{job_conf.get('job_id', job_conf.get('notebook_id', 'N/A'))}")
            lines.append(
                f"job_name,{job_conf.get('name', job_conf.get('notebook_name', 'N/A'))}")
            lines.append(f"job_type,{spark_type.get('job_type', 'N/A')}")
            lines.append(f"language,{spark_type.get('language', 'N/A')}")
            lines.append(f"cluster_id,{cluster_info.get('cluster_id', 'N/A')}")
            lines.append(f"cluster_name,{cluster_info.get('name', 'N/A')}")
            lines.append(
                f"cluster_status,{cluster_info.get('cluster_status', 'N/A')}")
            lines.append(
                f"runtime_arguments,{runtime_args.get('arguments', 'N/A')}")
            lines.append(f"runtime_conf,{runtime_args.get('conf', 'N/A')}")

            if data.get("url"):
                lines.append(f"url,{data['url']}")

            # Optionally get status history
            if include_status_history:
                status_data = await client.get(
                    f"/api/v1/workspace/{workspace_id}/spark/job/run/{run_id}/status"
                )
                if isinstance(status_data, list):
                    lines.append("")
                    lines.append("status,start_time,end_time")
                    for s in status_data:
                        lines.append(
                            f"{s.get('run_status', 'N/A')},"
                            f"{s.get('start_time', 'N/A')},"
                            f"{s.get('end_time', 'N/A')}"
                        )

            return "\n".join(lines)

    except Exception as e:
        return f"Error getting job run details: {str(e)}. HINT: Check if workspace_id and run_id are correct."


@mcp.tool(
    description="Stops a RUNNING or SUBMITTED Spark job run by run_id. Returns CSV: run_id, action, status.",
    annotations={"destructiveHint": True}
)
async def stop_job_run(
    workspace_id: int = Field(
        description="Workspace ID containing the job run"),
    run_id: int = Field(description="Run ID of the job to stop"),
) -> str:
    """Stop a running job. Job must be in RUNNING or SUBMITTED state."""
    try:
        async with YeeduClient() as client:
            data = await client.post(
                f"/api/v1/workspace/{workspace_id}/spark/job/run/stop/{run_id}"
            )

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            lines = ["field,value"]
            lines.append(f"run_id,{run_id}")
            lines.append(f"action,stop")
            if isinstance(data, dict):
                lines.append(
                    f"status,{data.get('run_status', data.get('message', 'Stop initiated'))}")
            else:
                lines.append(f"status,Stop initiated")
            return "\n".join(lines)

    except Exception as e:
        return f"Error stopping job run: {str(e)}. HINT: A job can only be stopped if it is currently RUNNING or SUBMITTED."

