# Architecture: Local Dev Stack — Notes API

## Overview
A small notes-taking API for local development and demos. Deployment target:
**Docker Compose** (no cloud resources, no Terraform).

## Stack
- **App**: Python 3.11 FastAPI service, served by uvicorn on port 8000.
  Source lives in `./app` (assume `app/main.py` exposes `app`).
- **Database**: PostgreSQL 16 container with a named volume for data.
- **Cache**: Redis 7 container.

## Constraints
- One `Dockerfile` for the app and one `docker-compose.yml` wiring app,
  postgres, and redis together.
- The app reads `DATABASE_URL` and `REDIS_URL` from the environment.
- No secrets baked into images or compose — use environment variables with
  an `.env.example` documenting them.
- `docker compose up` must work out of the box with sensible defaults.

## Out of scope
- Kubernetes manifests, Terraform, cloud deployment.
