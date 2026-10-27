"""Response formatters for all tool return values.

serialize_table() is verbatim from forgeai-agents/server/utils/formatting.py.
format_record(), format_mutation_success(), format_error() are new helpers that
enforce the response contract from design doc §7.
"""

import csv
import io


def serialize_table(data: list, headers: list[str]) -> str:
    """Serialize tabular data as pipe-delimited rows (header + values, no padding).

    A row with a 500-character value in one column does not pad every other row
    to match — this was the core fix over the tabulate approach.
    """
    if not data:
        return "No results found."
    output = io.StringIO()
    writer = csv.writer(output, delimiter="|", lineterminator="\n")
    writer.writerow(headers)
    if isinstance(data[0], dict):
        writer.writerows([row.get(c, "") for c in headers] for row in data)
    else:
        writer.writerows(data)
    return output.getvalue()


def format_record(data: dict) -> str:
    """Single-record response: Key: value lines.

    Used for get-by-id responses where returning a table makes no sense.
    """
    if not data:
        return "No record found."
    return "\n".join(f"{k}: {v}" for k, v in data.items())


def format_mutation_success(
    entity: str,
    name: str,
    action: str,
    entity_id_label: str,
    entity_id,
) -> str:
    """Mutation success response.

    Format: <Entity> '<name>' <actioned> successfully. <entity_id_label>: <id>
    Example: Cluster 'my-cluster' started successfully. cluster_id: 42
    """
    return f"{entity} '{name}' {action} successfully. {entity_id_label}: {entity_id}"


def format_error(message: str, hint: str) -> str:
    """Error response with self-correction hint.

    The hint is the most important part — it enables the LLM to recover without
    a full retry loop by telling it exactly what to do next.

    Example:
        format_error(
            "Cluster ID 99 not found",
            "Use manage_cluster(action='list') to retrieve valid cluster IDs."
        )
    """
    return f"Error: {message}. Hint: {hint}"
