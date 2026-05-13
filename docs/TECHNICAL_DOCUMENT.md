# Technical Document — Kubernetes Resource Optimization Agent
### OptOps.AI Assignment Submission | Sudharsan S

---

## 1. Problem Understanding

Kubernetes workloads are commonly overprovisioned — teams set conservative CPU and memory requests at deployment time and rarely revisit them. This creates two problems:

- **Resource waste**: nodes are logically "full" even when actual utilization is 10–20%.
- **Cost inflation**: cloud cost correlates directly with requested resources, not actual usage.

The goal is to build an engine that ingests workload metrics and generates *safe* downsizing recommendations — not just a ratio calculation, but an approach that accounts for traffic spikes, OOM risks, autoscaling interactions, and configuration flexibility.

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│                  k8s-optimizer                         │
│                                                        │
│  ┌──────────┐   ┌──────────┐   ┌───────────────────┐  │
│  │  Parser  │──▶│  Engine  │──▶│  Output / API     │  │
│  │          │   │          │   │                   │  │
│  │ Validates│   │ Analyzes │   │ CLI table / JSON  │  │
│  │ JSON     │   │ metrics  │   │ FastAPI REST       │  │
│  │ input    │   │ generates│   │ Prometheus metrics │  │
│  │          │   │ recs     │   │                   │  │
│  └──────────┘   └──────────┘   └───────────────────┘  │
└────────────────────────────────────────────────────────┘
```

Three focused modules, zero framework magic:

| Module | File | Responsibility |
|---|---|---|
| Parser | `app/parser.py` | Input validation, type coercion, error aggregation |
| Engine | `app/engine.py` | Optimization algorithm, safety logic, result generation |
| API | `app/api.py` | FastAPI REST layer, Prometheus integration, config management |
| CLI | `main.py` | File/stdin mode, table display, server mode entrypoint |

---

## 3. Recommendation Algorithm

### 3.1 Naive approach (rejected)

A naive implementation would do:

```
recommended = avg_usage × 1.2
```

This is dangerous:
- No floor — can recommend < 50m CPU or < 64Mi memory
- Ignores traffic spikes entirely
- Ignores OOM risk
- Ignores HPA coupling
- No cap — could cut 95% of a request in one shot

### 3.2 Production-safe approach (implemented)

The algorithm runs in 5 stages:

**Stage 1 — Determine effective peak**

```python
if p95_usage is available:
    peak = max(avg_usage, p95_usage)
else:
    peak = ceil(avg_usage × SPIKE_HEADROOM)  # default: 1.20×
```

Using p95 is strictly better — it captures burst behaviour. When unavailable, a 20% headroom over average approximates spike tolerance.

**Stage 2 — Add safety buffer**

```python
raw = ceil(peak × (1 + SAFETY_BUFFER_PCT))  # default: 25%
```

This ensures we never recommend *exactly* the p95 value — there must be breathing room above observed peak.

**Stage 3 — Enforce floors and reduction cap**

```python
minimum_allowed = ceil(current_request × (1 − MAX_REDUCTION_PCT))  # default: 40% of request
recommendation = max(raw, floor, minimum_allowed)
recommendation = min(recommendation, current_request)
```

Two safety nets:
- **Floor**: never go below 50m CPU / 64Mi memory — below these, containers become unstable.
- **Reduction cap**: never recommend cutting more than 60% in one pass. This is intentional — resource tuning should be iterative, not a cliff.

**Stage 4 — OOM guard**

```python
if memory_usage_avg / memory_request > 0.80:
    recommendation = min(current_request, ceil(recommendation × 1.10))
    emit warning("High memory pressure detected...")
```

When a workload is using >80% of its requested memory, it is close to OOM-kill territory. The recommendation is padded by an extra 10% and a warning is surfaced.

**Stage 5 — HPA awareness**

```python
if has_hpa:
    cpu_recommendation = min(current_request, ceil(cpu_recommendation × 1.10))
    emit warning("HPA is active: per-pod requests influence scaling...")
