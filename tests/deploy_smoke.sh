#!/usr/bin/env bash
# Exercise the deployment transaction with a stateful Docker substitute.
# No registry access, Docker daemon, server, or credentials are required.
set -euo pipefail

for tool in bash flock timeout mktemp cmp; do
    if ! command -v "$tool" >/dev/null; then
        printf 'SKIP: deployment transaction tests require %s (run on Linux).\n' "$tool" >&2
        exit 77
    fi
done

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
suite_tmp=$(mktemp -d "${TMPDIR:-/tmp}/fullish-deploy-test.XXXXXXXX")
cleanup() {
    [[ -n $suite_tmp && $suite_tmp == "${TMPDIR:-/tmp}"/fullish-deploy-test.* ]] || return
    rm -rf -- "$suite_tmp"
}
trap cleanup EXIT
mkdir -p "$suite_tmp/bin"

cat > "$suite_tmp/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_DOCKER_STATE:?}
printf '%s | %s\n' "${FULLISH_IMAGE:-unset}" "$*" >> "$state/calls.log"
matches() { [[ -f $state/$1 && $(< "$state/$1") == "$2" ]]; }
inspect_container() {
    [[ -f $state/container-image ]] || exit 1
    case "$1" in
        '{{.Config.Image}}') cat "$state/container-image" ;;
        '{{.State.Health.Status}}') cat "$state/container-health" ;;
        '{{.State.Running}}') cat "$state/container-running" ;;
        '{{.State.Status}}')
            if [[ $(< "$state/container-running") == true ]]; then echo running; else echo exited; fi
            ;;
        *) printf 'Unsupported container inspect format: %s\n' "$1" >&2; exit 2 ;;
    esac
}
case "${1:-}" in
    pull)
        if matches fail-pull "$2"; then exit 1; fi
        printf 'Pulled %s\n' "$2"
        ;;
    image)
        [[ ${2:-} == inspect ]] || exit 2
        shift 2
        if [[ ${1:-} == --format ]]; then
            [[ ${2:-} == '{{json .Config.Healthcheck.Test}}' ]] || exit 2
            if matches missing-healthcheck "$3"; then echo null; else echo '["CMD","probe"]'; fi
        else
            echo '{}'
        fi
        ;;
    inspect)
        [[ ${2:-} == --format ]] || exit 2
        inspect_container "$3"
        ;;
    compose)
        shift
        while [[ ${1:-} == --* ]]; do
            case "$1" in --project-name|--file) shift 2 ;; *) exit 2 ;; esac
        done
        action=${1:?}
        shift
        case "$action" in
            config) [[ ${1:-} == --quiet ]] || exit 2 ;;
            ps)
                if [[ -f $state/container-image ]]; then
                    if [[ " $* " == *' --all '* || $(< "$state/container-running") == true ]]; then
                        echo fake-fullish-bot
                    fi
                fi
                ;;
            stop)
                if [[ -f $state/container-image ]]; then
                    echo false > "$state/container-running"
                    echo unhealthy > "$state/container-health"
                fi
                ;;
            up)
                # A release must remain unpublished until the candidate is healthy.
                if [[ -f $state/before-state ]]; then
                    cmp -s "$state/before-state" "$FULLISH_ROOT/state/release.env" || {
                        touch "$state/early-state-write"; exit 2;
                    }
                elif [[ -e $FULLISH_ROOT/state/release.env ]]; then
                    touch "$state/early-state-write"; exit 2
                fi
                printf '%s\n' "$FULLISH_IMAGE" > "$state/container-image"
                if matches wrong-running-image "$FULLISH_IMAGE"; then
                    printf '%s\n' 'ghcr.io/example/fullish@sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' > "$state/container-image"
                fi
                echo true > "$state/container-running"
                if matches fail-up "$FULLISH_IMAGE"; then
                    echo unhealthy > "$state/container-health"
                    exit 1
                fi
                if matches unhealthy "$FULLISH_IMAGE"; then
                    echo unhealthy > "$state/container-health"
                else
                    echo healthy > "$state/container-health"
                fi
                ;;
            *) printf 'Unsupported compose command: %s\n' "$action" >&2; exit 2 ;;
        esac
        ;;
    *) printf 'Unsupported docker command: %s\n' "$*" >&2; exit 2 ;;
