#!/usr/bin/env bash
# Host-side deployment transaction. Infrastructure files are installed separately.
set -euo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
valid_image() { [[ $1 =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]+)?(/[a-z0-9][a-z0-9._-]*)+@sha256:[a-f0-9]{64}$ ]]; }
root=${FULLISH_ROOT:-/opt/fullish}
[[ $root == /* && $root != / ]] || die 'FULLISH_ROOT must be an absolute installation directory.'
[[ $# -eq 1 ]] || die 'Usage: deploy.sh repository@sha256:digest | --rollback'
[[ -f $root/compose.yaml && -f $root/runtime.env && -f $root/image-repository && -d $root/state ]] || die 'Run server-setup.sh first.'
for tool in docker flock timeout; do command -v "$tool" >/dev/null || die "Required command missing: $tool"; done
exec 9>"$root/.deploy.lock"
flock -w 10 9 || die 'Another deployment holds the server lock.'

state="$root/state/release.env"
current=''
previous=''
if [[ -f $state ]]; then
    current=$(sed -n 's/^CURRENT_IMAGE=//p' "$state")
    previous=$(sed -n 's/^PREVIOUS_IMAGE=//p' "$state")
    valid_image "$current" || die 'Invalid current image in release state; repair it manually.'
    [[ -z $previous ]] || valid_image "$previous" || die 'Invalid previous image in release state.'
fi
target=$1
rollback_mode=0
if [[ $target == --rollback ]]; then
    rollback_mode=1
    [[ -n $previous ]] || die 'No previous successful image is available.'
    target=$previous
fi
valid_image "$target" || die 'Deployment requires an immutable repository@sha256 digest.'
repository=$(< "$root/image-repository")
[[ ${target%@sha256:*} == "$repository" ]] || die 'Image repository is not allowed by this host.'
[[ -z $current || ${current%@sha256:*} == "$repository" ]] || die 'Current image repository differs from host configuration.'
export FULLISH_IMAGE="$target"
compose=(docker compose --project-name fullish --file "$root/compose.yaml")
"${compose[@]}" config --quiet

apply_started=0
runtime_backup=''
on_exit() {
    local result=$?
    trap - EXIT INT TERM
    if (( result != 0 )) && [[ -n $runtime_backup ]]; then
        mv -f -- "$runtime_backup" "$root/runtime.env"
        runtime_backup=''
        printf 'Previous runtime configuration restored.\n' >&2
    fi
    if (( result != 0 && apply_started == 1 )); then
        printf 'Deployment failed; restoring the last successful image.\n' >&2
        if [[ -n $current ]]; then
            if FULLISH_IMAGE="$current" timeout 150 "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 90 bot; then
                printf 'Previous image restored. Deployment still reports failure.\n' >&2
            else
                printf 'ROLLBACK FAILED: inspect the server before another deployment.\n' >&2
            fi
        else
            if timeout 45 "${compose[@]}" stop --timeout 30 bot; then
                printf 'First deployment failed; stopped the candidate. There is no previous release.\n' >&2
            else
                printf 'CANDIDATE STOP FAILED: inspect Docker and stop the failed application manually.\n' >&2
            fi
        fi
    fi
    rm -f -- "$root/state/release.env.tmp.$$"
    [[ -z $runtime_backup ]] || rm -f -- "$runtime_backup"
    exit "$result"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Pull and validate before stopping the old process. Never prune rollback images.
if (( rollback_mode == 0 )) || ! docker image inspect "$target" >/dev/null 2>&1; then
    timeout 300 docker pull "$target" >/dev/null
fi
healthcheck=$(docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$target")
[[ $healthcheck != null && $healthcheck != '[]' && $healthcheck != '["NONE"]' ]] || die 'Candidate image has no usable healthcheck.'
if [[ -n $current ]] && ! docker image inspect "$current" >/dev/null 2>&1; then
    timeout 120 docker pull "$current" >/dev/null
fi
container=$("${compose[@]}" ps --quiet bot)
if [[ -z $current && -n $container ]]; then
    die 'A container exists without release state. Inspect and recover state before deploying.'
fi
if [[ -n ${FULLISH_QQ_CONFIG_FILE:-} ]]; then
    runtime_backup=$(mktemp "$root/state/runtime-backup.XXXXXXXX")
    cp -p -- "$root/runtime.env" "$runtime_backup"
    helper=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/runtime_config.py
    python3 "$helper" update "$root/runtime.env" < "$FULLISH_QQ_CONFIG_FILE"
fi
# An enabled sandbox must work through the candidate's actual Compose mounts
# before the healthy sender is stopped. This runs no model or QQ requests.
sandbox_enabled() { grep -Eq '^SANDBOX_(SOCKET|TOKEN)=.+' "$root/runtime.env"; }
if sandbox_enabled; then
    timeout 90 "${compose[@]}" run --rm --no-deps --pull never bot python -m fullish.sandbox_check
fi
apply_started=1
# Single active sender: stop old application before starting its replacement.
timeout 45 "${compose[@]}" stop --timeout 30 bot
timeout 150 "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 90 bot
container=$("${compose[@]}" ps --quiet bot)
[[ -n $container ]] || die 'Compose returned without a running application.'
[[ $(docker inspect --format '{{.State.Health.Status}}' "$container") == healthy ]] || die 'Application did not pass its healthcheck.'
[[ $(docker inspect --format '{{.Config.Image}}' "$container") == "$target" ]] || die 'Running image does not match the requested digest.'
if sandbox_enabled; then
    timeout 90 docker exec "$container" python -m fullish.sandbox_check
fi

if [[ $current != "$target" ]]; then previous=$current; fi
# Commit is a tiny critical section: after the state rename, a TERM must not
# restore the old container while leaving the new image recorded as current.
trap '' INT TERM
printf 'CURRENT_IMAGE=%s\nPREVIOUS_IMAGE=%s\n' "$target" "$previous" > "$root/state/release.env.tmp.$$"
mv -f -- "$root/state/release.env.tmp.$$" "$state"
apply_started=0
[[ -z $runtime_backup ]] || rm -f -- "$runtime_backup"
runtime_backup=''
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'Deployment healthy: %s\n' "$target"

# Operational reporting happens only after the successful release transaction.
# A report failure must never restore the previous healthy application.
revision_of() {
    docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1"
}
report_revision=$(revision_of "$target" 2>/dev/null || true)
report_previous=''
[[ -z $current ]] || report_previous=$(revision_of "$current" 2>/dev/null || true)
if [[ $report_revision =~ ^[a-f0-9]{40}$ ]] && [[ -z $report_previous || $report_previous =~ ^[a-f0-9]{40}$ ]]; then
    report_args=(--previous "$report_previous" --current "$report_revision")
    (( rollback_mode == 0 )) || report_args+=(--rollback)
    if ! timeout 320 docker exec "$container" python -m fullish.release_report "${report_args[@]}"; then
        printf 'WARNING: deployment succeeded, but the QQ deployment report failed.\n' >&2
    fi
else
    printf 'WARNING: deployment succeeded, but image revision labels are unavailable for reporting.\n' >&2
fi