```

When a HorizontalPodAutoscaler is attached, per-pod CPU request is the denominator for scale-down decisions. Cutting too aggressively could cause the HPA to scale down prematurely under load.

---

## 4. Input Schema

```json
{
  "deployment": "api-service",         // required: string
  "cpu_request": 1000,                 // required: int (millicores)
  "cpu_usage_avg": 180,                // required: int (millicores)
  "memory_request": 2048,              // required: int (MiB)
  "memory_usage_avg": 700,             // required: int (MiB)
  "cpu_usage_p95": 320,                // optional: int (millicores) — enables accurate peak detection
  "memory_usage_p95": 900,             // optional: int (MiB)
  "replicas": 3,                       // optional: int (default 1)
  "has_hpa": false                     // optional: bool (default false) — triggers conservative CPU recs
}
```

### Validation logic

- All required fields must be present — errors are aggregated across all records (not fail-fast on first error).
- All numeric fields must be non-negative.
- `deployment` must be a non-empty string.
- Invalid records are reported by index with clear messages.

---

## 5. Output Schema

```json
{
  "deployment": "api-service",
  "recommended_cpu": 400,
  "recommended_memory": 1050,
  "reason": "CPU request is 5.6× average usage — safe to downsize with buffer applied.",
  "original_cpu_request": 1000,
  "original_memory_request": 2048,
  "cpu_reduction_pct": 60.0,
  "memory_reduction_pct": 48.7,
  "is_overprovisioned": true,
  "warnings": []
}
```

Only workloads exceeding the overprovisioning ratio (default: 2.0×) appear in output. The `warnings` array surfaces actionable risk signals without blocking the recommendation.

---

## 6. Configuration

All parameters are tunable without code changes:

| Parameter | Default | Rationale |
|---|---|---|
| `SAFETY_BUFFER_PCT` | 25% | Industry-standard headroom above observed peak |
| `OVERPROV_RATIO` | 2.0× | Request > 2× usage is a clear signal |
| `MIN_CPU_MILLICORES` | 50m | Below 50m, containers become latency-sensitive |
| `MIN_MEMORY_MIB` | 64Mi | Below 64Mi, JVM / Python runtimes are unstable |
| `SPIKE_HEADROOM` | 1.20 | Estimated burst = 20% above average |
| `MAX_REDUCTION_PCT` | 60% | Max single-pass cut — forces iterative adoption |

Per-request overrides are also supported via the API `config` field.

---

## 7. API Design

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/optimize` | Submit workload batch for analysis |
| `GET` | `/health` | Liveness probe (used by Kubernetes) |
| `GET` | `/config` | Inspect active configuration |
| `GET` | `/metrics` | Prometheus metrics exposition |

### Design decisions

- **Batch input** — processes multiple workloads in one call; avoids per-workload network overhead.
- **422 on validation failure** — FastAPI/HTTP standard for payload errors.
- **Config override per request** — enables A/B testing different buffer strategies without redeployment.
- **Prometheus disabled gracefully** — if `prometheus-client` is not installed, `/metrics` returns 503 with a helpful message; the rest of the API is unaffected.

---

## 8. Prometheus Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `optimizer_requests_total` | Counter | `status` | Total API calls (success/error) |
| `optimizer_workloads_analyzed_total` | Counter | — | Cumulative workloads processed |
| `optimizer_overprovisioned_total` | Counter | — | Overprovisioned workloads detected |
| `optimizer_request_latency_seconds` | Histogram | — | Processing time per request |
| `optimizer_last_run_timestamp_seconds` | Gauge | — | Timestamp of most recent run |

These metrics support dashboarding (Grafana), alerting (alert if latency p99 > 1s), and audit trails.

---

## 9. Testing Strategy

**21 unit tests** across two test classes:

| Class | Coverage |
|---|---|
| `TestParser` | Valid input, missing fields, negative values, invalid JSON, empty array, non-array input, optional fields, Python list input, multiple error aggregation |
| `TestEngine` | Overprovisioning detection, near-capacity pass-through, safety buffer application, floor enforcement, max reduction cap, p95 usage, OOM warning, HPA warning, batch filtering, custom config, assignment sample |