esac
DOCKER
chmod +x "$suite_tmp/bin/docker"
export PATH="$suite_tmp/bin:$PATH"

repo=ghcr.io/example/fullish
printf -v digest_a '%064d' 1
printf -v digest_b '%064d' 2
printf -v digest_c '%064d' 3
image_a="$repo@sha256:$digest_a"
image_b="$repo@sha256:$digest_b"
image_c="$repo@sha256:$digest_c"
case_count=0

fail() {
    printf 'FAIL [%s]: %s\n' "$case_name" "$*" >&2
    [[ ! -f $FAKE_DOCKER_STATE/output ]] || cat "$FAKE_DOCKER_STATE/output" >&2
    [[ ! -f $FAKE_DOCKER_STATE/calls.log ]] || cat "$FAKE_DOCKER_STATE/calls.log" >&2
    exit 1
}
new_case() {
    case_name=$1
    case_count=$((case_count + 1))
    export FULLISH_ROOT="$suite_tmp/$case_name"
    export FAKE_DOCKER_STATE="$FULLISH_ROOT/fake-docker"
    mkdir -p "$FULLISH_ROOT/state" "$FAKE_DOCKER_STATE"
    printf 'services: {}\n' > "$FULLISH_ROOT/compose.yaml"
    : > "$FULLISH_ROOT/runtime.env"
    printf '%s\n' "$repo" > "$FULLISH_ROOT/image-repository"
    : > "$FAKE_DOCKER_STATE/calls.log"
}
seed_release() {
    printf 'CURRENT_IMAGE=%s\nPREVIOUS_IMAGE=%s\n' "$1" "${2:-}" > "$FULLISH_ROOT/state/release.env"
    printf '%s\n' "$1" > "$FAKE_DOCKER_STATE/container-image"
    echo true > "$FAKE_DOCKER_STATE/container-running"
    echo healthy > "$FAKE_DOCKER_STATE/container-health"
}
run_deploy() {
    if [[ -f $FULLISH_ROOT/state/release.env ]]; then
        cp "$FULLISH_ROOT/state/release.env" "$FAKE_DOCKER_STATE/before-state"
    else
        rm -f -- "$FAKE_DOCKER_STATE/before-state"
    fi
    status=0
    bash "$project_root/scripts/deploy.sh" "$@" > "$FAKE_DOCKER_STATE/output" 2>&1 || status=$?
    [[ ! -e $FAKE_DOCKER_STATE/early-state-write ]] || fail 'release state changed before health confirmation'
    local leftovers=("$FULLISH_ROOT"/state/release.env.tmp.*)
    [[ ! -e ${leftovers[0]} ]] || fail 'temporary release state leaked'
}
assert_success() { [[ $status -eq 0 ]] || fail "expected success, received $status"; }
assert_failure() { [[ $status -ne 0 ]] || fail 'expected deployment failure'; }
assert_state() {
    printf 'CURRENT_IMAGE=%s\nPREVIOUS_IMAGE=%s\n' "$1" "${2:-}" > "$FAKE_DOCKER_STATE/expected-state"
    cmp -s "$FAKE_DOCKER_STATE/expected-state" "$FULLISH_ROOT/state/release.env" || fail 'unexpected release state'
}
assert_running() {
    [[ $(< "$FAKE_DOCKER_STATE/container-image") == "$1" ]] || fail 'unexpected running image'
    [[ $(< "$FAKE_DOCKER_STATE/container-running") == true ]] || fail 'container is stopped'
    [[ $(< "$FAKE_DOCKER_STATE/container-health") == healthy ]] || fail 'container is unhealthy'
}
assert_no_stop() {
    while IFS= read -r call; do
        [[ $call != *' stop '* ]] || fail 'old container was stopped'
    done < "$FAKE_DOCKER_STATE/calls.log"
}
assert_restarted() {
    local stopped=false started=false
    while IFS= read -r call; do
        [[ $call != *' stop '* ]] || stopped=true
        if [[ $call == *' up '* ]]; then
            [[ $stopped == true ]] || fail 'replacement started before the old process stopped'
            started=true
        fi
    done < "$FAKE_DOCKER_STATE/calls.log"
    [[ $stopped == true && $started == true ]] || fail 'same-digest deployment did not restart'
}
pass() { printf 'PASS %s\n' "$case_name"; }

