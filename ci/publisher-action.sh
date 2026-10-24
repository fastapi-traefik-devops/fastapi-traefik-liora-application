#!/bin/sh
# Trusted entrypoint; no application checkout code runs with write credentials.
set -eu
set +x
: "${GIT_TOKEN:?Git token required}"
helpers=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
credentials=$(mktemp -d)
trap 'rm -rf "$credentials"' EXIT HUP INT TERM
cat > "$credentials/askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *) printf '%s\n' "$GIT_TOKEN" ;;
esac
ASKPASS
chmod 700 "$credentials/askpass"
export GIT_ASKPASS="$credentials/askpass" GIT_TERMINAL_PROMPT=0
if [ "${1:-}" = accept-stage ]; then
    exec_status=0
    python3 "$helpers/promote.py" accept-stage stage-acceptance.json || exec_status=$?
    exit "$exec_status"
fi
# Explicit exits: after the trap is set, bash-as-sh turns a failed ${VAR:?} into status 0.
for required in EXPECTED_SOURCE_SHA GHCR_USER GHCR_TOKEN ACTION; do
    eval "[ -n \"\${$required:-}\" ]" || { echo "$required required" >&2; exit 1; }
done
# Each write action has exactly one allowed source; PROD has no publisher action.
case "$ACTION:${SOURCE_JOB:-}" in publish-only:*|stage:app-ci/main|dev:dev-build) ;; *) exit 1 ;; esac
if [ "$ACTION" = stage ]; then
    # Skip obsolete candidates before publication as well as immediately before Git writes.
    latest=$(git ls-remote --exit-code https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-application.git refs/heads/main)
    latest=$(printf '%s\n' "$latest" | cut -f1)
    if [ "$latest" != "$EXPECTED_SOURCE_SHA" ]; then
        printf '{"status":"superseded"}\n' > stage-request.json
        exit 0
    fi
fi
export DOCKER_CONFIG="$credentials/docker"
mkdir -p "$DOCKER_CONFIG"
# Registry Python authenticates and removes its own private config for publication.
# Main and DEV builds are published only with a release attestation (scans included);
# functional-only candidates are accepted solely from the manual migration checkpoint.
case "${SOURCE_JOB:-}" in app-ci/main|dev-build) mode=release ;; migration-ci) mode=functional ;; *) exit 1 ;; esac
python3 "$helpers/registry.py" artifacts/.ci-output --mode "$mode" --source-job "$SOURCE_JOB" --source-build "$CI_BUILD"
[ "$ACTION" != publish-only ] || exit 0
printf '%s' "$GHCR_TOKEN" | crane auth login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null 2>&1
python3 "$helpers/promote.py" "$ACTION" "$EXPECTED_SOURCE_SHA" --release "${RELEASE_TAG:-}"
