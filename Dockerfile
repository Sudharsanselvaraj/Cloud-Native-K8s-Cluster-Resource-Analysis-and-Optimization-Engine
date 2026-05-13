# ── Stage 1: build / test ─────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY . .

# Run tests at build time — fail fast if tests break
RUN PYTHONPATH=/app python -m pytest tests/ -q

# ── Stage 2: production image ─────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="k8s-resource-optimizer" \
      org.opencontainers.image.description="Kubernetes workload resource optimization engine" \
      org.opencontainers.image.version="1.0.0"

# Non-root user for security
RUN addgroup --system optimizer && adduser --system --ingroup optimizer optimizer

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /app /app

USER optimizer

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    SAFETY_BUFFER_PCT=0.25 \
    OVERPROV_RATIO=2.0 \
    MIN_CPU_MILLICORES=50 \
    MIN_MEMORY_MIB=64 \
    SPIKE_HEADROOM=1.20 \
    MAX_REDUCTION_PCT=0.60

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

CMD ["python", "main.py", "--serve", "--port", "8080"]
