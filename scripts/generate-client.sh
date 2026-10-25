#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  uv_bin=uv
elif [[ -x .tools/phase1/uv ]]; then
  uv_bin=.tools/phase1/uv
else
  echo "uv is required to generate the API client" >&2
  exit 1
fi
UV_PROJECT_ENVIRONMENT="$PWD/.tools/phase1/backend-venv" \
  "$uv_bin" run --directory backend --locked python ../ci/generate_openapi.py > frontend/openapi.json
if command -v npm >/dev/null 2>&1; then
  (
    cd frontend
    npm ci
    npm run generate-client
  )
else
  node_image="$(python3 -c 'import json; print(json.load(open("ci/images.lock.json"))["node:24.21.0-bookworm-slim"])')"
  docker run --rm --user "$(id -u):$(id -g)" --cap-drop ALL \
    --security-opt no-new-privileges:true --memory 2g --cpus 2 \
    -e HOME=/tmp -e NPM_CONFIG_CACHE=/tmp/npm-cache \
    -v "$PWD/frontend:/app" -w /app "$node_image" \
    sh -ec 'npm ci && npm run generate-client'
fi
