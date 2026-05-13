"""
FastAPI REST API for the Kubernetes Resource Optimization Engine.
Exposes:
  POST /optimize          – analyze workloads and return recommendations
  GET  /health            – liveness probe
  GET  /metrics           – Prometheus exposition (optional)
  GET  /config            – current engine config
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.engine import OptimizationConfig, run_optimization
from app.parser import ParseError, parse_metrics

# ── Optional Prometheus integration ──────────────────────────────────────────
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    PROMETHEUS_ENABLED = True
except ImportError:
    PROMETHEUS_ENABLED = False

if PROMETHEUS_ENABLED:
    REQUEST_COUNT = Counter(
        "optimizer_requests_total",
        "Total optimization requests",
        ["status"],
    )
    WORKLOADS_ANALYZED = Counter(
        "optimizer_workloads_analyzed_total",
        "Total workloads analyzed",
    )
    OVERPROVISIONED_FOUND = Counter(
        "optimizer_overprovisioned_total",
        "Overprovisioned workloads detected",
    )
    REQUEST_LATENCY = Histogram(
        "optimizer_request_latency_seconds",
        "Optimization request latency",
        buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    )
    LAST_RUN_TIMESTAMP = Gauge(
        "optimizer_last_run_timestamp_seconds",
        "Unix timestamp of the last optimization run",
    )


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="K8s Resource Optimization API",
    description=(
        "Analyzes Kubernetes workload metrics and recommends optimized "
        "CPU / memory requests to reduce overprovisioning."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class WorkloadInput(BaseModel):
    deployment: str
    cpu_request: int = Field(..., ge=0, description="CPU request in millicores")
    cpu_usage_avg: int = Field(..., ge=0, description="Average CPU usage in millicores")
    memory_request: int = Field(..., ge=0, description="Memory request in MiB")
    memory_usage_avg: int = Field(..., ge=0, description="Average memory usage in MiB")
    cpu_usage_p95: Optional[int] = Field(None, ge=0, description="P95 CPU usage in millicores")
    memory_usage_p95: Optional[int] = Field(None, ge=0, description="P95 memory usage in MiB")
    replicas: int = Field(1, ge=1)
    has_hpa: bool = False

    class Config:
        json_schema_extra = {
            "example": {
                "deployment": "api-service",
                "cpu_request": 1000,
                "cpu_usage_avg": 180,
                "memory_request": 2048,
                "memory_usage_avg": 700,
            }
        }


class OptimizeRequest(BaseModel):
    workloads: list[WorkloadInput]
    config: Optional[dict] = None   # optional config overrides


class OptimizeResponse(BaseModel):
    recommendations: list[dict]
    total_workloads: int
    overprovisioned_count: int
    analysis_duration_ms: float


# ── Config from environment ───────────────────────────────────────────────────

def _build_config(override: Optional[dict]) -> OptimizationConfig:
    cfg = OptimizationConfig(
        safety_buffer_pct=float(os.getenv("SAFETY_BUFFER_PCT", 0.25)),
        overprovisioning_ratio=float(os.getenv("OVERPROV_RATIO", 2.0)),
        min_cpu_millicores=int(os.getenv("MIN_CPU_MILLICORES", 50)),
        min_memory_mib=int(os.getenv("MIN_MEMORY_MIB", 64)),
        spike_headroom=float(os.getenv("SPIKE_HEADROOM", 1.20)),
        max_reduction_pct=float(os.getenv("MAX_REDUCTION_PCT", 0.60)),
    )
    if override:
        for k, v in override.items():
            if hasattr(cfg, k):
                setattr(cfg, k, type(getattr(cfg, k))(v))
    return cfg


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok", "prometheus": PROMETHEUS_ENABLED}


@app.get("/config", tags=["ops"])
def get_config():
    """Return the current default configuration."""
    from dataclasses import asdict
    return asdict(_build_config(None))


@app.post("/optimize", response_model=OptimizeResponse, tags=["optimization"])
async def optimize(request: OptimizeRequest):
    """
    Analyze workload metrics and return optimization recommendations.

    Only workloads that are overprovisioned (above the configured ratio)
    are included in the output.
    """
    start = time.perf_counter()
    config = _build_config(request.config)

    raw = [w.model_dump() for w in request.workloads]
    try:
        workloads = parse_metrics(raw)
    except ParseError as exc:
        if PROMETHEUS_ENABLED:
            REQUEST_COUNT.labels(status="error").inc()
        raise HTTPException(status_code=422, detail=str(exc))

    results = run_optimization(workloads, config)
    duration_ms = (time.perf_counter() - start) * 1000

    if PROMETHEUS_ENABLED:
        REQUEST_COUNT.labels(status="success").inc()
        WORKLOADS_ANALYZED.inc(len(workloads))
        OVERPROVISIONED_FOUND.inc(len(results))
        REQUEST_LATENCY.observe(duration_ms / 1000)
        LAST_RUN_TIMESTAMP.set(time.time())

    return OptimizeResponse(
        recommendations=[r.to_dict() for r in results],
        total_workloads=len(workloads),
        overprovisioned_count=len(results),
        analysis_duration_ms=round(duration_ms, 3),
    )


@app.get("/metrics", tags=["ops"])
def metrics():
    """Prometheus metrics endpoint."""
    if not PROMETHEUS_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Prometheus client not installed. Run: pip install prometheus-client",
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
