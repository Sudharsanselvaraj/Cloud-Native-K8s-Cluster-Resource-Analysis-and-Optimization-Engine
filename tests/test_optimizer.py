"""
Unit tests for the K8s Resource Optimization Engine.
Run with: pytest tests/ -v
"""

import json
import pytest

from app.engine import (
    OptimizationConfig,
    WorkloadMetrics,
    analyze_workload,
    run_optimization,
)
from app.parser import ParseError, parse_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def overprovisioned_cpu():
    return WorkloadMetrics(
        deployment="api-service",
        cpu_request=1000,
        cpu_usage_avg=180,
        memory_request=2048,
        memory_usage_avg=700,
    )


@pytest.fixture
def near_capacity():
    return WorkloadMetrics(
        deployment="worker-service",
        cpu_request=500,
        cpu_usage_avg=450,
        memory_request=1024,
        memory_usage_avg=900,
    )


@pytest.fixture
def default_config():
    return OptimizationConfig()


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestParser:
    def test_valid_input(self):
        raw = json.dumps([
            {
                "deployment": "svc",
                "cpu_request": 500,
                "cpu_usage_avg": 100,
                "memory_request": 512,
                "memory_usage_avg": 200,
            }
        ])
        workloads = parse_metrics(raw)
        assert len(workloads) == 1
        assert workloads[0].deployment == "svc"

    def test_missing_required_field(self):
        raw = json.dumps([{"deployment": "svc", "cpu_request": 100}])
        with pytest.raises(ParseError, match="missing fields"):
            parse_metrics(raw)

    def test_negative_value(self):
        raw = json.dumps([{
            "deployment": "svc",
            "cpu_request": -1,
            "cpu_usage_avg": 50,
            "memory_request": 512,
            "memory_usage_avg": 100,
        }])
        with pytest.raises(ParseError):
            parse_metrics(raw)

    def test_invalid_json(self):
        with pytest.raises(ParseError, match="Invalid JSON"):
            parse_metrics("{not valid}")

    def test_empty_array(self):
        with pytest.raises(ParseError, match="empty"):
            parse_metrics("[]")

    def test_not_array(self):
        with pytest.raises(ParseError, match="array"):
            parse_metrics('{"deployment": "svc"}')

    def test_optional_p95_fields(self):
        raw = json.dumps([{
            "deployment": "svc",
            "cpu_request": 500,
            "cpu_usage_avg": 100,
            "memory_request": 512,
            "memory_usage_avg": 200,
            "cpu_usage_p95": 150,
            "memory_usage_p95": 250,
        }])
        workloads = parse_metrics(raw)
        assert workloads[0].cpu_usage_p95 == 150
        assert workloads[0].memory_usage_p95 == 250

    def test_python_list_input(self):
        data = [{
            "deployment": "svc",
            "cpu_request": 500,
            "cpu_usage_avg": 100,
            "memory_request": 512,
            "memory_usage_avg": 200,
        }]
        workloads = parse_metrics(data)
        assert len(workloads) == 1

    def test_multiple_errors_reported(self):
        raw = json.dumps([
            {"deployment": "ok", "cpu_request": 100, "cpu_usage_avg": 50, "memory_request": 256, "memory_usage_avg": 100},
            {"cpu_request": 100},   # missing deployment + memory fields
        ])
        with pytest.raises(ParseError):
            parse_metrics(raw)


# ── Engine tests ──────────────────────────────────────────────────────────────

