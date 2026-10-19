#!/usr/bin/env bash
# Exercise the production container restrictions without any external credentials.
set -euo pipefail
[[ $# -eq 2 ]] || { printf 'Usage: container-smoke.sh IMAGE REVISION\n' >&2; exit 1; }
image=$1
revision=$2
name="fullish-smoke-$$"
cleanup() {
    result=$?
    trap - EXIT
    if (( result != 0 )); then docker logs "$name" || true; fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    exit "$result"
}
trap cleanup EXIT
docker run -d --name "$name" --read-only --init \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --cap-drop ALL --security-opt no-new-privileges:true "$image" >/dev/null
healthy=false
for _ in $(seq 1 45); do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$name")
    if [[ $status == healthy ]]; then healthy=true; break; fi
    if [[ $status == unhealthy ]]; then break; fi
    sleep 2
done
[[ $healthy == true ]] || { printf 'Container failed readiness.\n' >&2; exit 1; }
docker exec -e "EXPECTED_REVISION=$revision" "$name" python -c '
import json, os, urllib.request
assert os.getuid() != 0, "Container must run as a non-root user"
with urllib.request.urlopen("http://127.0.0.1:8080/version", timeout=3) as response:
    body = json.load(response)
assert body["revision"] == os.environ["EXPECTED_REVISION"], body
assert body["mode"] == "foundation", body
'
docker stop --time 30 "$name" >/dev/null
exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
# Uvicorn re-raises SIGTERM after its graceful shutdown and can exit with 143.
[[ $exit_code == 0 || $exit_code == 143 ]] || { printf 'Container did not shut down cleanly (exit %s).\n' "$exit_code" >&2; exit 1; }
docker logs "$name" 2>&1 | grep -F 'Application shutdown complete.' >/dev/null
printf 'Container startup, readiness, build revision and graceful shutdown passed.\n'