Key design: the assignment's exact sample input (`api-service` / `worker-service`) is a named test case — it would catch any regression in the core algorithm.

---

## 10. Deployment (Kubernetes-Native)

`k8s/deployment.yaml` includes:

- **Namespace isolation** — `k8s-optimizer` namespace.
- **ConfigMap** — all tunable parameters externalized, no secrets in image.
- **Deployment** — 2 replicas with `runAsNonRoot: true` security context.
- **Resources** — self-optimized: 100m CPU / 128Mi memory request, 500m / 256Mi limit.
- **Liveness + Readiness probes** — `/health` endpoint, standard Kubernetes lifecycle.
- **Prometheus annotations** — `prometheus.io/scrape: "true"` for auto-discovery.
- **HPA** — scales 1–5 pods at 70% CPU utilization.

---

## 11. Discussion: Extending to Real Clusters at Scale

This section answers the assignment's additional discussion question.

### 11.1 Metrics Collection

The current system accepts static JSON. In a real cluster:

- **Metrics Server** — provides real-time CPU/memory via `kubectl top`. Queryable via the Kubernetes Metrics API.
- **Prometheus + kube-state-metrics** — enables historical queries, percentile computation (p95, p99), and time-windowed averages via PromQL.
- **VPA (Vertical Pod Autoscaler)** — can be run in `Off` mode to generate recommendations only, without applying them. Our engine could cross-validate against VPA's suggestions.

### 11.2 Kubernetes API Integration

Replace JSON file input with a live cluster data source:

```python
from kubernetes import client, config

config.load_incluster_config()  # or load_kube_config() for local dev
v1 = client.AppsV1Api()
deployments = v1.list_namespaced_deployment(namespace="production")
```

For each deployment, fetch metrics via the `metrics.k8s.io` API or query Prometheus directly.

### 11.3 Real-Time vs Batch Recommendations

| Mode | Use Case |
|---|---|
| **Batch (current)** | Daily/weekly optimization report; safe for human review |
| **Event-triggered** | Recompute on new deployment or config change |
| **Continuous** | Watch loop via Kubernetes Informers; low-latency recommendations |

For production, a Kubernetes Operator pattern is ideal — the engine runs as a controller, watches `Deployment` objects, and surfaces recommendations as Kubernetes Events or a custom `OptimizationReport` CRD.

### 11.4 Scalability Considerations

- **Multi-cluster**: federated Prometheus or Thanos for unified metrics; cluster label in all metrics.
- **Throughput**: the current engine processes 1000 workloads in ~5ms. At cluster scale (10k deployments), parallelism with `asyncio` or worker pools would maintain sub-second latency.
- **Historical data**: store recommendations in a time-series DB (InfluxDB / TimescaleDB) to track adoption and validate that recommendations held under load.

### 11.5 Reliability

- **Idempotent recommendations** — the same input always produces the same output. Safe to retry.
- **Dry-run mode** — never apply changes directly; emit recommendations for human approval or GitOps pipeline integration.
- **Change velocity control** — the `MAX_REDUCTION_PCT` cap is already present. A production extension would add a "cooldown period" per deployment (don't re-recommend within 7 days of last change).
- **Rollback awareness** — integrate with deployment history to detect if a previous optimization caused a pod crash loop.

---

## 12. What I Would Build Next

1. **CRD-based recommendations** — `OptimizationReport` custom resource for GitOps integration.
2. **Slack/PagerDuty alerts** — notify teams when high-reduction opportunities are found.
3. **Percentile-based p95 query** — pull directly from Prometheus: `histogram_quantile(0.95, ...)`.
4. **Grafana dashboard** — visualize savings over time, recommendation adoption rate.
5. **Cost estimation** — attach cloud pricing API (AWS Savings Plans / GCP pricing) to quantify ₹/$ savings per recommendation.

---

*Submitted by: Sudharsan S | SRMIST Trichy | ML Engineer & Agentic AI Engineer*
