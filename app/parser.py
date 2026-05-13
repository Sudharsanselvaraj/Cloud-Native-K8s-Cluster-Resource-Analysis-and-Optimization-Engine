"""
Input parsing and validation for workload metrics.
"""

from __future__ import annotations

import json
from typing import Any

from app.engine import WorkloadMetrics


class ParseError(ValueError):
    """Raised when input cannot be parsed into valid WorkloadMetrics."""


REQUIRED_FIELDS = {"deployment", "cpu_request", "cpu_usage_avg", "memory_request", "memory_usage_avg"}


def _validate_record(record: Any, idx: int) -> WorkloadMetrics:
    if not isinstance(record, dict):
        raise ParseError(f"Record [{idx}] must be a JSON object, got {type(record).__name__}.")

    missing = REQUIRED_FIELDS - record.keys()
    if missing:
        raise ParseError(f"Record [{idx}] ('{record.get('deployment', '?')}') is missing fields: {missing}.")

    # Type + sanity checks
    for field in ("cpu_request", "cpu_usage_avg", "memory_request", "memory_usage_avg"):
        val = record[field]
        if not isinstance(val, (int, float)) or val < 0:
            raise ParseError(
                f"Record [{idx}] field '{field}' must be a non-negative number, got {val!r}."
            )

    if not isinstance(record["deployment"], str) or not record["deployment"].strip():
        raise ParseError(f"Record [{idx}] 'deployment' must be a non-empty string.")

    return WorkloadMetrics(
        deployment=record["deployment"].strip(),
        cpu_request=int(record["cpu_request"]),
        cpu_usage_avg=int(record["cpu_usage_avg"]),
        memory_request=int(record["memory_request"]),
        memory_usage_avg=int(record["memory_usage_avg"]),
        cpu_usage_p95=record.get("cpu_usage_p95"),
        memory_usage_p95=record.get("memory_usage_p95"),
        replicas=int(record.get("replicas", 1)),
        has_hpa=bool(record.get("has_hpa", False)),
    )


def parse_metrics(raw: str | list) -> list[WorkloadMetrics]:
    """Parse a JSON string or a Python list into WorkloadMetrics objects."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Invalid JSON: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, list):
        raise ParseError("Input must be a JSON array of workload objects.")

    if len(data) == 0:
        raise ParseError("Input array is empty; nothing to analyse.")

    workloads: list[WorkloadMetrics] = []
    errors: list[str] = []
    for idx, record in enumerate(data):
        try:
            workloads.append(_validate_record(record, idx))
        except ParseError as exc:
            errors.append(str(exc))

    if errors:
        raise ParseError("Validation errors:\n" + "\n".join(f"  • {e}" for e in errors))

    return workloads
