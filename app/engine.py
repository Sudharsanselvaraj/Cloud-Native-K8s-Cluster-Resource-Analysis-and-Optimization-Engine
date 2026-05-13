"""
Kubernetes Resource Optimization Engine
Analyzes workload metrics and generates safe, intelligent resource recommendations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ── Configuration ──────────────────────────────────────────────────────────────

@dataclass
class OptimizationConfig:
    """Tunable knobs for the recommendation engine."""

    # Safety buffer applied on top of peak usage
    safety_buffer_pct: float = 0.25          # 25%

    # Minimum threshold ratio to flag overprovisioning (request / usage)
    overprovisioning_ratio: float = 2.0      # flag if request > 2× usage

    # Hard floor values (millicores / MiB) – never go below these
    min_cpu_millicores: int = 50
    min_memory_mib: int = 64

    # Spike headroom multiplier applied over avg when p95 is unavailable
    spike_headroom: float = 1.20             # assume spikes ≈ 20% above avg

    # Downsizing is capped: never recommend below this fraction of current request
    max_reduction_pct: float = 0.60          # can cut at most 60% of request


DEFAULT_CONFIG = OptimizationConfig()


# ── Data Models ────────────────────────────────────────────────────────────────

@dataclass
class WorkloadMetrics:
    deployment: str
    cpu_request: int            # millicores
    cpu_usage_avg: int          # millicores
    memory_request: int         # MiB
    memory_usage_avg: int       # MiB
    cpu_usage_p95: Optional[int] = None     # millicores (optional)
    memory_usage_p95: Optional[int] = None  # MiB (optional)
    replicas: int = 1
    has_hpa: bool = False       # if HPA is attached, be more conservative


@dataclass
class OptimizationResult:
    deployment: str
    recommended_cpu: int        # millicores
    recommended_memory: int     # MiB
    reason: str
    original_cpu_request: int
    original_memory_request: int
    cpu_reduction_pct: float
    memory_reduction_pct: float
    is_overprovisioned: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "deployment": self.deployment,
            "recommended_cpu": self.recommended_cpu,
            "recommended_memory": self.recommended_memory,
            "reason": self.reason,
            "original_cpu_request": self.original_cpu_request,
            "original_memory_request": self.original_memory_request,
            "cpu_reduction_pct": round(self.cpu_reduction_pct, 1),
            "memory_reduction_pct": round(self.memory_reduction_pct, 1),
            "is_overprovisioned": self.is_overprovisioned,
            "warnings": self.warnings,
        }


# ── Recommendation Logic ───────────────────────────────────────────────────────

def _effective_peak(avg: int, p95: Optional[int], spike_headroom: float) -> int:
    """Return the effective peak: p95 if available, else avg * spike_headroom."""
    if p95 is not None:
        return max(avg, p95)
    return math.ceil(avg * spike_headroom)


def _safe_recommend(
    peak: int,
    current_request: int,
    buffer_pct: float,
    floor: int,
    max_reduction_pct: float,
) -> int:
    """
    Compute a safe recommendation:
      1. Start from peak usage.
      2. Add safety buffer.
      3. Clamp to [floor, current_request].
      4. Never reduce below (1 - max_reduction_pct) * current_request.
    """
    raw = math.ceil(peak * (1 + buffer_pct))
    minimum_allowed = math.ceil(current_request * (1 - max_reduction_pct))
    clamped = max(raw, floor, minimum_allowed)
    return min(clamped, current_request)   # never suggest *more* than requested


def analyze_workload(
    metrics: WorkloadMetrics,
    config: OptimizationConfig = DEFAULT_CONFIG,
) -> OptimizationResult:
    """
    Core analysis function: produces a single OptimizationResult for one workload.
    """
    warnings: list[str] = []

    # ── CPU ──────────────────────────────────────────────────────────────────
    cpu_peak = _effective_peak(
        metrics.cpu_usage_avg,
        metrics.cpu_usage_p95,
        config.spike_headroom,
    )
    recommended_cpu = _safe_recommend(
        peak=cpu_peak,
        current_request=metrics.cpu_request,
        buffer_pct=config.safety_buffer_pct,
        floor=config.min_cpu_millicores,
        max_reduction_pct=config.max_reduction_pct,
    )

    # ── Memory ───────────────────────────────────────────────────────────────
    mem_peak = _effective_peak(
        metrics.memory_usage_avg,
        metrics.memory_usage_p95,
        config.spike_headroom,
    )
    recommended_memory = _safe_recommend(
        peak=mem_peak,
        current_request=metrics.memory_request,
        buffer_pct=config.safety_buffer_pct,
        floor=config.min_memory_mib,
        max_reduction_pct=config.max_reduction_pct,
    )

    # ── OOM risk guard ────────────────────────────────────────────────────────
    if metrics.memory_usage_avg / metrics.memory_request > 0.80:
        warnings.append("High memory pressure detected (>80% utilisation). "
                        "Monitor for OOM kills before applying recommendation.")
        # Pull back the recommendation slightly to leave more headroom
        recommended_memory = min(
            metrics.memory_request,
            math.ceil(recommended_memory * 1.10),
        )

    # ── HPA awareness ─────────────────────────────────────────────────────────
    if metrics.has_hpa:
        warnings.append(
            "HPA is active: per-pod requests directly influence scale decisions. "
            "Validate autoscaling behaviour after applying changes."
        )
        # Be a bit more conservative when HPA is present
        recommended_cpu = min(
            metrics.cpu_request,
            math.ceil(recommended_cpu * 1.10),
        )

    # ── Overprovisioning check ─────────────────────────────────────────────────
    cpu_ratio = metrics.cpu_request / max(metrics.cpu_usage_avg, 1)
    mem_ratio = metrics.memory_request / max(metrics.memory_usage_avg, 1)
    is_overprovisioned = (
        cpu_ratio >= config.overprovisioning_ratio
        or mem_ratio >= config.overprovisioning_ratio
    )

    # ── Reduction percentages ─────────────────────────────────────────────────
    cpu_reduction = (1 - recommended_cpu / metrics.cpu_request) * 100
    mem_reduction = (1 - recommended_memory / metrics.memory_request) * 100

    # ── Reason string ─────────────────────────────────────────────────────────
    if not is_overprovisioned:
        reason = "Resource usage is within acceptable range; no significant change recommended."
    else:
        parts = []
        if cpu_ratio >= config.overprovisioning_ratio:
            parts.append(f"CPU request is {cpu_ratio:.1f}× average usage")
        if mem_ratio >= config.overprovisioning_ratio:
            parts.append(f"Memory request is {mem_ratio:.1f}× average usage")
        reason = (
            "Average usage significantly below requested resources"
            if not parts
            else "; ".join(parts) + " — safe to downsize with buffer applied."
        )

    return OptimizationResult(
        deployment=metrics.deployment,
        recommended_cpu=recommended_cpu,
        recommended_memory=recommended_memory,
        reason=reason,
        original_cpu_request=metrics.cpu_request,
        original_memory_request=metrics.memory_request,
        cpu_reduction_pct=max(cpu_reduction, 0),
        memory_reduction_pct=max(mem_reduction, 0),
        is_overprovisioned=is_overprovisioned,
        warnings=warnings,
    )


def run_optimization(
    workloads: list[WorkloadMetrics],
    config: OptimizationConfig = DEFAULT_CONFIG,
) -> list[OptimizationResult]:
    """Analyze a list of workloads; returns only overprovisioned ones."""
    results = [analyze_workload(w, config) for w in workloads]
    return [r for r in results if r.is_overprovisioned]