new_case initial_and_successive_releases
run_deploy "$image_a"
assert_success
assert_state "$image_a" ''
assert_running "$image_a"
run_deploy "$image_b"
assert_success
assert_state "$image_b" "$image_a"
assert_running "$image_b"
run_deploy "$image_c"
assert_success
assert_state "$image_c" "$image_b"
assert_running "$image_c"
pass

new_case pull_failure_keeps_old_process
seed_release "$image_a"
printf '%s\n' "$image_b" > "$FAKE_DOCKER_STATE/fail-pull"
run_deploy "$image_b"
assert_failure
assert_state "$image_a" ''
assert_running "$image_a"
assert_no_stop
pass

new_case failed_candidate_rolls_back_and_reports_failure
seed_release "$image_b" "$image_a"
printf '%s\n' "$image_c" > "$FAKE_DOCKER_STATE/fail-up"
run_deploy "$image_c"
assert_failure
assert_state "$image_b" "$image_a"
assert_running "$image_b"
pass

new_case first_failure_allows_retry
printf '%s\n' "$image_a" > "$FAKE_DOCKER_STATE/fail-up"
run_deploy "$image_a"
assert_failure
[[ ! -e $FULLISH_ROOT/state/release.env ]] || fail 'failed first release was recorded'
[[ $(< "$FAKE_DOCKER_STATE/container-running") == false ]] || fail 'failed first candidate was left running'
rm -f -- "$FAKE_DOCKER_STATE/fail-up"
run_deploy "$image_a"
assert_success
assert_state "$image_a" ''
assert_running "$image_a"
pass

new_case manual_rollback
seed_release "$image_b" "$image_a"
run_deploy --rollback
assert_success
assert_state "$image_a" "$image_b"
assert_running "$image_a"
pass

new_case reject_invalid_or_disallowed_images
seed_release "$image_b" "$image_a"
for rejected in 'invalid' "$repo:latest" "ghcr.io/other/fullish@sha256:$digest_c"; do
    run_deploy "$rejected"
    assert_failure
    assert_state "$image_b" "$image_a"
    assert_running "$image_b"
done
[[ ! -s $FAKE_DOCKER_STATE/calls.log ]] || fail 'invalid input invoked Docker'
pass

new_case reject_image_without_healthcheck
seed_release "$image_a"
printf '%s\n' "$image_b" > "$FAKE_DOCKER_STATE/missing-healthcheck"
run_deploy "$image_b"
assert_failure
assert_state "$image_a" ''
assert_running "$image_a"
assert_no_stop
pass

new_case unhealthy_candidate_rolls_back
seed_release "$image_a"
printf '%s\n' "$image_b" > "$FAKE_DOCKER_STATE/unhealthy"
run_deploy "$image_b"
assert_failure
assert_state "$image_a" ''
assert_running "$image_a"
pass

new_case wrong_running_digest_rolls_back
seed_release "$image_a"
printf '%s\n' "$image_b" > "$FAKE_DOCKER_STATE/wrong-running-image"
run_deploy "$image_b"
assert_failure
assert_state "$image_a" ''
assert_running "$image_a"
pass

new_case refuse_untracked_running_container
seed_release "$image_a"
rm -f -- "$FULLISH_ROOT/state/release.env"
run_deploy "$image_b"
assert_failure
[[ ! -e $FULLISH_ROOT/state/release.env ]] || fail 'untracked container state was replaced'
assert_running "$image_a"
assert_no_stop
pass

new_case same_digest_applies_configuration_and_preserves_previous
seed_release "$image_b" "$image_a"
printf 'LOG_LEVEL=DEBUG\n' > "$FULLISH_ROOT/runtime.env"
run_deploy "$image_b"
assert_success
assert_state "$image_b" "$image_a"
assert_running "$image_b"
assert_restarted
pass

printf 'All %s deployment transaction scenarios passed.\n' "$case_count"
