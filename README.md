<div align="center">

<img src="Assets/k8s.png" height="80" alt="Kubernetes"/>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
<img src="Assets/pngegg.png" height="80" alt="Docker"/>

# K8s Resource Optimization Engine

**Analyze · Detect · Recommend — safely.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docker.com)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-Native-326CE5?style=flat-square&logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![Prometheus](https://img.shields.io/badge/Prometheus-Metrics-E6522C?style=flat-square&logo=prometheus&logoColor=white)](https://prometheus.io)
[![Tests](https://img.shields.io/badge/Tests-21%20Passing-2ea44f?style=flat-square&logo=pytest&logoColor=white)](./tests)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

A lightweight, production-safe service that ingests Kubernetes workload metrics and generates
intelligent CPU & memory right-sizing recommendations — with safety buffers, OOM protection,
HPA awareness, and Prometheus observability built in.

</div>

---

## Table of Contents

- [Problem Statement](#problem-statement)
- [Architecture](#architecture)
- [Recommendation Algorithm](#recommendation-algorithm)
- [Quick Start](#quick-start)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Prometheus Metrics](#prometheus-metrics)
- [Testing](#testing)
- [Assumptions](#assumptions)
- [Extending to Real Clusters](#extending-to-real-clusters)

---

## Problem Statement

Kubernetes workloads are routinely overprovisioned — teams set conservative resource requests
at deploy time and rarely revisit them. This creates two cascading problems:

```
  Overprovisioned Request
          │
          ▼
  ┌───────────────────────────────────────┐
  │  Node appears "full"                  │  ← Scheduler can't place new pods
  │  Cloud bill climbs                    │  ← Paying for idle capacity
  │  Cluster utilisation tanks (10–20%)   │  ← Engineering team unaware
  └───────────────────────────────────────┘
```

This engine inverts that: it reads actual usage, applies safety-first math, and outputs
downsizing recommendations that are safe to apply in production.

---

## Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                    k8s-resource-optimizer                       │
  │                                                                 │
  │   JSON / REST                                                   │
  │   Input ──────▶  ┌──────────┐   ┌──────────┐   ┌───────────┐  │
  │                  │  Parser  │──▶│  Engine  │──▶│  Output   │  │
  │                  │          │   │          │   │           │  │
  │                  │ Validate │   │ 5-Stage  │   │ CLI Table │  │
  │                  │ Coerce   │   │ Algorithm│   │ JSON File │  │
  │                  │ Aggregate│   │          │   │ REST API  │  │
  │                  │ Errors   │   │          │   │ /metrics  │  │
  │                  └──────────┘   └──────────┘   └───────────┘  │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

### Module Map

```
k8s-optimizer/
├── app/
│   ├── engine.py          ← Core optimization algorithm
│   ├── parser.py          ← Input validation & type coercion
│   └── api.py             ← FastAPI REST layer + Prometheus
├── tests/
│   └── test_optimizer.py  ← 21 unit tests (parser + engine)
├── k8s/
│   ├── deployment.yaml    ← Namespace, Deployment, Service, HPA
│   └── prometheus.yml     ← Scrape config
├── Assets/
│   ├── k8s.png
│   └── dockerlogo.png
├── main.py                ← CLI entrypoint + --serve mode
├── sample_input.json
├── sample_output.json
├── Dockerfile             ← Multi-stage, non-root, tests at build
├── docker-compose.yml     ← App + optional Prometheus profile
└── requirements.txt
```

---

## Recommendation Algorithm

The engine runs a **5-stage pipeline** per workload — not a naive ratio calculation.

```
  ┌────────────────────────────────────────────────────────────────────┐
  │                  5-Stage Recommendation Pipeline                   │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                    │
  │  Stage 1 ── Effective Peak                                         │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  p95_usage available? ──YES──▶ peak = max(avg, p95)         │  │
  │  │         │                                                    │  │
  │  │         NO                                                   │  │
  │  │         ▼                                                    │  │
  │  │  peak = ceil(avg × SPIKE_HEADROOM)    [default: 1.20×]      │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                         │                                          │
  │                         ▼                                          │
  │  Stage 2 ── Safety Buffer                                          │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  raw = ceil(peak × (1 + SAFETY_BUFFER_PCT))  [default: 25%] │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                         │                                          │
  │                         ▼                                          │
  │  Stage 3 ── Floors & Reduction Cap                                 │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  min_allowed = ceil(request × (1 − MAX_REDUCTION_PCT))      │  │
  │  │  rec = max(raw, floor, min_allowed)                         │  │
  │  │  rec = min(rec, current_request)   ← never go UP            │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                         │                                          │
  │                         ▼                                          │
  │  Stage 4 ── OOM Guard                                              │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  mem_usage / mem_request > 0.80?                            │  │
  │  │    YES ──▶ rec × 1.10  +  emit warning                      │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                         │                                          │
  │                         ▼                                          │
  │  Stage 5 ── HPA Awareness                                          │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  has_hpa = true?                                            │  │
  │  │    YES ──▶ cpu_rec × 1.10  +  emit warning                  │  │
  │  │    (per-pod CPU request drives HPA scale-down decisions)    │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                         │                                          │
  │                         ▼                                          │
  │               OptimizationResult                                   │
  └────────────────────────────────────────────────────────────────────┘
```

### Why not a simple ratio?

| Naive approach | This engine |
|---|---|
| `rec = avg × 1.2` | Peak-aware (avg or p95) |
| Can recommend < 50m CPU | Hard floor enforcement |
| No spike protection | Configurable spike headroom |
| Ignores OOM risk | OOM guard at 80% memory pressure |
| Ignores HPA coupling | HPA-aware CPU padding |
| Can cut 95% in one pass | Max 60% reduction cap (iterative-safe) |

---

## Quick Start

### Option 1 — CLI (no dependencies)

```bash
# Analyze a file
python main.py --input sample_input.json

# Write JSON output
python main.py --input sample_input.json --output recommendations.json

# Include all workloads (not just overprovisioned)
python main.py --input sample_input.json --all

# Read from stdin
cat sample_input.json | python main.py --input -
```

**CLI output:**

```
────────────────────────────────────────────────────────────────────────────────
DEPLOYMENT             CPU REQ   CPU REC   MEM REQ   MEM REC  REASON
────────────────────────────────────────────────────────────────────────────────
api-service             1000m      400m     2048Mi    1050Mi  CPU 5.6× avg usage...
batch-processor         2000m      800m     4096Mi    1639Mi  CPU 6.7× avg usage...
────────────────────────────────────────────────────────────────────────────────
  2 workload(s) flagged as overprovisioned.
```

### Option 2 — FastAPI Server

```bash
pip install -r requirements.txt
python main.py --serve --port 8080

# Interactive docs
open http://localhost:8080/docs
```

### Option 3 — Docker

```bash
# Build (tests run at build time — fails fast on breakage)
docker build -t k8s-optimizer:1.0.0 .

# Run
docker run -p 8080:8080 k8s-optimizer:1.0.0

# With custom config
docker run -p 8080:8080 \
  -e SAFETY_BUFFER_PCT=0.30 \
  -e OVERPROV_RATIO=1.5 \
  k8s-optimizer:1.0.0
```

### Option 4 — Docker Compose

```bash
# App only
docker-compose up optimizer

# App + Prometheus monitoring
docker-compose --profile monitoring up
```

---

## API Reference

### `POST /optimize`

Submit a batch of workload metrics for analysis.

**Request body:**

```json
{
  "workloads": [
    {
      "deployment":       "api-service",
      "cpu_request":      1000,
      "cpu_usage_avg":    180,
      "memory_request":   2048,
      "memory_usage_avg": 700,
      "cpu_usage_p95":    320,
      "memory_usage_p95": 900,
      "replicas":         3,
      "has_hpa":          false
    }
  ],
  "config": {
    "safety_buffer_pct": 0.30
  }
}
```

**Response:**

```json
{
  "recommendations": [
    {
      "deployment":              "api-service",
      "recommended_cpu":         400,
      "recommended_memory":      1050,
      "reason":                  "CPU request is 5.6× average usage — safe to downsize.",
      "original_cpu_request":    1000,
      "original_memory_request": 2048,
      "cpu_reduction_pct":       60.0,
      "memory_reduction_pct":    48.7,
      "is_overprovisioned":      true,
      "warnings":                []
    }
  ],
  "total_workloads":       2,
  "overprovisioned_count": 1,
  "analysis_duration_ms":  0.412
}
```

> Only overprovisioned workloads appear in `recommendations`. Healthy workloads are silently
> passed through. Use `--all` in CLI mode to see everything.

### Other endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe — returns `{"status": "ok"}` |
| `GET` | `/config` | Inspect active configuration values |
| `GET` | `/metrics` | Prometheus exposition (requires `prometheus-client`) |
| `GET` | `/docs` | Interactive Swagger UI |

---

## Configuration

All parameters are tunable via **environment variables**, without code changes.
They can also be overridden **per-request** via the `config` field in the API body.

| Variable | Default | Description |
|---|---|---|
| `SAFETY_BUFFER_PCT` | `0.25` | Safety headroom above peak usage (25%) |
| `OVERPROV_RATIO` | `2.0` | Flag workload if `request / usage ≥ N` |
| `MIN_CPU_MILLICORES` | `50` | Hard floor — never recommend below 50m |
| `MIN_MEMORY_MIB` | `64` | Hard floor — never recommend below 64Mi |
| `SPIKE_HEADROOM` | `1.20` | Spike multiplier when p95 is unavailable |
| `MAX_REDUCTION_PCT` | `0.60` | Cap single-pass cut at 60% of request |

**Example — aggressive cost-saving mode:**

```bash
docker run -p 8080:8080 \
  -e SAFETY_BUFFER_PCT=0.15 \
  -e OVERPROV_RATIO=1.5  \
  -e MAX_REDUCTION_PCT=0.70 \
  k8s-optimizer:1.0.0
```

---

## Kubernetes Deployment

```
  ┌─────────────────────────────────────────────────────────┐
  │  Namespace: k8s-optimizer                               │
  │                                                         │
  │  ┌──────────────┐        ┌──────────────────────────┐   │
  │  │  ConfigMap   │───────▶│  Deployment              │   │
  │  │  (env tuning)│        │  replicas: 2             │   │
  │  └──────────────┘        │  runAsNonRoot: true      │   │
  │                          │  liveness:  /health      │   │
  │                          │  readiness: /health      │   │
  │                          │  Prometheus annotations  │   │
  │                          └────────────┬─────────────┘   │
  │                                       │                  │
  │                          ┌────────────▼─────────────┐   │
  │                          │  Service (ClusterIP :80) │   │
  │                          └────────────┬─────────────┘   │
  │                                       │                  │
  │                          ┌────────────▼─────────────┐   │
  │                          │  HPA                     │   │
  │                          │  min: 1  max: 5          │   │
  │                          │  target CPU: 70%         │   │
  │                          └──────────────────────────┘   │
  └─────────────────────────────────────────────────────────┘
```

```bash
# Deploy
kubectl apply -f k8s/deployment.yaml

# Verify
kubectl -n k8s-optimizer get pods
kubectl -n k8s-optimizer get hpa

# Access locally
kubectl -n k8s-optimizer port-forward svc/k8s-optimizer 8080:80
```

---

## Prometheus Metrics

Enable the monitoring stack:

```bash
docker-compose --profile monitoring up
# Prometheus UI → http://localhost:9090
```

| Metric | Type | Labels | Description |
|---|---|---|---|
| `optimizer_requests_total` | Counter | `status` | Total API requests (success / error) |
| `optimizer_workloads_analyzed_total` | Counter | — | Cumulative workloads processed |
| `optimizer_overprovisioned_total` | Counter | — | Overprovisioned workloads found |
| `optimizer_request_latency_seconds` | Histogram | — | Per-request processing time |
| `optimizer_last_run_timestamp_seconds` | Gauge | — | Unix timestamp of last optimization run |

**Sample PromQL queries:**

```promql
# Error rate
rate(optimizer_requests_total{status="error"}[5m])

# Average latency
rate(optimizer_request_latency_seconds_sum[5m])
  / rate(optimizer_request_latency_seconds_count[5m])

# Detection rate
rate(optimizer_overprovisioned_total[1h])
```

---

## Testing

```bash
# Run all 21 tests
pytest tests/ -v
```

**Test coverage:**

```
TestParser (9 tests)                     TestEngine (12 tests)
─────────────────────────────────        ─────────────────────────────────────────
✓ valid JSON input                       ✓ overprovisioned workload detected
✓ missing required fields                ✓ near-capacity workload not flagged
✓ negative field value                   ✓ recommendation ≤ original request
✓ invalid JSON string                    ✓ safety buffer applied above avg
✓ empty array input                      ✓ floor values always respected
✓ non-array input                        ✓ max reduction cap enforced
✓ optional p95 fields parsed             ✓ p95 yields higher recommendation
✓ Python list input (not string)         ✓ OOM warning at >80% memory pressure
✓ multiple errors aggregated             ✓ HPA warning when has_hpa=true
                                         ✓ batch filtering (only overprovisioned)
                                         ✓ custom config ratio respected
                                         ✓ assignment sample input reproduces spec
```

---

## Sample Input / Output

**Input (`sample_input.json`):**

```json
[
  { "deployment": "api-service",     "cpu_request": 1000, "cpu_usage_avg": 180,  "memory_request": 2048, "memory_usage_avg": 700  },
  { "deployment": "worker-service",  "cpu_request": 500,  "cpu_usage_avg": 450,  "memory_request": 1024, "memory_usage_avg": 900  },
  { "deployment": "batch-processor", "cpu_request": 2000, "cpu_usage_avg": 300,  "memory_request": 4096, "memory_usage_avg": 512,
    "cpu_usage_p95": 600, "memory_usage_p95": 800 },
  { "deployment": "ml-inference",    "cpu_request": 4000, "cpu_usage_avg": 3800, "memory_request": 8192, "memory_usage_avg": 7500,
    "has_hpa": true }
]
```

**Output (`sample_output.json`):**

```json
[
  {
    "deployment": "api-service",
    "recommended_cpu": 400,
    "recommended_memory": 1050,
    "reason": "CPU request is 5.6× average usage; Memory request is 2.9× average usage — safe to downsize.",
    "cpu_reduction_pct": 60.0,
    "memory_reduction_pct": 48.7,
    "is_overprovisioned": true,
    "warnings": []
  },
  {
    "deployment": "batch-processor",
    "recommended_cpu": 800,
    "recommended_memory": 1639,
    "reason": "CPU request is 6.7× average usage; Memory request is 8.0× average usage — safe to downsize.",
    "cpu_reduction_pct": 60.0,
    "memory_reduction_pct": 60.0,
    "is_overprovisioned": true,
    "warnings": []
  }
]
```

> `worker-service` and `ml-inference` are not returned — both are within the healthy utilisation range.

---

## Assumptions

1. **Units** — CPU in millicores (`m`), memory in MiB. Standard Kubernetes units throughout.
2. **Required fields** — `deployment`, `cpu_request`, `cpu_usage_avg`, `memory_request`, `memory_usage_avg` must be present. All others are optional enhancements.
3. **p95 optional but preferred** — when unavailable, a 20% spike headroom is applied over average.
4. **Safety buffer = 25%** — above the effective peak. Errs on the side of caution; tunable.
5. **Max 60% cut per pass** — prevents cliff-edge downsizing. Designed for iterative adoption over multiple optimization cycles.
6. **OOM threshold = 80%** — memory utilisation above 80% triggers extra headroom and a surfaced warning.
7. **HPA padding** — when `has_hpa=true`, CPU recommendation is padded 10% because per-pod CPU request is the denominator for HPA scale-down decisions.
8. **Only overprovisioned workloads in output** — workloads within the healthy ratio are considered fine and excluded from results.

---

## Extending to Real Clusters

```
  Real-Cluster Integration Architecture
  ══════════════════════════════════════

  ┌─────────────┐   metrics.k8s.io    ┌──────────────────┐
  │  Metrics    │◀────────────────────│  k8s-optimizer   │
  │  Server     │                     │  (Operator mode) │
  └─────────────┘                     └────────┬─────────┘
                                               │
  ┌─────────────┐   PromQL queries             │
  │  Prometheus │◀────────────────────────────-│
  │  + kube-    │                              │
  │  state-mets │                              │
  └─────────────┘                              │
                                               │ kubectl patch /
  ┌─────────────┐   OptimizationReport CRD     │ GitOps PR
  │  Kubernetes │◀────────────────────────────-┘
  │  API Server │
  └─────────────┘
```

Key extension points:

- **Live metrics** via `kubernetes` Python client + `metrics.k8s.io` API, or direct Prometheus PromQL for p95/p99 over a rolling 7-day window.
- **Kubernetes Operator** — watch `Deployment` objects via Informers; emit recommendations as Kubernetes Events or a custom `OptimizationReport` CRD.
- **Multi-cluster** — federated Prometheus (Thanos) with a `cluster` label on all metrics.
- **GitOps integration** — recommendations open a PR against the Helm values file instead of patching directly.
- **Change velocity control** — cooldown period per deployment (don't re-recommend within 7 days of last change).
- **Cost attribution** — attach cloud pricing API (AWS/GCP) to quantify `$` savings per recommendation.

---

<div align="center">

Built for the **OptOps.AI** Software Engineering Internship Assignment

*Sudharsan S · SRMIST Trichy · ML Engineer & Agentic AI Engineer*

</div>