class TestEngine:
    def test_overprovisioned_detected(self, overprovisioned_cpu, default_config):
        result = analyze_workload(overprovisioned_cpu, default_config)
        assert result.is_overprovisioned is True

    def test_near_capacity_not_flagged(self, near_capacity, default_config):
        result = analyze_workload(near_capacity, default_config)
        assert result.is_overprovisioned is False

    def test_recommendation_less_than_request(self, overprovisioned_cpu, default_config):
        result = analyze_workload(overprovisioned_cpu, default_config)
        assert result.recommended_cpu <= overprovisioned_cpu.cpu_request
        assert result.recommended_memory <= overprovisioned_cpu.memory_request

    def test_safety_buffer_applied(self, default_config):
        # 120m avg * 1.20 spike * 1.25 buffer = 180m → must be >= avg
        m = WorkloadMetrics(
            deployment="test", cpu_request=1000, cpu_usage_avg=100,
            memory_request=1024, memory_usage_avg=300,
        )
        result = analyze_workload(m, default_config)
        assert result.recommended_cpu >= m.cpu_usage_avg

    def test_floor_values_respected(self, default_config):
        m = WorkloadMetrics(
            deployment="tiny",
            cpu_request=200, cpu_usage_avg=5,
            memory_request=128, memory_usage_avg=10,
        )
        result = analyze_workload(m, default_config)
        assert result.recommended_cpu >= default_config.min_cpu_millicores
        assert result.recommended_memory >= default_config.min_memory_mib

    def test_max_reduction_cap(self, default_config):
        m = WorkloadMetrics(
            deployment="bloated",
            cpu_request=10000, cpu_usage_avg=1,
            memory_request=8192, memory_usage_avg=1,
        )
        result = analyze_workload(m, default_config)
        min_cpu = int(m.cpu_request * (1 - default_config.max_reduction_pct))
        min_mem = int(m.memory_request * (1 - default_config.max_reduction_pct))
        assert result.recommended_cpu >= min_cpu
        assert result.recommended_memory >= min_mem

    def test_p95_used_when_available(self, default_config):
        m_no_p95 = WorkloadMetrics(
            deployment="svc", cpu_request=1000, cpu_usage_avg=100,
            memory_request=1024, memory_usage_avg=200,
        )
        m_with_p95 = WorkloadMetrics(
            deployment="svc", cpu_request=1000, cpu_usage_avg=100,
            memory_request=1024, memory_usage_avg=200,
            cpu_usage_p95=400,
        )
        r1 = analyze_workload(m_no_p95, default_config)
        r2 = analyze_workload(m_with_p95, default_config)
        # Higher p95 → higher recommendation
        assert r2.recommended_cpu >= r1.recommended_cpu

    def test_oom_warning_high_memory_pressure(self, default_config):
        m = WorkloadMetrics(
            deployment="oom-risk",
            cpu_request=1000, cpu_usage_avg=100,
            memory_request=1024, memory_usage_avg=850,  # >80%
        )
        result = analyze_workload(m, default_config)
        assert any("OOM" in w or "memory" in w.lower() for w in result.warnings)

    def test_hpa_warning(self, default_config):
        m = WorkloadMetrics(
            deployment="scaled-svc",
            cpu_request=1000, cpu_usage_avg=100,
            memory_request=1024, memory_usage_avg=200,
            has_hpa=True,
        )
        result = analyze_workload(m, default_config)
        assert any("HPA" in w for w in result.warnings)

    def test_run_optimization_filters_correctly(self):
        workloads_data = [
            {"deployment": "over", "cpu_request": 1000, "cpu_usage_avg": 100,
             "memory_request": 2048, "memory_usage_avg": 200},
            {"deployment": "fine", "cpu_request": 500, "cpu_usage_avg": 450,
             "memory_request": 1024, "memory_usage_avg": 900},
        ]
        workloads = parse_metrics(workloads_data)
        results = run_optimization(workloads)
        assert len(results) == 1
        assert results[0].deployment == "over"

    def test_custom_config_overprovisioning_ratio(self):
        config = OptimizationConfig(overprovisioning_ratio=3.0)
        m = WorkloadMetrics(
            deployment="svc",
            cpu_request=500, cpu_usage_avg=200,  # ratio = 2.5 < 3.0
            memory_request=1024, memory_usage_avg=400,
        )
        result = analyze_workload(m, config)
        assert result.is_overprovisioned is False

    def test_sample_input_from_assignment(self):
        """Reproduces the exact sample from the assignment spec."""
        raw = json.dumps([
            {"deployment": "api-service", "cpu_request": 1000, "cpu_usage_avg": 180,
             "memory_request": 2048, "memory_usage_avg": 700},
            {"deployment": "worker-service", "cpu_request": 500, "cpu_usage_avg": 450,
             "memory_request": 1024, "memory_usage_avg": 900},
        ])
        workloads = parse_metrics(raw)
        results = run_optimization(workloads)
        assert len(results) == 1
        assert results[0].deployment == "api-service"
        # Recommended values must be lower than originals
        assert results[0].recommended_cpu < 1000
        assert results[0].recommended_memory < 2048
