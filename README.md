# K8s Resource Optimization Engine

A lightweight service that analyzes Kubernetes workload resource usage and recommends optimized CPU and memory requests — with safety buffers, OOM protection, HPA awareness, and Prometheus metrics.

---

## Quick Start

### CLI (no dependencies except Python 3.10+)

```bash
python main.py --input sample_input.json
```

### FastAPI server

```bash
pip install -r requirements.txt
python main.py --serve --port 8080
# → http://localhost:8080/docs
```

### Docker

```bash
docker build -t k8s-optimizer:1.0.0 .
docker run -p 8080:8080 k8s-optimizer:1.0.0
```

### Docker Compose (with optional Prometheus)

```bash
docker-compose up optimizer
# with monitoring stack:
docker-compose --profile monitoring up
```

### Kubernetes

```bash
kubectl apply -f k8s/deployment.yaml
kubectl -n k8s-optimizer port-forward svc/k8s-optimizer 8080:80
```

---

## Project Structure

```
k8s-optimizer/
├── app/
│   ├── engine.py      # Core recommendation logic
│   ├── parser.py      # Input validation
│   └── api.py         # FastAPI REST layer
├── tests/
│   └── test_optimizer.py   # 21 unit tests
├── k8s/
│   ├── deployment.yaml     # K8s Deployment, Service, HPA
│   └── prometheus.yml      # Prometheus scrape config
├── main.py            # CLI + server entrypoint
├── sample_input.json
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## API Reference

### `POST /optimize`

```json
{
  "workloads": [
    {
      "deployment": "api-service",
      "cpu_request": 1000,
      "cpu_usage_avg": 180,
      "memory_request": 2048,
      "memory_usage_avg": 700,
      "cpu_usage_p95": 320,
      "memory_usage_p95": 900,
      "replicas": 3,
      "has_hpa": false
    }
  ]
}
```

**Response:**

```json
{
  "recommendations": [
    {
      "deployment": "api-service",
      "recommended_cpu": 450,
      "recommended_memory": 1125,
      "reason": "CPU request is 5.6× average usage ...",
      "original_cpu_request": 1000,
      "original_memory_request": 2048,
      "cpu_reduction_pct": 55.0,
      "memory_reduction_pct": 45.1,
      "is_overprovisioned": true,
      "warnings": []
    }
  ],
  "total_workloads": 1,
  "overprovisioned_count": 1,
  "analysis_duration_ms": 0.412
}
```

### `GET /health` — Liveness probe  
### `GET /config` — Active configuration  
### `GET /metrics` — Prometheus exposition  

---

## Configuration

All knobs are configurable via environment variables or per-request JSON override:

| Variable | Default | Description |
|---|---|---|
| `SAFETY_BUFFER_PCT` | `0.25` | Buffer added on top of peak usage (25%) |
| `OVERPROV_RATIO` | `2.0` | Flag if request > N× usage |
| `MIN_CPU_MILLICORES` | `50` | Hard floor for CPU recommendation |
| `MIN_MEMORY_MIB` | `64` | Hard floor for memory recommendation |
| `SPIKE_HEADROOM` | `1.20` | Spike multiplier when p95 is unavailable |
| `MAX_REDUCTION_PCT` | `0.60` | Cap: never cut more than 60% of a request |

---

## Recommendation Algorithm

```
1. Determine effective peak
   peak = p95_usage  (if available)
         OR avg_usage × SPIKE_HEADROOM

2. Apply safety buffer
   raw_recommendation = ceil(peak × (1 + SAFETY_BUFFER_PCT))

3. Enforce floors and reduction caps
   minimum_allowed = ceil(current_request × (1 − MAX_REDUCTION_PCT))
   recommendation = max(raw, floor, minimum_allowed)
   recommendation = min(recommendation, current_request)

4. OOM guard
   If memory_usage_avg / memory_request > 0.80:
     recommendation × 1.10 + emit warning

5. HPA guard
   If has_hpa = true:
     cpu_recommendation × 1.10 + emit warning
```

---

## Running Tests

```bash
pytest tests/ -v
# 21 tests, ~0.04s
```

---

## Assumptions Made

1. **Millicores for CPU, MiB for memory** — standard Kubernetes units.
2. **Average usage** is assumed to be available (required field). P95 is optional but preferred for accuracy.
3. **Safety buffer of 25%** above peak — tunable. Errs on the side of caution.
4. **Max 60% reduction** in a single pass — prevents aggressive, risky cuts in one shot. Intended for iterative adoption.
5. **OOM detection threshold at 80%** — above this, the engine adds an extra 10% headroom and emits a warning.
6. **HPA awareness** — when `has_hpa=true`, CPU recommendation is padded by 10% since per-pod CPU drives scale-down decisions.
7. **Only overprovisioned workloads are returned** in the default output — workloads within ratio are considered healthy.

---

## Bonus: Prometheus Metrics

| Metric | Type | Description |
|---|---|---|
| `optimizer_requests_total` | Counter | Total API requests by status |
| `optimizer_workloads_analyzed_total` | Counter | Total workloads processed |
| `optimizer_overprovisioned_total` | Counter | Overprovisioned workloads detected |
| `optimizer_request_latency_seconds` | Histogram | Request processing latency |
| `optimizer_last_run_timestamp_seconds` | Gauge | Unix timestamp of last run |

---

## Sample Input / Output

**Input:** `sample_input.json`  
**Output:**

```json
[
  {
    "deployment": "api-service",
    "recommended_cpu": 400,
    "recommended_memory": 1050,
    "reason": "CPU request is 5.6× average usage; Memory request is 2.9× average usage — safe to downsize with buffer applied.",
    "original_cpu_request": 1000,
    "original_memory_request": 2048,
    "cpu_reduction_pct": 60.0,
    "memory_reduction_pct": 48.7,
    "is_overprovisioned": true,
    "warnings": []
  }
]
```
