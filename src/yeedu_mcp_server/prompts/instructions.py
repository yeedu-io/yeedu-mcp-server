"""MCP Prompt Templates for Yeedu interactive processes.

These prompts appear as templates/slash commands in the MCP client UI.
"""

from yeedu_mcp_server.mcp_server import mcp


@mcp.prompt()
def create_job_instructions() -> str:
    """Get interactive instructions for creating a job configuration"""
    return """
    You are about to create a job.  
    Follow these instructions exactly:

    1. You must interactively ask the user for the parameters listed below.  
    2. Do not skip any parameter — if it's optional, confirm whether the user wants to provide it.  
    3. Apply all validations and constraints from the parameter descriptions.  
    4. After collecting all inputs, summarize the full job configuration back to the user.  
    5. Ask for explicit confirmation ("Do you want to proceed and create the job?") before calling the `create_job` tool.  
    6. Only proceed to call the `create_job` tool if the user confirms YES.  
    7. Important: Never create the job automatically. If the user does not explicitly confirm, do not call any creation API/tool.

    Parameters & Constraints

    Required:
    - name *(1-64 chars, allowed: a-z, 0-9, ., @, _, -)*
    - job_type *(One of: Custom Code, Thrift SQL, Functions, JAR)*
    - job_language *(Required only if job_type=Custom Code; One of: Raw Scala, PySpark, Spark SQL)*

    Concurrency Rules:
    - max_concurrency:  
    * 0 = unlimited  
    * >0 = limit concurrent runs  

    Functions-Specific:
    - yeedu_functions_max_request_concurrency *(1-512)*
    - yeedu_functions_request_timeout_sec *(1-900 seconds)*
    - yeedu_functions_idle_timeout_sec *(300-172800 seconds)*
    - yeedu_functions_project_path *(root directory if using local imports; must start with file:///files/)*
    - yeedu_functions_script_path *(Python file containing the function; must start with file:///files/)*
    - yeedu_functions_function_name *(function inside script to expose as endpoint)*
    - yeedu_functions_requirements *(optional: Python dependencies list)*

    Functions Behavior:
    Yeedu Functions lets you deploy plain Python functions, AI/ML models, or PySpark code as REST API endpoints.  
    It supports auto-scaling based on request load.

    Function Rules:
    - Function must accept two parameters: `payload` (POST body) and `context` (metadata such as request_uuid).
    - Function must return a JSON-serializable response.
    - Optional: You may define an `init()` function to initialize state or connections during deployment.

    Using Project Path (Local Imports):
    - Use `yeedu_functions_project_path` if your function depends on local Python modules.  
    - Example:
      - Project Path: `file:///files/my_project`
      - Script Path: `file:///files/my_project/main.py`
      - Function Name: `multiply_numbers`

    should_append_params:
    - true = append job Spark config to cluster Spark config  
    - false = override cluster Spark config

    Custom Code Specific:
    - job_command, job_arguments → only if `language` is PySpark or JAR.
    job_command is the path to the job's script file, example: 'file:///files/spark-examples_2.12-3.2.2.jar'. If the file does not exist, create a new file using the `upload_workspace_file` tool based on the user's instruction.
    - job_class_name → only if `language` is JAR
    - job_rawScalaCode → only if `language` is Raw Scala

    Cluster Mode 'STANDALONE' OR 'CLUSTER' Only:
    - driver_memory, driver_cores, executor_memory, executor_cores, num_executors, total_executor_cores

    If cluster mode is 'YEEDU': omit ALL execution parameters

    File Paths:
    - All file paths must start with: `file:///files/`

    Cluster Attachment:
    - Attaching a cluster is optional. You can create a job without specifying `cluster_id` (or any cluster). The job can be attached to a cluster later if desired.

    Other Optional Parameters:
    - cluster_id *(Cluster ID to run job on)*
    - files, properties_file, conf, packages, repositories, jars, archives
    - driver_java_options, driver_library_path, driver_class_path
    - principal, keytab, queue
    - job_sqlCode *(SQL job only)*
    - job_timeout_min
    - should_append_params
    - yeedu_functions_* fields (if applicable)

    Final Rule:  
    You must not create the job without:  
    1. Gathering all inputs.  
    2. Validating them.  
    3. Showing a summary to the user.  
    4. Getting an explicit "YES" from the user.  
    5. If the user response is anything other than an unambiguous YES (e.g., yes/confirm/proceed), you must not call `create_job`.

    If the user says "NO", cancel the operation.
    """


