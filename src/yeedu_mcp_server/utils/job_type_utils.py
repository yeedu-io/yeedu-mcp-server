"""
Mapping utilities for Yeedu job types and languages.

The API accepts internal codes (e.g. SPARK_JOB, RAW_SCALA) for filter params,
but the UI shows display names (e.g. Custom Code, Raw Scala).  These dicts are
the single source of truth for both directions.
"""

# ---------------------------------------------------------------------------
# Job type: internal code ↔ display name  (1-to-1, safe to invert)
# ---------------------------------------------------------------------------
JOB_TYPE_CODE_TO_DISPLAY: dict[str, str] = {
    "SPARK_JOB":       "Custom Code",
    "NOTEBOOK":        "Notebook",
    "SPARK_SQL":       "Spark SQL",
    "THRIFT_SQL":      "Thrift SQL",
    "YEEDU_FUNCTIONS": "Functions",
    "JAR":             "JAR",
}

JOB_TYPE_DISPLAY_TO_CODE: dict[str, str] = {
    v: k for k, v in JOB_TYPE_CODE_TO_DISPLAY.items()
}


# ---------------------------------------------------------------------------
# Language: display name → internal lang_name  (input / filter params)
# Note: Some display names map to the same lang_name (SQL ambiguity) —
# this is accepted; the filter returns a superset which is still correct.
# ---------------------------------------------------------------------------
JOB_LANG_DISPLAY_TO_CODE: dict[str, str] = {
    "Raw Scala": "RAW_SCALA",
    "PySpark":   "Python3",
    "Python":    "Python3",
    "JAR":       "Jar",
    "Spark SQL": "SQL",
    "Thrift SQL": "SQL",
    # These are already valid codes — kept as explicit pass-throughs for clarity
    "Python3":   "Python3",
    "Scala":     "Scala",
    "SQL":       "SQL",
    "Jar":       "Jar",
    "RAW_SCALA": "RAW_SCALA",
}


# ---------------------------------------------------------------------------
# Language: (job_type_code, lang_name) → display name  (output / table render)
# Based on LOOKUP_SPARK_JOB_TYPE_LANG DB data.
# ---------------------------------------------------------------------------
JOB_LANG_CONTEXT_TO_DISPLAY: dict[tuple[str, str], str] = {
    ("SPARK_JOB",       "RAW_SCALA"): "Raw Scala",
    ("SPARK_JOB",       "Python3"):   "PySpark",
    ("SPARK_JOB",       "SQL"):       "Spark SQL",
    ("NOTEBOOK",        "Python3"):   "Python3",
    ("NOTEBOOK",        "Scala"):     "Scala",
    ("NOTEBOOK",        "SQL"):       "SQL",
    ("THRIFT_SQL",      "SQL"):       "Thrift SQL",
    ("YEEDU_FUNCTIONS", "Python3"):   "Python",
    ("JAR",             "Jar"):       "JAR",
}


def display_job_type(code: str) -> str:
    """Return the display name for a job type code, falling back to the code itself."""
    return JOB_TYPE_CODE_TO_DISPLAY.get(code, code)


def display_job_lang(job_type_code: str, lang_name: str) -> str:
    """Return the display name for a (job_type, lang_name) pair."""
    return JOB_LANG_CONTEXT_TO_DISPLAY.get((job_type_code, lang_name), lang_name)
