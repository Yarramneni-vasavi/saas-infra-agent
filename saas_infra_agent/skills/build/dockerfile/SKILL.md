---
name: dockerfile
description: Dockerfile patterns for the Build agent — multi-stage builds, non-root users, healthchecks, and image slimming for Python/Node app layers. Use whenever the architecture plan implies a containerized application or service layer.
---

# Dockerfile (Build Agent Output Contract)

## Baseline Pattern (Python service)

```dockerfile
# ---- build stage ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Node equivalent: `node:22-slim`, `npm ci --omit=dev` in the build stage, run as
the built-in `node` user.

## Rules

- **Multi-stage always** — build deps never ship in the runtime image.
- **Pin the base image** to a specific minor version tag, never `latest`.
- **Non-root user** in the runtime stage.
- **No secrets** in the image: no ENV with API keys, no copied `.env`. Secrets
  arrive at runtime (compose env_file, ECS task secrets, k8s secretRef).
- **Always emit a `.dockerignore`** alongside (at minimum: `.git`, `.env*`,
  `__pycache__`, `node_modules`, `*.md`, `infra/`).
- **HEALTHCHECK** on every service image — the orchestrator and monitoring
  stack depend on it.
- Copy dependency manifests before source so the dependency layer caches.

## GPU / ML Workloads

- Base on the framework's CUDA runtime image (e.g. `pytorch/pytorch:*-cuda*-runtime`),
  not `-devel`, unless the plan requires compilation.
- Model weights are volumes or downloaded at start — never baked into the image.

## Related Skills

- `docker-compose` — local orchestration of the images this skill produces
- `kubernetes-manifests` — when the deployment target is k8s