@mcp.prompt()
def edit_job_instructions() -> str:
    """Get interactive instructions for editing a job configuration"""
    return """
    You are about to edit a job configuration.

    Follow these rules exactly:

    1. You must interactively ask the user which parameters they want to update from the list below.  
    2. Do not prompt for all parameters if not needed — only ask for those the user wants to change.  
    3. For each chosen parameter, validate input against the constraints below.  
    4. Summarize the intended changes to the user before proceeding.  
    5. Ask for explicit confirmation ("Do you want to proceed and update the job?") before calling the `edit_job` tool.  
    6. Only call `edit_job` if the user confirms YES. If NO, cancel the operation.  
    7. Important: Never apply edits automatically. If the user does not explicitly confirm, do not call any edit/update API/tool.

    Editable Parameters & Constraints

    Concurrency Rules:
    - max_concurrency: 0 = unlimited, >0 = limit concurrent runs  

    Functions-Specific:
    - yeedu_functions_max_request_concurrency *(1-512)*
    - yeedu_functions_request_timeout_sec *(1-900 seconds)*
    - yeedu_functions_idle_timeout_sec *(300-172800 seconds)*
    - yeedu_functions_project_path, yeedu_functions_script_path, yeedu_functions_function_name, yeedu_functions_requirements → *only for Functions job type*

    should_append_params:
    - true = append job Spark config to cluster Spark config  
    - false = override cluster Spark config

    Custom Code Specific:
    - job_command, job_arguments → only if job_type is Custom Code and `language` is PySpark or Spark SQL or Raw Scala.
    job_command is the path to the job's script file, example: 'file:///files/spark-examples_2.12-3.2.2.jar'. If the file does not exist, create a new file using the `upload_workspace_file` tool based on the user's instruction.
    - job_class_name → only if job_type=Custom Code and language is JAR
    - job_rawScalaCode → only if job_type=Custom Code and language is Raw Scala

    File Paths:
    - All file paths must start with: `file:///files/`

    Other Optional Editable Fields:
    - name *(1-64 chars, allowed: a-z, 0-9, ., @, _, -)*
    - cluster_id *(numeric)*
    - cluster_name *(string)*
    - files, properties_file, conf, packages, repositories, jars, archives
    - driver_java_options, driver_library_path, driver_class_path
    - principal, keytab, queue
    - job_sqlCode *(Spark SQL / Thrift SQL job only)*
    - job_timeout_min
    - should_append_params
    - yeedu_functions_ fields (if applicable)

    Final Rule:
    You must not edit the job without:  
    1. Asking which fields to change.  
    2. Validating each field's new value.  
    3. Summarizing changes to the user.  
    4. Getting an explicit "YES" to proceed.  
    5. If the user response is anything other than an unambiguous YES (e.g., yes/confirm/proceed), you must not call `edit_job`.

    If the user says "NO", cancel the update.
    """


@mcp.prompt()
def create_cluster_instructions() -> str:
    """Get interactive instructions for creating a cluster configuration."""
    return """
    You are about to create a cluster.
    Follow these instructions exactly:

    1. First call `get_cluster_prerequisites` to fetch available cloud environments, cluster configurations, and spark infra versions.
    2. Present the available options to the user in a readable format.
    3. Ask the user to select:
       - A cloud_env_id (from cloud environments list)
       - A cluster_conf_id (from cluster configurations list)
       - A spark_infra_version_id (from spark infra versions list)
    4. Ask for a cluster name (1-64 chars, allowed: a-z, 0-9, ., @, _, -).
    5. Ask for cluster_type:
       - YEEDU: Auto-managed, Yeedu handles provisioning. No worker count needed.
       - STANDALONE: Single node cluster. No worker count needed.
       - CLUSTER: Multi-node cluster. Ask for num_of_workers (1-30, required).
    6. Ask for num_of_workers only if cluster_type is CLUSTER.
    7. Optionally ask for:
       - idle_timeout_ms (default: 240000 ms = 4 minutes)
       - description
       - is_spot_instance (default: false)
       - enable_public_ip (default: false)
       - is_turbo_enabled (default: false, requires spark infra with turbo support)
       - is_cuda_enabled (default: false, requires cluster_conf with has_cuda=true and spark infra with cuda support)
       - min_instances / max_instances (required for YEEDU/STANDALONE, default 1; must NOT be sent for CLUSTER type)
       - max_parallel_spark_job_execution_per_instance
       - bootstrap_shell_script (shell script to run on startup)
       - object_storage_manager_id (attach storage)
       - metastore_catalog_id (attach metastore)
       - spark_config: {conf: [], packages: [], jars: [], archives: [], repositories: [], env_var: [], files: [], py-files: []}
       - mount_ids: list of mount IDs
    8. Summarize the full cluster configuration back to the user.
    9. Ask for explicit confirmation ("Do you want to proceed and create the cluster?") before calling `create_cluster`.
    10. Only call `create_cluster` if the user confirms YES.
    11. Never create the cluster automatically without user confirmation.

    IMPORTANT RULES:
    - cluster_type YEEDU or STANDALONE → do NOT set num_of_workers
    - cluster_type CLUSTER → num_of_workers MUST be set (1-30)
    - is_cuda_enabled=true requires both cluster_conf with has_cuda=true AND spark infra with has_cuda_support=true
    - is_turbo_enabled=true requires spark infra with has_turbo_support=true
    - Cluster name must be unique within the workspace

    SPARK VERSION SELECTION BASED ON FEATURES:
    - If user wants CUDA enabled → only show/select spark_infra_version where has_cuda_support=true
    - If user wants TURBO enabled → only show/select spark_infra_version where has_turbo_support=true
    - If user wants BOTH CUDA and TURBO → only show/select spark_infra_version where has_cuda_support=true AND has_turbo_support=true
    - If neither is needed → any spark_infra_version is valid
    - Always filter the spark infra versions list BEFORE presenting options to the user based on their CUDA/TURBO preference.
    - If no spark version supports the requested combination, inform the user that the feature combination is not available.
    """
