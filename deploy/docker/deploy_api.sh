#!/usr/bin/env bash

set -euo pipefail

readonly PROJECT_DIR="/home/dev1/Desktop/data/features/feature_atta/Velo_claim"
readonly COMPOSE_FILE="${PROJECT_DIR}/deploy/docker/compose.api.yaml"

cd "${PROJECT_DIR}"

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
    echo "Backend environment file not found: ${PROJECT_DIR}/.env" >&2
    exit 1
fi

if ! docker inspect velo-claim-api >/dev/null 2>&1; then
    echo "Stopping any manually launched Velo Claim API..."
    pkill -f '[u]vicorn velo_claim.api.app:app --host 127.0.0.1 --port 8000' || true
fi

echo "Building and starting the persistent API container..."
docker compose -f "${COMPOSE_FILE}" up -d --build --remove-orphans

echo "Waiting for the API container health check..."
for attempt in $(seq 1 30); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' velo-claim-api)"
    if [[ "${status}" == "healthy" ]]; then
        docker ps --filter name=velo-claim-api --format 'table {{.Names}}\t{{.Status}}'
        exit 0
    fi
    if [[ "${status}" == "unhealthy" || "${status}" == "exited" ]]; then
        docker logs --tail 100 velo-claim-api
        exit 1
    fi
    sleep 2
done

echo "API health check timed out." >&2
docker logs --tail 100 velo-claim-api
exit 1
