---
name: docker-compose
description: docker-compose.yml patterns for the Build agent — local dev orchestration of the app plus its backing services (DB, cache, vector store), env handling, and healthcheck-gated startup. Use when generating a local development environment for the planned stack.
---

# Docker Compose (Build Agent Output Contract)

Compose files the Build agent generates are for **local development parity** with
the cloud stack — one service per component in architecture.md.

## Baseline Pattern

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-app}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-app}
      POSTGRES_DB: ${POSTGRES_DB:-app}
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER:-app}"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  db_data:
```

## Rules

- **Mirror the plan**: each managed cloud service gets a local stand-in
  (RDS → postgres, ElastiCache → redis, OpenSearch/vector DB → qdrant, S3 → minio).
- **Secrets via `.env`** with `${VAR:-default}` interpolation; always emit a
  matching `.env.example`. Never commit real values.
- **Healthchecks on stateful services**, and `depends_on.condition:
  service_healthy` on their consumers, so `docker compose up` works first try.
- **Named volumes** for anything stateful; no bind mounts for data.
- **Pin image tags** to a major/minor version, never `latest`.
- No `version:` top-level key (obsolete in Compose v2).
- Keep it one file; overlays (`compose.override.yml`) only if the plan
  explicitly calls for prod-like local profiles.

## Common Local Stand-ins

| Cloud service        | Local image              |
|----------------------|--------------------------|
| RDS PostgreSQL       | `postgres:16-alpine`     |
| ElastiCache Redis    | `redis:7-alpine`         |
| S3                   | `minio/minio`            |
| SQS/SNS              | `localstack/localstack`  |
| Vector DB            | `qdrant/qdrant`          |
| Prometheus/Grafana   | `prom/prometheus`, `grafana/grafana` |

## Related Skills

- `dockerfile` — the app image this compose file builds
- `terraform-scaffold` — the cloud equivalent of these services
