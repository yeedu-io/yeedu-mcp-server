"""Cluster instance management and monitoring tools for Yeedu MCP Server."""
import json
from typing import List, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp
from yeedu_mcp_server.utils.job_type_utils import display_job_type


@mcp.tool(
    description="Lists or searches cluster instances with optional filters (name, status, cluster config ID/name, enabled state). Returns a table: ID, Name, Type, Cluster Config ID, Spark Version, Machine Type, Architecture, Status, vCPUs, Memory, Modified By, Last Updated with pagination.",
    annotations={"readOnlyHint": True}
)
async def list_clusters(
    cluster_name: Optional[str] = Field(
        default=None, min_length=1, description="Name of the cluster to filter"
    ),
    cluster_status: Optional[List[str]] = Field(
        default=None,
        description="Filter by cluster statuses: INITIATING, RUNNING, STOPPING, STOPPED, DESTROYING, DESTROYED, ERROR, RESIZING_UP, RESIZING_DOWN",
    ),
    cluster_conf_id: Optional[int] = Field(
        default=None, description="Filter by cluster config ID"
    ),
    cluster_conf_name: Optional[str] = Field(
        default=None, description="Filter by cluster config name"
    ),
    enable: Optional[bool] = Field(
        default=True,
        description="Filter by state: True=active only, False=disabled only, None=all",
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of clusters to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """List or Search clusters (use pagination for more results)"""
    try:
        params = {}

        if cluster_status:
            params["cluster_status"] = cluster_status

        if cluster_conf_id is not None:
            params["cluster_conf_id"] = cluster_conf_id
        if cluster_conf_name:
            params["cluster_conf_name"] = cluster_conf_name

        if isinstance(enable, bool):
            params["enable"] = str(enable).lower()

        params["pageNumber"] = page
        params["limit"] = limit

        async with YeeduClient() as client:
            if cluster_name:
                params["cluster_name"] = cluster_name
                data = await client.get("/api/v1/clusters/search", params=params)
            else:
                data = await client.get("/api/v1/clusters", params=params)

            if not data or "data" not in data:
                return "No clusters found or error in API response."

            # Extract relevant fields
            clusters = []
            for cluster in data["data"]:
                # Get cluster type and version info
                cluster_type = cluster.get("cluster_type", "N/A")
                spark_version = cluster.get("spark_infra_version", {}).get(
                    "spark_version", "N/A"
                )

                # Get machine type and architecture
                cluster_conf = cluster.get("cluster_conf", {})
                machine_type = cluster_conf.get("machine_type", {})
                machine_name = machine_type.get("name", "N/A")
                architecture = machine_type.get("machine_architecture_type", {}).get(
                    "machine_architecture_type", "N/A"
                )
                # Get cluster config ID
                cluster_conf_id = cluster_conf.get("cluster_conf_id", "N/A")
                # Get cluster status
                status = cluster.get("cluster_status", "N/A")

                # Get vCPUs and memory
                vcpus = machine_type.get("vCPUs", "N/A")
                memory = machine_type.get("memory", "N/A")

                # Get modified by info
                modified_by = cluster.get("modified_by", {})
                modified_by_username = modified_by.get("username", "N/A")

                clusters.append(
                    [
                        cluster.get("cluster_id", "N/A"),
                        cluster.get("name", "N/A"),
                        cluster_type,
                        cluster_conf_id,
                        spark_version,
                        machine_name,
                        architecture,
                        status,
                        vcpus,
                        memory,
                        modified_by_username,
                        cluster.get("last_update_date", "N/A"),
                    ]
                )

            # Create table
            table = serialize_table(
                clusters,
                headers=[
                    "ID",
                    "Name",
                    "Type",
                    "Cluster Config ID",
                    "Spark Version",
                    "Machine Type",
                    "Architecture",
                    "Status",
                    "vCPUs",
                    "Memory",
                    "Modified By",
                    "Last Updated",
                ],
            )

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
        return f"Error listing clusters: {str(e)}. HINT: If listing fails due to parameters, verify your filters."


@mcp.tool(
    description="Gets Spark job execution counts for a specific cluster broken down by status. Returns a table: Job Status (TOTAL_JOB_COUNT, RUNNING, SUBMITTED, DONE, ERROR, TERMINATED, STOPPING, STOPPED) and Count.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_stats(
    cluster_id: int = Field(
        default=None, description="Cluster ID for job statistics"),
) -> str:
    """Get Spark job execution statistics (running, completed, failed, terminated, pending) for a specific cluster"""
    try:
        params = {"cluster_id": cluster_id}

        async with YeeduClient() as client:
            data = await client.get("/api/v1/cluster/stats", params=params)

            if not data:
                return "No cluster statistics found."

            # Create a list of status counts
            status_counts = []

            # Define the order of statuses for display (based on actual API response)
            status_order = [
                "TOTAL_JOB_COUNT",
                "RUNNING",
                "SUBMITTED",
                "DONE",
                "ERROR",
                "TERMINATED",
                "STOPPING",
                "STOPPED"
            ]

            # Add each status count in the defined order
            for status_key in status_order:
                count = data.get(status_key, 0)
                status_counts.append([status_key, count])

            # Create table
            table = serialize_table(status_counts, headers=[
                             "Job Status", "Count"])

            # Add cluster ID info
            cluster_info = f"\n**Cluster ID:** {cluster_id}"

            return f"{cluster_info}\n\n{table}"

    except Exception as e:
        return f"Error getting cluster stats: {str(e)}. HINT: You may have passed an invalid or non-existent cluster_id. Use list_clusters to find valid cluster IDs."


@mcp.tool(
    description="Gets comprehensive configuration details for a cluster by cluster_id or cluster_name. Returns a full key-value report covering: machine specs (vCPUs, memory, GPU, disk), cloud environment, availability zone, network and credentials config, boot disk image, Spark version with feature flags (CUDA/Turbo/Unity), Spark config, metastore catalog, object storage manager, autoscaling settings, and audit fields.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_details(
    cluster_id: Optional[int] = Field(
        default=None, description="Cluster ID"),
    cluster_name: Optional[str] = Field(
        default=None, description="Cluster name (used if ID not provided)")
) -> str:
    """Get comprehensive cluster configuration by cluster_id or cluster_name. Returns detailed cluster specs including machine type, resources, cloud environment, network config, Spark versions, storage settings, and infrastructure capabilities."""
    try:
        if (cluster_id is None) and (cluster_name is None):
            return "Error: Either cluster_id or cluster_name is required. HINT: Specify at least one of these to retrieve configuration details."

        async with YeeduClient() as client:
            params = {}
            if cluster_id:
                params["cluster_id"] = cluster_id
            elif cluster_name:
                params["cluster_name"] = cluster_name

            data = await client.get("/api/v1/cluster", params=params)

            if not data:
                identifier = f"ID {cluster_id}" if cluster_id else f"name '{cluster_name}'"
                return f"No cluster found with {identifier} or error in API response. HINT: Check if you passed a run_id or job_id by mistake. You must pass a valid cluster_id or cluster_name. Use list_clusters to find valid cluster IDs/names."

            # Extract relevant fields
            cluster_conf = data.get("cluster_conf", {}) or {}
            machine_type = cluster_conf.get("machine_type", {}) or {}
            machine_arch = machine_type.get(
                "machine_architecture_type", {}) or {}
            machine_volume_conf = cluster_conf.get(
                "machine_volume_conf", {}) or {}
            disk_type = machine_volume_conf.get("disk_type", {}) or {}

            cloud_env = data.get("cloud_env", {}) or {}
            cloud_provider = cloud_env.get("cloud_provider", {}) or {}
            availability_zone = cloud_env.get("availability_zone", {}) or {}
            network_config = cloud_env.get("network_config", {}) or {}
            credentials_config = cloud_env.get("credentials_config", {}) or {}
            boot_disk_image = cloud_env.get("boot_disk_image", {}) or {}
            linux_distro = boot_disk_image.get("linux_distro", {}) or {}

            spark_infra_version = data.get("spark_infra_version", {}) or {}
            engine_cluster_spark_config = data.get(
                "engine_cluster_spark_config", {}) or {}
            spark_config = data.get("spark_config", {}) or {}

            metastore_catalog = data.get("metastore_catalog", {}) or {}
            metastore_catalog_type = metastore_catalog.get(
                "metastore_catalog_type", {}) or {}

            object_storage_manager = data.get(
                "object_storage_manager", {}) or {}
            osm_credentials = object_storage_manager.get(
                "credentials_config", {}) or {}

            machine_volume_config = data.get("machine_volume_config", {}) or {}
            local_disk_bus_type = machine_type.get(
                "local_disk_bus_type", {}) or {}

            created_by = data.get("created_by", {}) or {}
            modified_by = data.get("modified_by", {}) or {}
            nodes = data.get("nodes") or []

            details = [
                f"Cluster ID: {data.get('cluster_id', 'N/A')}",
                f"Cluster Name: {data.get('name', 'N/A')}",
                f"Description: {data.get('description', 'N/A')}",
                f"Tenant ID: {data.get('tenant_id', 'N/A')}",
                f"Labels: {data.get('labels', {})}",
                f"Idle Timeout (ms): {data.get('idle_timeout_ms', 'N/A')}",
                f"Cluster Status: {data.get('cluster_status', 'N/A')}",
                f"Cluster Type: {data.get('cluster_type', 'N/A')}",
                f"Instance Size: {data.get('instance_size', 'N/A')}",
                f"Spot Instance: {data.get('is_spot_instance', 'N/A')}",
                f"Public IP Enabled: {data.get('enable_public_ip', 'N/A')}",
                f"Block Project SSH Keys: {data.get('block_project_ssh_keys', 'N/A')}",
                f"Min Instances: {data.get('min_instances', 'N/A')}",
                f"Max Instances: {data.get('max_instances', 'N/A')}",
                f"Total YCU: {data.get('total_ycu', 'N/A')}",
                f"Cleanup Timeout (s): {data.get('clean_up_timeout', 'N/A')}",
                f"Keep Scratch Disk: {data.get('keep_scratch_disk', 'N/A')}",
                f"Cloud Identity: {data.get('cloud_identity', 'N/A')}",
                f"Turbo Enabled: {data.get('is_turbo_enabled', 'N/A')}",
                f"CUDA Enabled: {data.get('is_cuda_enabled', 'N/A')}",
                f"Bootstrap Shell Script: {data.get('bootstrap_shell_script', 'N/A')}",
                # Cluster Configuration
                f"Cluster Config ID: {cluster_conf.get('cluster_conf_id', 'N/A')}",
                f"Cluster Config Name: {cluster_conf.get('cluster_conf_name', 'N/A')}",
                f"Machine Type Category: {cluster_conf.get('machine_type_category', 'N/A')}",
                # Machine Type
                f"Machine Type ID: {machine_type.get('machine_type_id', 'N/A')}",
                f"Machine Type: {machine_type.get('name', 'N/A')}",
                f"Machine Architecture: {machine_arch.get('machine_architecture_type', 'N/A')}",
                f"vCPUs: {machine_type.get('vCPUs', 'N/A')}",
                f"Memory: {machine_type.get('memory', 'N/A')}",
                f"Has CUDA: {machine_type.get('has_cuda', 'N/A')}",
                f"GPU Model: {machine_type.get('gpu_model', 'N/A')}",
                f"GPUs: {machine_type.get('gpus', 'N/A')}",
                f"GPU Memory: {machine_type.get('gpu_memory', 'N/A')}",
                f"CPU Model: {machine_type.get('cpu_model', 'N/A')}",
                f"CPU Min Frequency (GHz): {machine_type.get('cpu_min_frequency_GHz', 'N/A')}",
                f"CPU Max Frequency (GHz): {machine_type.get('cpu_max_frequency_GHz', 'N/A')}",
                f"Has Local Disk: {machine_type.get('has_local_disk', 'N/A')}",
                f"Local Disk Size (GB): {machine_type.get('local_disk_size_GB', 'N/A')}",
                f"Local Disk Count: {machine_type.get('local_num_of_disks', 'N/A')}",
                f"Local Disk Bus Type: {local_disk_bus_type.get('local_disk_bus_type', 'N/A')}",
                f"Local Disk Throughput (MB): {machine_type.get('local_disk_throughput_MB', 'N/A')}",
                f"Spot Instance Support: {machine_type.get('has_spot_instance_support', 'N/A')}",
                f"Machine Price (YCU): {machine_type.get('machine_price_ycu', 'N/A')}",
                # machine_volume_conf
                f"Volume Encrypted: {machine_volume_conf.get('encrypted', 'N/A')}",
                f"Volume Size (GB): {machine_volume_conf.get('size', 'N/A')}",
                f"Disk Type: {disk_type.get('name', 'N/A')}",
                f"Disk Has Fixed Size: {disk_type.get('has_fixed_size', 'N/A')}",
                f"Disk Min Size (GB): {disk_type.get('min_size', 'N/A')}",
                f"Disk Max Size (GB): {disk_type.get('max_size', 'N/A')}",
                f"Disk Has Fixed IOPS: {disk_type.get('has_fixed_iops', 'N/A')}",
                f"Disk Min IOPS: {disk_type.get('min_iops', 'N/A')}",
                f"Disk Max IOPS: {disk_type.get('max_iops', 'N/A')}",
                f"Disk Has Fixed Throughput: {disk_type.get('has_fixed_throughput', 'N/A')}",
                f"Disk Min Throughput (MB): {disk_type.get('min_throughput', 'N/A')}",
                f"Disk Max Throughput (MB): {disk_type.get('max_throughput', 'N/A')}",
                f"Volume Count: {machine_volume_conf.get('machine_volume_num', 'N/A')}",
                f"Volume Stripe Count: {machine_volume_conf.get('machine_volume_strip_num', 'N/A')}",
                f"Disk IOPS: {machine_volume_conf.get('disk_iops', 'N/A')}",
                f"Disk Throughput (MB): {machine_volume_conf.get('disk_throughput_MB', 'N/A')}",
                f"Disk Count: {machine_volume_conf.get('disk_num', 'N/A')}",
                f"Disk Size (GB): {machine_volume_conf.get('disk_size', 'N/A')}",
                # Cloud Environment
                f"Cloud Env ID: {cloud_env.get('cloud_env_id', 'N/A')}",
                f"Cloud Env Name: {cloud_env.get('cloud_env_name', 'N/A')}",
                f"Cloud Env Endpoint: {cloud_env.get('endpoint', 'N/A')}",
                f"Cloud Provider: {cloud_provider.get('name', 'N/A')}",
                f"Cloud Provider ID: {cloud_provider.get('cloud_provider_id', 'N/A')}",
                f"Cloud Project: {cloud_env.get('cloud_project', 'N/A')}",
                # Availability Zone
                f"Availability Zone: {availability_zone.get('name', 'N/A')}",
                f"Region: {availability_zone.get('region', 'N/A')}",
                f"AZ Description: {availability_zone.get('description', 'N/A')}",
                # Network Configuration
                f"Network Config ID: {network_config.get('network_conf_id', 'N/A')}",
                f"Network Config Name: {network_config.get('network_conf_name', 'N/A')}",
                f"Network Project ID: {network_config.get('network_project_id', 'N/A')}",
                f"Network Name: {network_config.get('network_name', 'N/A')}",
                f"Network Tags: {network_config.get('network_tags', 'N/A')}",
                f"Subnet: {network_config.get('subnet', 'N/A')}",
                f"Network Bridge: {network_config.get('bridge', 'N/A')}",
                f"Network Model: {network_config.get('model', 'N/A')}",
                # Credentials Configuration
                f"Credentials Config ID: {credentials_config.get('credentials_conf_id', 'N/A')}",
                f"Credentials Config Name: {credentials_config.get('credentials_conf_name', 'N/A')}",
                f"Credential Type: {credentials_config.get('credential_type', {}).get('name', 'N/A')}",
                # Boot Disk Image
                f"Boot Disk Image ID: {boot_disk_image.get('boot_disk_image_id', 'N/A')}",
                f"Boot Disk Image Name: {boot_disk_image.get('boot_disk_image_name', 'N/A')}",
                f"Boot Disk Image: {boot_disk_image.get('boot_disk_image', 'N/A')}",
                f"Linux Distro: {linux_distro.get('distro_name', 'N/A')}",
                f"Distro Version: {linux_distro.get('distro_version', 'N/A')}",
                # Spark Infrastructure Version
                f"Spark Infra Version ID: {spark_infra_version.get('spark_infra_version_id', 'N/A')}",
                f"Spark Docker Image: {spark_infra_version.get('spark_docker_image_name', 'N/A')}",
                f"Spark Version: {spark_infra_version.get('spark_version', 'N/A')}",
                f"Hive Version: {spark_infra_version.get('hive_version', 'N/A')}",
                f"Hadoop Version: {spark_infra_version.get('hadoop_version', 'N/A')}",
                f"Scala Version: {spark_infra_version.get('scala_version', 'N/A')}",
                f"Python Version: {spark_infra_version.get('python_version', 'N/A')}",
                f"Notebook Support: {spark_infra_version.get('notebook_support', 'N/A')}",
                f"CUDA Support: {spark_infra_version.get('has_cuda_support', 'N/A')}",
                f"Thrift Support: {spark_infra_version.get('thrift_support', 'N/A')}",
                f"Yeedu Functions Support: {spark_infra_version.get('yeedu_functions_support', 'N/A')}",
                f"Turbo Support: {spark_infra_version.get('has_turbo_support', 'N/A')}",
                f"Turbo Version: {spark_infra_version.get('turbo_version', 'N/A')}",
                f"Unity Support: {spark_infra_version.get('has_unity_support', 'N/A')}",
                f"Unity Version: {spark_infra_version.get('unity_version', 'N/A')}",
                f"Hive Support: {spark_infra_version.get('has_hive_support', 'N/A')}",
                f"CUDA Rapids Version: {spark_infra_version.get('cuda_rapids_version', 'N/A')}",
                # Engine Cluster Spark Configuration
                f"Max Parallel Spark Jobs per Instance: {engine_cluster_spark_config.get('max_parallel_spark_job_execution_per_instance', 'N/A')}",
                f"Worker Count: {engine_cluster_spark_config.get('num_of_workers', 'N/A')}",
                # Spark Configuration
                f"Spark Config - Conf: {spark_config.get('conf', [])}",
                f"Spark Config - Packages: {spark_config.get('packages', [])}",
                f"Spark Config - Repositories: {spark_config.get('repositories', [])}",
                f"Spark Config - JARs: {spark_config.get('jars', [])}",
                f"Spark Config - Archives: {spark_config.get('archives', [])}",
                f"Spark Config - Env Vars: {spark_config.get('env_var', [])}",
                f"Spark Config - Files: {spark_config.get('files', [])}",
                f"Spark Config - Py Files: {spark_config.get('py-files', [])}",
                f"Spark Config - Conf Secret: {spark_config.get('conf_secret', 'N/A')}",
                f"Spark Config - Env Var Secret: {spark_config.get('env_var_secret', 'N/A')}",
                # Metastore Catalog
                f"Metastore Catalog ID: {metastore_catalog.get('metastore_catalog_id', 'N/A')}",
                f"Metastore Catalog Name: {metastore_catalog.get('metastore_catalog_name', 'N/A')}",
                f"Metastore Catalog Type: {metastore_catalog_type.get('name', 'N/A')}",
                # Object Storage Manager
                f"Object Storage Manager ID: {object_storage_manager.get('object_storage_manager_id', 'N/A')}",
                f"Object Storage Manager Name: {object_storage_manager.get('object_storage_manager_name', 'N/A')}",
                f"OSM Bucket Name: {object_storage_manager.get('object_storage_bucket_name', 'N/A')}",
                f"OSM Credentials Name: {osm_credentials.get('name', 'N/A')}",
                f"OSM Credential Type: {osm_credentials.get('credential_type_name', 'N/A')}",
                # Machine Volume Config
                f"Machine Volume Config: {machine_volume_config if machine_volume_config else 'N/A'}",
                f"Nodes: {len(nodes)} node(s): " + ", ".join(
                    f"[node_id={n.get('node_id')} num={n.get('node_number')} type={n.get('node_type')} status={n.get('node_status')} ip={n.get('node_ip')} spot={n.get('is_spot_instance')}]"
                    for n in nodes
                ) if nodes else "Nodes: None",
                f"Created By: {created_by.get('username', 'N/A')}",
                f"Modified By: {modified_by.get('username', 'N/A')}",
                f"Last Updated: {data.get('last_update_date', 'N/A')}",
                f"Created Date: {data.get('from_date', 'N/A')}",
            ]

        return "\n".join(details)

    except Exception as e:
        return f"Error getting cluster details: {str(e)}. HINT: If this fails with a 404, verify the cluster_id or cluster_name."


@mcp.tool(
    description="Gets lifecycle status-change events for a cluster (e.g., INITIATING, RUNNING, STOPPED, ERROR) with start/end timestamps. Returns a table: Cluster Status ID, Cluster Status, Started By, Start Time, End Time with pagination. Use Cluster Status ID as node_status_id for get_cluster_logs and get_cluster_errors.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_events(
    cluster_id: int = Field(description="ID of the cluster to filter events"),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of events to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """Get cluster lifecycle events and status changes with timestamps and pagination"""
    try:
        params = {"cluster_id": cluster_id, "limit": limit, "pageNumber": page}

        async with YeeduClient() as client:
            data = await client.get("/api/v1/cluster/status", params=params)

            if not data or "data" not in data:
                return "No cluster events found or error in API response. HINT: Ensure you provided a valid cluster_id. Check list_clusters to find valid IDs."

            # Extract relevant fields
            events = data.get("data", [])
            if not events:
                return "No cluster events found."

            event_details = []
            for event in events:
                event_details.append(
                    [
                        event.get("cluster_status_id", "N/A"),
                        event.get("cluster_status", "N/A"),
                        event.get("created_by", "N/A"),
                        event.get("start_time", "N/A"),
                        event.get("end_time", "N/A"),
                    ]
                )

            table = serialize_table(
                event_details,
                headers=[
                    "Cluster Status ID",
                    "Cluster Status",
                    "Started By",
                    "Start Time",
                    "End Time"
                ])

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
        return f"Error getting cluster events: {str(e)}. HINT: Ensure you provided a valid cluster_id. Check list_clusters to find valid IDs."


@mcp.tool(
    description="Retrieves the last N lines of STDOUT or STDERR logs for a specific cluster node event (node_status_id from get_cluster_events). Essential for debugging cluster initialization or shutdown failures. Returns raw log text.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_logs(
    cluster_id: int = Field(description="ID of the cluster to filter logs"),
    node_status_id: int = Field(
        description="ID of the node status/event to filter logs"
    ),
    log_type: str = Field(
        description="Type of logs to retrieve: STDOUT or STDERR"),
    last_n_lines: int = Field(
        ge=1, le=1000, description="Number of lines to retrieve from the end of the file")
) -> str:
    """Retrieve cluster STDOUT/STDERR logs for specific cluster event - essential for debugging failures"""
    try:
        params = {
            "cluster_id": cluster_id,
            "node_status_id": node_status_id,
            "last_n_lines": last_n_lines
        }

        async with YeeduClient() as client:
            data = await client.get(
                f"/api/v1/cluster/log/{log_type.lower()}",
                params=params,
                headers={"Accept": "text/plain"},
            )

            if not data:
                return f"No {log_type} logs available for node_status_id {node_status_id}."

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
        return f"Error getting cluster logs: {str(e)}. HINT: Make sure the node_status_id is a valid cluster status ID from get_cluster_events and log_type is STDOUT or STDERR."


@mcp.tool(
    description="Retrieves system error messages for a specific cluster node event (node_status_id from get_cluster_events). Returns a table of error messages with pagination. Use after get_cluster_events to obtain the node_status_id.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_errors(
    cluster_id: int = Field(
        description="Cluster ID to retrieve system errors for"),
    node_status_id: int = Field(
        description="Node Status(Event) ID to retrieve system errors for"),
    limit: int = Field(
        default=10, ge=1, le=100, description="Number of errors to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """Get system error messages for cluster failures - critical for troubleshooting cluster events"""
    try:
        params = {
            "node_status_id": node_status_id,
            "limit": limit,
            "pageNumber": page
        }

        async with YeeduClient() as client:
            data = await client.get(f"/api/v1/cluster/{cluster_id}/errors", params=params)

            if "data" not in data:
                return f"No workflow errors found for cluster event with ID {node_status_id} for the cluster with ID {cluster_id} or error in API response."

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
        return f"Error getting cluster errors: {str(e)}. HINT: Make sure node_status_id is a valid status ID from get_cluster_events."


@mcp.tool(
    description="Lists cluster instances that have been granted access to run jobs or notebooks in a specific workspace. Optionally filter by job_type, cluster_status, and enabled state. Returns a table of cluster details with pagination.",
    annotations={"readOnlyHint": True}
)
async def get_workspace_clusters(
    workspace_id: int = Field(description="Workspace ID to filter clusters"),
    cluster_status: Optional[List[str]] = Field(
        default=None,
        description="List of cluster statuses to filter, can be the following values: INITIATING, RUNNING, STOPPING, STOPPED, DESTROYING, DESTROYED, ERROR, RESIZING UP, RESIZING DOWN."
    ),
    job_type: Optional[str] = Field(
        default=None,
        description="Job types that should be supported by the clusters, can be the following values: Custom Code, Notebook, Thrift SQL, Functions, JAR"
    ),
    enable: Optional[bool] = Field(
        default=None,
        description="True for active/enabled clusters, False for disabled/inactive clusters, None for all"
    ),
    limit: int = Field(
        default=20, ge=1, le=100, description="Number of clusters to return"
    ),
    pageNumber: int = Field(
        default=1,
        description="Page number for pagination"
    )
) -> str:
    """Get clusters which have access to run jobs/notebooks in a workspace, with optional filters by job type, cluster status, enablement, and pagination."""
    try:
        params = {}
        if cluster_status:
            params["cluster_status"] = cluster_status
        if job_type:
            params["job_type"] = job_type
        if enable is not None:
            params["enable"] = str(enable).lower()
        params["limit"] = limit
        params["pageNumber"] = pageNumber

        endpoint = f"/api/v1/ac/workspace/{workspace_id}/clusters"

        async with YeeduClient() as client:
            data = await client.get(endpoint, params=params)

            if not data or "data" not in data:
                return f"No clusters found for workspace {workspace_id}."

            clusters = data["data"]
            cluster_rows = []
            for cluster in clusters:
                cluster_info = cluster.get("cluster_info", {})
                spark_infra_version = cluster_info.get(
                    "spark_infra_version", {})
                cluster_rows.append([
                    cluster_info.get("cluster_id", "N/A"),
                    cluster_info.get("name", "N/A"),
                    cluster_info.get("cluster_status", "N/A"),
                    cluster_info.get("cluster_type", "N/A"),
                    cluster_info.get("instance_size", "N/A"),
                    cluster.get("enable", "N/A"),
                    cluster.get("job_type", "N/A"),
                    spark_infra_version.get("spark_version", "N/A"),
                    spark_infra_version.get("hive_version", "N/A"),
                    spark_infra_version.get("hadoop_version", "N/A"),
                    spark_infra_version.get("scala_version", "N/A"),
                    spark_infra_version.get("python_version", "N/A"),
                    spark_infra_version.get("notebook_support", "N/A"),
                    spark_infra_version.get("has_cuda_support", "N/A"),
                    spark_infra_version.get("thrift_support", "N/A"),
                    spark_infra_version.get("yeedu_functions_support", "N/A"),
                    spark_infra_version.get("has_turbo_support", "N/A"),
                    spark_infra_version.get("turbo_version", "N/A"),
                    spark_infra_version.get("has_unity_support", "N/A"),
                    spark_infra_version.get("unity_version", "N/A"),
                    spark_infra_version.get("has_hive_support", "N/A"),
                    spark_infra_version.get("cuda_rapids_version", "N/A"),
                ])

            table = serialize_table(
                cluster_rows,
                headers=[
                    "cluster_id",
                    "name",
                    "status",
                    "type",
                    "instance_size",
                    "enabled",
                    "job_type",
                    "spark_version",
                    "hive_version",
                    "hadoop_version",
                    "scala_version",
                    "python_version",
                    "notebook_support",
                    "has_cuda_support",
                    "thrift_support",
                    "yeedu_functions_support",
                    "has_turbo_support",
                    "turbo_version",
                    "has_unity_support",
                    "unity_version",
                    "has_hive_support",
                    "cuda_rapids_version"
                ])

            # Add pagination info if available
            result_set = data.get("result_set")
            if result_set:
                pagination_info = (
                    f"\nPage {result_set.get('current_page', pageNumber)} of {result_set.get('total_pages', 1)} | "
                    f"Total Items: {result_set.get('total_objects', 0)} | "
                    f"Items Per Page: {limit}"
                )
                table += pagination_info

            return table

    except Exception as e:
        return f"Error getting workspace clusters: {str(e)}. HINT: Double check if workspace_id is correct. Use list_workspaces to check."


@mcp.tool(
    description="Lists workspaces where a specific cluster has been granted access to run jobs or notebooks. Returns a table showing cluster-to-workspace relationships with pagination.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_workspaces(
    cluster_id: int = Field(description="Cluster ID to filter workspaces"),
    limit: int = Field(
        default=20, ge=1, le=100,
        description="Number of workspaces to return"
    ),
    pageNumber: int = Field(
        default=1,
        description="Page number for pagination"
    )
) -> str:
    """Get workspaces where cluster has access to run jobs/notebooks - shows cluster-to-workspace relationships"""
    try:
        params = {
            "limit": limit,
            "pageNumber": pageNumber
        }

        endpoint = f"/api/v1/ac/cluster/{cluster_id}/workspaces"

        async with YeeduClient() as client:
            data = await client.get(endpoint, params=params)

            if not data or "data" not in data:
                return f"No workspaces found for cluster {cluster_id}."

            workspaces_data = data["data"]
            workspace_rows = []
            for workspace_data in workspaces_data:
                ws = workspace_data.get("workspace", {})
                workspace_rows.append([
                    ws.get("workspace_id", "N/A"),
                    ws.get("name", "N/A"),
                    ws.get("description", "N/A")
                ])

            table = serialize_table(
                workspace_rows,
                headers=[
                    "workspace_id",
                    "name",
                    "description"
                ])

            # Add pagination info if available
            result_set = data.get("result_set")
            if result_set:
                pagination_info = (
                    f"\nPage {result_set.get('current_page', pageNumber)} of {result_set.get('total_pages', 1)} | "
                    f"Total Items: {result_set.get('total_objects', 0)} | "
                    f"Items Per Page: {limit}"
                )
                table += pagination_info

            return table

    except Exception as e:
        return f"Error getting cluster workspaces: {str(e)}. HINT: Ensure cluster_id is valid."


@mcp.tool(
    description="Gets detailed node-level events for a cluster run, including node status, node type, IP addresses, and resource utilization. Returns a table with node event details and pagination.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_node_events(
    cluster_id: int = Field(
        description="Cluster ID to retrieve node events for"),
    limit: int = Field(
        default=100, ge=1, le=100, description="Number of node events to retrieve"
    ),
    page: int = Field(default=1, ge=1, description="Page number to retrieve"),
) -> str:
    """Get detailed node-level events for a cluster including node status, type, IP addresses, and resource utilization"""
    try:
        params = {
            "cluster_id": cluster_id,
            "limit": limit,
            "pageNumber": page
        }

        async with YeeduClient() as client:
            data = await client.get("/api/v1/cluster/node/events", params=params)

            if not data or "data" not in data:
                return f"No node events found for cluster {cluster_id}."

            # Extract relevant fields
            node_events = []
            for event in data["data"]:
                resource_details = event.get("resource_details", {}) or {}
                reason_code = event.get(
                    "workflow_status_reason_code", {}) or {}

                node_events.append([
                    event.get("node_status_id", "N/A"),
                    event.get("node_id", "N/A"),
                    event.get("node_number", "N/A"),
                    event.get("node_ip", "N/A"),
                    event.get("node_type", "N/A"),
                    event.get("node_status", "N/A"),
                    event.get("is_spot_instance", "N/A"),
                    f"{reason_code.get('name', 'N/A')} - {reason_code.get('description', 'N/A')}" if reason_code else "N/A",
                    event.get("start_time", "N/A"),
                    event.get("end_time", "N/A"),
                    resource_details.get("CPU", "N/A"),
                    resource_details.get("MEMORY", "N/A"),
                    resource_details.get("SCRATCH_DISK_USAGE", "N/A"),
                    resource_details.get("NETWORK", "N/A"),
                    resource_details.get("IO_WAIT", "N/A"),
                    resource_details.get("UPTIME", "N/A"),
                ])

            table = serialize_table(
                node_events,
                headers=[
                    "Node Status ID",
                    "Node ID",
                    "Node #",
                    "Node IP",
                    "Node Type",
                    "Status",
                    "Spot Instance",
                    "Reason Code",
                    "Start Time",
                    "End Time",
                    "CPU",
                    "Memory",
                    "Disk Usage",
                    "Network",
                    "IO Wait",
                    "Uptime",
                ])

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
        return f"Error getting cluster node events: {str(e)}. HINT: Ensure cluster_id is valid."


@mcp.tool(
    description="""
    Analyze and fix issues with a cluster by identifying failed/destroyed nodes and their errors. This helps with:
    - Understanding why a cluster entered ERROR/STOPPED states
    - Debugging provisioning/start/resize failures
    - Identifying which specific nodes failed (ERROR) or were destroyed (DESTROYED) and why
    - Reviewing resource configuration and environment
    - Reading relevant logs and system error messages

    Returns a natural language prompt that guides the LLM through a structured cluster diagnosis.
    """,
    annotations={"readOnlyHint": True}
)
async def analyze_cluster_issue(
    cluster_id: int = Field(description="Cluster ID to analyze"),
) -> str:
    """
    Analyze and fix issues with a cluster by identifying failed/destroyed nodes and their errors. This helps with:
    - Understanding why a cluster entered ERROR/STOPPED states
    - Debugging provisioning/start/resize failures
    - Identifying which specific nodes failed (ERROR) or were destroyed (DESTROYED) and why
    - Reviewing resource configuration and environment
    - Reading relevant logs and system error messages

    Returns a natural language prompt that guides the LLM through a structured cluster diagnosis.
    """
    prompt = f"""Diagnose cluster {cluster_id} by identifying nodes in ERROR or DESTROYED state and providing individual diagnosis for each failed node. Execute this systematic analysis:

STEP 1: Collect Cluster Configuration
- Execute get_cluster_details(cluster_id={cluster_id}) to gather:
  - Infrastructure specs (machine type, vCPUs, memory, disk configuration)
  - Scaling settings (min/max instances, instance size, spot instances)
  - Runtime versions (Spark, Hadoop, Hive, Python, Scala)
  - Feature flags (CUDA, Turbo, public IP, SSH keys)
  - Network configurations (VPC, subnets, security groups, etc.)
  - Bootstrap scripts and custom configurations
  - Resource limits and timeouts

STEP 2: Identify Nodes in ERROR or DESTROYED State
- Execute get_cluster_node_events(cluster_id={cluster_id}) to retrieve all node-level events
- **CRITICAL: FILTER STRICTLY - Include ONLY nodes where "Status" column exactly equals "ERROR" or "DESTROYED"**
- **EXCLUDE all other statuses including: TERMINATED, RUNNING, STOPPED, STOPPING, INITIATING, etc.**
- For each ERROR/DESTROYED node, capture:
  - Node Status ID (this is the key identifier for fetching errors)
  - Node ID and Node Number
  - Node Type (master/worker)
  - Node IP
  - Status (ERROR or DESTROYED)
  - Reason Code
  - Resource utilization (CPU, Memory, Disk Usage)
  - Start/End time of the event
- If NO nodes have "Status" = "ERROR" or "DESTROYED", report "No nodes in ERROR or DESTROYED state found" and stop analysis
- DO NOT process or report on nodes with any status other than ERROR or DESTROYED

STEP 3: Individual Node Diagnosis - FOR EACH ERROR/DESTROYED NODE
For EACH node identified in STEP 2, perform separate diagnosis:

3.1 Extract Error Messages for this specific node:
- Execute get_cluster_errors(cluster_id={cluster_id}, node_status_id=<this_node_status_id>)
- Document all error messages for this node

3.2 Extract Diagnostic Logs for this specific node:
- get_cluster_logs(cluster_id={cluster_id}, node_status_id=<this_node_status_id>, log_type="STDERR", last_n_lines=100)
- get_cluster_logs(cluster_id={cluster_id}, node_status_id=<this_node_status_id>, log_type="STDOUT", last_n_lines=100)
- Note: DESTROYED nodes may have partial or no logs depending on how far provisioning progressed before destruction

3.3 Analyze this node's failure:
- Identify root cause specific to this node:
  - Cloud provider errors (quota limits, service unavailability)
  - Network/connectivity failures (firewall, DNS)
  - Authentication/authorization issues
  - Configuration errors
  - Resource exhaustion (CPU, Memory, Disk)
  - Bootstrap script failures
  - Spot instance eviction (common cause for DESTROYED nodes)
  - Intentional or forced node destruction

STEP 4: Cross-Node Pattern Analysis
After analyzing all ERROR and DESTROYED nodes individually, identify patterns:
- Are all nodes failing with the same error? (Suggests cluster-wide config issue)
- Are only specific node types failing? (master vs worker)
- Are failures sequential or simultaneous?
- Is there a cascade pattern? (Did master fail first, then workers?)
- Are DESTROYED nodes related to ERROR nodes? (e.g., spot eviction chain)

STEP 5: Root Cause Classification
Based on individual node analysis and patterns:
- Determine if issue is cluster-wide or node-specific
- Classify as: Configuration Error, Resource Constraint, Network Issue, Authentication Failure, Spot Eviction, or Service Dependency
- Assess if transient (spot eviction) or persistent (config error)
- Distinguish between nodes that failed (ERROR) vs nodes that were destroyed (DESTROYED)

STEP 6: Remediation Strategy - PER NODE
Provide specific fixes for each ERROR/DESTROYED node:
- If same root cause across all nodes: Provide single cluster-level fix
- If different root causes: Provide separate fix for each affected node
- For DESTROYED nodes due to spot eviction: Recommend on-demand fallback or retry strategy

DELIVERABLE: Comprehensive Diagnostic Report
Structure your findings as:

1. Executive Summary
   - Total nodes in ERROR state: <count>
   - Total nodes in DESTROYED state: <count>
   - Brief summary of issue(s)
   - **NOTE: Only nodes with Status="ERROR" or Status="DESTROYED" are included in this report**

2. Individual Node Diagnosis (SEPARATE SECTION FOR EACH ERROR/DESTROYED NODE)
   For each node, provide:
   - Node Details: Node Status ID, Node ID, Node Number, Node Type, Node IP, **Status: ERROR or DESTROYED**
   - Error Timeline: Start time, End time
   - Error Messages: All errors from get_cluster_errors
   - Log Analysis: Key findings from STDERR/STDOUT (if retrieved)
   - Root Cause: Specific cause for THIS node
   - Resource State: CPU, Memory, Disk utilization at time of failure/destruction

3. Pattern Analysis
   - Common patterns across failed/destroyed nodes
   - Cascade failure analysis (ERROR → DESTROYED relationships)
   - Cluster-wide vs node-specific issues

4. Remediation Plan
   - If cluster-wide issue: Single fix with steps
   - If node-specific issues: Separate fix for each node
   - Priority order for fixes
   - Commands or configuration changes needed

Begin analysis immediately. Focus ONLY on ERROR and DESTROYED state nodes. Request additional data only if critical information is missing.
"""

    return prompt

"""Cluster lifecycle action tools — start, stop, destroy, stop_all_jobs, create, update."""

import json
from typing import Dict, List, Optional
from pydantic import Field
from yeedu_mcp_server.client import YeeduClient
from yeedu_mcp_server.utils.formatting import serialize_table
from yeedu_mcp_server.mcp_server import mcp


@mcp.tool(
    description="Fetches all data needed to create a cluster in a single call: (1) cloud environments (cloud_env_id, name, provider, availability zone), (2) cluster configurations (cluster_conf_id, name, machine type, vCPUs, memory, has_cuda), and (3) spark infra versions (version_id, spark/python versions, notebook/CUDA/turbo support). Optionally filter by cloud_provider and architecture_type. Call this before create_cluster.",
    annotations={"readOnlyHint": True}
)
async def get_cluster_prerequisites(
    cloud_provider: Optional[str] = Field(
        default=None, description="Filter by cloud provider: GCP, AWS, or Azure"
    ),
    architecture_type: Optional[str] = Field(
        default="x86_64", description="Machine architecture: x86_64 or arm64"
    ),
) -> str:
    """Fetch cloud environments, cluster configs, and spark infra versions needed to create a cluster."""
    try:
        if cloud_provider and not isinstance(cloud_provider, str):
            cloud_provider = None
        if not architecture_type or not isinstance(architecture_type, str):
            architecture_type = "x86_64"

        async with YeeduClient() as client:
            # 1. Fetch cloud environments
            env_params: Dict = {"pageNumber": 1, "limit": 100}
            if cloud_provider:
                env_params["cloud_provider"] = cloud_provider
            if architecture_type:
                env_params["architecture_type"] = architecture_type

            env_data = await client.get("/api/v1/cluster/cloud/environments", params=env_params)

            # 2. Fetch cluster configurations
            conf_params: Dict = {"all": "true", "pageNumber": 1, "limit": 100}
            if cloud_provider:
                conf_params["cloud_provider"] = cloud_provider
            if architecture_type:
                conf_params["architecture_type"] = architecture_type

            conf_data = await client.get("/api/v1/cluster/confs", params=conf_params)

            # 3. Fetch spark infra versions
            spark_data = await client.get("/api/v1/lookup_spark_infra_version")

            # Format output
            output_lines = []

            # --- Cloud Environments ---
            output_lines.append("=== CLOUD ENVIRONMENTS ===")
            output_lines.append(
                "cloud_env_id,cloud_env_name,cloud_provider,availability_zone")
            if isinstance(env_data, dict) and "data" in env_data:
                for env in env_data["data"]:
                    cp = env.get("cloud_provider", {})
                    az = env.get("availability_zone", {})
                    output_lines.append(
                        f"{env.get('cloud_env_id', 'N/A')},"
                        f"{env.get('cloud_env_name', 'N/A')},"
                        f"{cp.get('name', 'N/A') if isinstance(cp, dict) else cp},"
                        f"{az.get('name', 'N/A') if isinstance(az, dict) else az}"
                    )
            elif isinstance(env_data, dict) and "error_message" in env_data:
                output_lines.append(f"Error: {env_data['error_message']}")
            else:
                output_lines.append("No cloud environments found.")

            output_lines.append("")

            # --- Cluster Configurations ---
            output_lines.append("=== CLUSTER CONFIGURATIONS ===")
            output_lines.append(
                "cluster_conf_id,cluster_conf_name,machine_type,vCPUs,memory,has_cuda")
            if isinstance(conf_data, dict) and "data" in conf_data:
                for conf in conf_data["data"]:
                    mt = conf.get("machine_type", {}) or {}
                    output_lines.append(
                        f"{conf.get('cluster_conf_id', 'N/A')},"
                        f"{conf.get('cluster_conf_name', 'N/A')},"
                        f"{mt.get('name', 'N/A') if isinstance(mt, dict) else mt},"
                        f"{mt.get('vCPUs', 'N/A') if isinstance(mt, dict) else 'N/A'},"
                        f"{mt.get('memory', 'N/A') if isinstance(mt, dict) else 'N/A'},"
                        f"{mt.get('has_cuda', False) if isinstance(mt, dict) else False}"
                    )
            elif isinstance(conf_data, dict) and "error_message" in conf_data:
                output_lines.append(f"Error: {conf_data['error_message']}")
            else:
                output_lines.append("No cluster configurations found.")

            output_lines.append("")

            # --- Spark Infra Versions ---
            output_lines.append("=== SPARK INFRA VERSIONS ===")
            output_lines.append(
                "spark_infra_version_id,spark_version,python_version,notebook_support,cuda_support,turbo_support")
            spark_items = spark_data if isinstance(spark_data, list) else (spark_data.get(
                "data", []) if isinstance(spark_data, dict) and "data" in spark_data else [])
            if spark_items:
                for sv in spark_items:
                    output_lines.append(
                        f"{sv.get('spark_infra_version_id', 'N/A')},"
                        f"{sv.get('spark_version', 'N/A')},"
                        f"{sv.get('python_version', 'N/A')},"
                        f"{sv.get('notebook_support', 'N/A')},"
                        f"{sv.get('has_cuda_support', False)},"
                        f"{sv.get('has_turbo_support', False)}"
                    )
            elif isinstance(spark_data, dict) and "error_message" in spark_data:
                output_lines.append(f"Error: {spark_data['error_message']}")
            else:
                output_lines.append("No spark infra versions found.")

            output_lines.append("")

            return "\n".join(output_lines)

    except Exception as e:
        return f"Error fetching cluster creation prerequisites: {str(e)}. HINT: Check your cloud_provider or connection."





@mcp.tool(
    description="Performs a lifecycle action on a cluster instance: start, stop, destroy, disable, enable, or stop_all_jobs. stop_all_jobs requires cluster_id. disable/enable use PUT, all others POST. Requires cluster_id or cluster_name. Returns CSV: action, cluster_id, cluster_name, status/message.",
    annotations={"destructiveHint": True}
)
async def cluster_action(
    action: str = Field(
        description="Action to perform: start, stop, destroy, disable, enable, stop_all_jobs"
    ),
    cluster_id: Optional[int] = Field(
        default=None, description="Cluster instance ID"
    ),
    cluster_name: Optional[str] = Field(
        default=None, description="Cluster instance name (used if ID not provided)"
    ),
) -> str:
    """Perform lifecycle actions on a cluster: start, stop, destroy, disable, enable, or stop all jobs."""
    try:
        valid_actions = ["start", "stop", "destroy",
                         "disable", "enable", "stop_all_jobs"]
        if action not in valid_actions:
            return f"Error: Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"

        if cluster_id is None and cluster_name is None:
            return "Error: Either cluster_id or cluster_name is required."

        # stop_all_jobs only accepts cluster_id (path param, no body)
        if action == "stop_all_jobs" and cluster_id is None:
            return "Error: cluster_id is required for stop_all_jobs action."

        async with YeeduClient() as client:
            payload = {}
            if cluster_id is not None:
                payload["cluster_id"] = cluster_id
            elif cluster_name:
                payload["cluster_name"] = cluster_name

            if action == "stop_all_jobs":
                data = await client.post(f"/api/v1/cluster/{cluster_id}/stop_all_jobs")
            elif action in ("disable", "enable"):
                # disable and enable use PUT
                data = await client.put(f"/api/v1/cluster/{action}", json=payload)
            else:
                data = await client.post(f"/api/v1/cluster/{action}", json=payload)

            # Format response as CSV
            if isinstance(data, dict):
                if "error_code" in data or "error_message" in data:
                    return f"Error: {data.get('error_message', data.get('error_code', 'Unknown error'))}"

                lines = [f"action,{action}"]
                cid = data.get("cluster_id", cluster_id)
                lines.append(f"cluster_id,{cid}")
                if "name" in data:
                    lines.append(f"cluster_name,{data['name']}")
                if "cluster_status" in data:
                    lines.append(f"status,{data['cluster_status']}")
                if "message" in data:
                    lines.append(f"message,{data['message']}")
                return "\n".join(lines)

            return f"Cluster {action} executed successfully: {data}"

    except Exception as e:
        return f"Error performing cluster {action}: {str(e)}. HINT: Ensure action is one of: start, stop, destroy, disable, enable, stop_all_jobs, and cluster_id/cluster_name is valid."


@mcp.tool(
    description="Use the create_cluster_instructions prompt to guide your inputs before calling this tool to create a cluster. Creates a new cluster instance. Required: name, cloud_env_id, cluster_conf_id, spark_infra_version_id, cluster_type. For CLUSTER type, provide num_of_workers (1-30). Optional: autoscaling (min/max_instances), Spark config, storage (object_storage_manager_id), metastore (metastore_catalog_id), GPU/Turbo flags, bootstrap script, labels, mount_ids. Returns CSV: cluster_id, name, status, cluster_type.",
    annotations={"idempotentHint": True}
)
async def create_cluster(
    name: str = Field(
        description="Name for the new cluster (1-64 chars: a-z, 0-9, ., @, _, -)"),
    cloud_env_id: int = Field(description="Cloud environment ID to use"),
    cluster_conf_id: int = Field(
        description="Cluster configuration ID (machine type + volume)"),
    spark_infra_version_id: int = Field(
        description="Spark infrastructure version ID"),
    cluster_type: str = Field(
        default="YEEDU", description="Cluster type: YEEDU, STANDALONE, or CLUSTER"),
    num_of_workers: Optional[int] = Field(
        default=None, description="Number of worker nodes for CLUSTER type (1-30). Omit for YEEDU/STANDALONE."),
    idle_timeout_ms: int = Field(
        default=240000, description="Idle timeout in milliseconds before auto-stop"),
    description: Optional[str] = Field(
        default=None, description="Cluster description"),
    labels: Optional[Dict[str, str]] = Field(
        default=None, description="Key-value labels for the cluster (max 25)"),
    is_spot_instance: bool = Field(
        default=False, description="Use spot/preemptible instances"),
    enable_public_ip: bool = Field(
        default=False, description="Enable public IP on nodes"),
    min_instances: int = Field(
        default=1, description="Minimum number of instances. Required for YEEDU and STANDALONE types (default 1). Do NOT set for CLUSTER type."),
    max_instances: int = Field(
        default=1, description="Maximum number of instances. Required for YEEDU and STANDALONE types (default 1). Do NOT set for CLUSTER type."),
    is_turbo_enabled: bool = Field(
        default=False, description="Enable Turbo mode (requires spark infra with turbo support)"),
    is_cuda_enabled: bool = Field(
        default=False, description="Enable CUDA/GPU support (requires cluster_conf with has_cuda=true and spark infra with cuda support)"),
    bootstrap_shell_script: Optional[str] = Field(
        default=None, description="Shell script to run on cluster startup"),
    object_storage_manager_id: Optional[int] = Field(
        default=None, description="Object storage manager ID to attach"),
    metastore_catalog_id: Optional[int] = Field(
        default=None, description="Metastore catalog ID to attach"),
    max_parallel_spark_job_execution_per_instance: Optional[int] = Field(
        default=None, description="Max parallel Spark jobs per instance"),
    spark_config: Optional[Dict] = Field(
        default=None, description="Spark configuration: {conf: [], packages: [], jars: [], archives: [], repositories: [], env_var: [], files: [], py-files: []}"),
    mount_ids: Optional[List[int]] = Field(
        default=None, description="List of mount IDs to attach to the cluster"),
) -> str:
    """Create a new cluster instance. Requires cloud_env_id, cluster_conf_id, and spark_infra_version_id."""
    try:
        payload: Dict = {
            "name": name,
            "cloud_env_id": cloud_env_id,
            "cluster_conf_id": cluster_conf_id,
            "spark_infra_version_id": spark_infra_version_id,
            "cluster_type": cluster_type,
            "idle_timeout_ms": idle_timeout_ms,
            "is_spot_instance": is_spot_instance,
            "enable_public_ip": enable_public_ip,
            "is_turbo_enabled": is_turbo_enabled,
            "is_cuda_enabled": is_cuda_enabled,
        }

        if description:
            payload["description"] = description
        if labels:
            payload["labels"] = labels
        # min_instances/max_instances required for YEEDU/STANDALONE, must NOT be sent for CLUSTER
        if cluster_type.upper() != "CLUSTER":
            payload["min_instances"] = min_instances
            payload["max_instances"] = max_instances
        if bootstrap_shell_script:
            payload["bootstrap_shell_script"] = bootstrap_shell_script
        if object_storage_manager_id:
            payload["object_storage_manager_id"] = object_storage_manager_id
        if metastore_catalog_id:
            payload["metastore_catalog_id"] = metastore_catalog_id

        # engine_cluster_spark_config must always be sent — backend reads num_of_workers unconditionally
        # None for YEEDU/STANDALONE
        engine_conf: Dict = {"num_of_workers": num_of_workers}
        if max_parallel_spark_job_execution_per_instance is not None:
            engine_conf["max_parallel_spark_job_execution_per_instance"] = max_parallel_spark_job_execution_per_instance
        payload["engine_cluster_spark_config"] = engine_conf

        if spark_config:
            payload["spark_config"] = spark_config
        if mount_ids:
            payload["mount_ids"] = mount_ids

        async with YeeduClient() as client:
            data = await client.post("/api/v1/cluster", json=payload)

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            # CSV output — CreatedClusterInstance response has no cluster_status field
            lines = ["field,value"]
            lines.append(f"cluster_id,{data.get('cluster_id', 'N/A')}")
            lines.append(f"name,{data.get('name', 'N/A')}")
            lines.append(
                f"cluster_type,{data.get('cluster_type', cluster_type)}")
            lines.append(
                f"cloud_env_id,{data.get('cloud_env_id', cloud_env_id)}")
            lines.append(
                f"cluster_conf_id,{data.get('cluster_conf_id', cluster_conf_id)}")
            lines.append(
                f"spark_infra_version_id,{data.get('spark_infra_version_id', spark_infra_version_id)}")
            lines.append(
                f"min_instances,{data.get('min_instances', min_instances)}")
            lines.append(
                f"max_instances,{data.get('max_instances', max_instances)}")
            lines.append(
                f"idle_timeout_ms,{data.get('idle_timeout_ms', idle_timeout_ms)}")
            lines.append(f"created_at,{data.get('from_date', 'N/A')}")
            lines.append(
                "note,Cluster created — use cluster_action(action='start') to start it")
            return "\n".join(lines)

    except Exception as e:
        return f"Error creating cluster: {str(e)}. HINT: Verify that the name is unique, cloud_env_id, cluster_conf_id, and spark_infra_version_id are valid. Refer to get_cluster_prerequisites for details."


@mcp.tool(
    description="Updates configuration fields of an existing cluster (name, description, idle timeout, num_of_workers, labels, Spark config, autoscaling, bootstrap script, etc.). Only provided fields are changed; omitted fields remain unchanged. Editable in any state: name, description, idle_timeout_ms. Only in DESTROYED/ERROR state: labels, cloud config, spark version, storage, mounts, etc. Returns CSV: cluster_id, name, status.",
    annotations={"idempotentHint": True}
)
async def update_cluster(
    cluster_id: Optional[int] = Field(
        default=None, description="Cluster ID to update"),
    cluster_name: Optional[str] = Field(
        default=None, description="Cluster name to update (if ID not provided)"),
    name: Optional[str] = Field(
        default=None, description="New name for the cluster"),
    description: Optional[str] = Field(
        default=None, description="New description"),
    idle_timeout_ms: Optional[int] = Field(
        default=None, description="New idle timeout in ms"),
    labels: Optional[Dict[str, str]] = Field(
        default=None, description="Updated labels (only in DESTROYED/ERROR state)"),
    is_spot_instance: Optional[bool] = Field(
        default=None, description="Use spot instances (only in DESTROYED/ERROR state)"),
    enable_public_ip: Optional[bool] = Field(
        default=None, description="Enable public IP (only in DESTROYED/ERROR state)"),
    min_instances: Optional[int] = Field(
        default=None, description="Min instances for autoscaling (only in DESTROYED/ERROR state)"),
    max_instances: Optional[int] = Field(
        default=None, description="Max instances for autoscaling (only in DESTROYED/ERROR state)"),
    num_of_workers: Optional[int] = Field(
        default=None, description="Worker node count (1-30, only in DESTROYED/ERROR state)"),
    max_parallel_spark_job_execution_per_instance: Optional[int] = Field(
        default=None, description="Max parallel Spark jobs per instance (only in DESTROYED/ERROR state)"),
    bootstrap_shell_script: Optional[str] = Field(
        default=None, description="New bootstrap script (only in DESTROYED/ERROR state)"),
    spark_config: Optional[Dict] = Field(
        default=None, description="Updated Spark configuration (only in STOPPED/DESTROYED/ERROR state)"),
    cloud_env_id: Optional[int] = Field(
        default=None, description="New cloud environment ID (only in DESTROYED/ERROR state)"),
    cluster_conf_id: Optional[int] = Field(
        default=None, description="New cluster config ID (only in DESTROYED/ERROR state)"),
    spark_infra_version_id: Optional[int] = Field(
        default=None, description="New Spark infra version ID (only in DESTROYED/ERROR state)"),
    object_storage_manager_id: Optional[int] = Field(
        default=None, description="New OSM ID (only in DESTROYED/ERROR state)"),
    metastore_catalog_id: Optional[int] = Field(
        default=None, description="New metastore catalog ID (only in DESTROYED/ERROR state)"),
    mount_ids: Optional[List[int]] = Field(
        default=None, description="Updated list of mount IDs (replaces existing mounts, only in DESTROYED/ERROR state)"),
) -> str:
    """Update cluster configuration. Fields editable in any state: name, description, idle_timeout_ms. Most other fields require DESTROYED/ERROR state."""
    try:
        if cluster_id is None and cluster_name is None:
            return "Error: Either cluster_id or cluster_name is required."

        params: Dict = {}
        if cluster_id is not None:
            params["cluster_id"] = cluster_id
        elif cluster_name:
            params["cluster_name"] = cluster_name

        # Build payload with only provided fields
        payload: Dict = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if idle_timeout_ms is not None:
            payload["idle_timeout_ms"] = idle_timeout_ms
        if labels is not None:
            payload["labels"] = labels
        if is_spot_instance is not None:
            payload["is_spot_instance"] = is_spot_instance
        if enable_public_ip is not None:
            payload["enable_public_ip"] = enable_public_ip
        if min_instances is not None:
            payload["min_instances"] = min_instances
        if max_instances is not None:
            payload["max_instances"] = max_instances
        if bootstrap_shell_script is not None:
            payload["bootstrap_shell_script"] = bootstrap_shell_script
        if spark_config is not None:
            payload["spark_config"] = spark_config
        if cloud_env_id is not None:
            payload["cloud_env_id"] = cloud_env_id
        if cluster_conf_id is not None:
            payload["cluster_conf_id"] = cluster_conf_id
        if spark_infra_version_id is not None:
            payload["spark_infra_version_id"] = spark_infra_version_id
        if object_storage_manager_id is not None:
            payload["object_storage_manager_id"] = object_storage_manager_id
        if metastore_catalog_id is not None:
            payload["metastore_catalog_id"] = metastore_catalog_id
        if mount_ids is not None:
            payload["mount_ids"] = mount_ids

        # Pack engine_cluster_spark_config fields
        engine_conf: Dict = {}
        if num_of_workers is not None:
            engine_conf["num_of_workers"] = num_of_workers
        if max_parallel_spark_job_execution_per_instance is not None:
            engine_conf["max_parallel_spark_job_execution_per_instance"] = max_parallel_spark_job_execution_per_instance
        if engine_conf:
            payload["engine_cluster_spark_config"] = engine_conf

        if not payload:
            return "Error: No fields provided to update."

        async with YeeduClient() as client:
            data = await client.put("/api/v1/cluster", params=params, json=payload)

            if isinstance(data, dict) and "error_message" in data:
                return f"Error: {data['error_message']}"

            lines = ["field,value"]
            lines.append(f"cluster_id,{data.get('cluster_id', cluster_id)}")
            lines.append(f"name,{data.get('name', 'N/A')}")
            lines.append(f"status,{data.get('cluster_status', 'N/A')}")
            lines.append(f"message,Cluster updated successfully")
            return "\n".join(lines)

    except Exception as e:
        return f"Error updating cluster: {str(e)}. HINT: Check if the cluster is in the correct state (DESTROYED/ERROR) to update configuration fields like labels, cloud config, spark version, storage, mounts. Fields like name, description, idle_timeout_ms can be updated in any state."

