#!/usr/bin/env bash
# Run only in a disposable root container with the repository mounted read-only.
# No Docker socket, registry access or cloud credentials are required.
set -euo pipefail

die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
[[ ${FULLISH_SETUP_TEST_CONTAINER:-} == 1 && $EUID -eq 0 && -f /.dockerenv ]] || {
    printf 'Run this test in a disposable root Docker container; see the CI command.\n' >&2
    exit 77
}
[[ ! -S /var/run/docker.sock ]] || die 'This test must not have access to a Docker socket.'
[[ ! -e /opt/fullish ]] || die 'Refusing to overwrite an existing /opt/fullish installation.'
for tool in bash flock timeout install mktemp cmp sha256sum; do
    command -v "$tool" >/dev/null || die "Missing test command: $tool"
done

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
test_root=$(mktemp -d /tmp/fullish-setup-test.XXXXXXXX)
mkdir -p "$test_root/source/scripts" "$test_root/source/deploy" "$test_root/bin"
cp "$project_root/scripts/server-setup.sh" "$project_root/scripts/deploy.sh" \
    "$project_root/scripts/rollback.sh" "$project_root/scripts/runtime_config.py" "$test_root/source/scripts/"
cp "$project_root/deploy/compose.yaml" "$project_root/deploy/runtime.env.example" \
    "$test_root/source/deploy/"
export SETUP_TEST_ROOT="$test_root"
export SETUP_TEST_REAL_INSTALL
SETUP_TEST_REAL_INSTALL=$(command -v install)

cat > "$test_root/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
    info) exit 0 ;;
    'compose version --short') printf '2.30.3\n'; exit 0 ;;
esac
[[ ${1:-} == compose && ${2:-} == -p && ${3:-} == fullish && ${4:-} == -f ]] || exit 2
compose=${5:?}
[[ ${6:-} == config && ${7:-} == --quiet ]] || exit 2
[[ $compose == /opt/fullish/setup-staging.*/compose.yaml ]] || exit 3
[[ -s ${compose%/*}/runtime.env ]] || exit 4
[[ ${FULLISH_IMAGE:-} == ghcr.io/example/fullish@sha256:* ]] || exit 5
while IFS= read -r line; do
    [[ $line != INVALID_TEST_COMPOSE ]] || exit 6
done < "$compose"
DOCKER

cat > "$test_root/bin/install" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail
# Fault injection after Compose and deploy.sh have been replaced.
if [[ -f $SETUP_TEST_ROOT/fail-install && ${!#} == /opt/fullish/bin/rollback.sh ]]; then
    rm -- "$SETUP_TEST_ROOT/fail-install"
    exit 41
fi
exec "$SETUP_TEST_REAL_INSTALL" "$@"
INSTALL
chmod +x "$test_root/bin/docker" "$test_root/bin/install"
export PATH="$test_root/bin:$PATH"

run_setup() {
    setup_status=0
    bash "$test_root/source/scripts/server-setup.sh" ghcr.io/example/fullish \
        > "$test_root/setup.log" 2>&1 || setup_status=$?
}
expect_success() {
    if (( setup_status != 0 )); then cat "$test_root/setup.log" >&2; die 'Setup failed.'; fi
}
expect_failure() { (( setup_status != 0 )) || die 'Setup unexpectedly succeeded.'; }
snapshot() {
    sha256sum /opt/fullish/compose.yaml /opt/fullish/bin/deploy.sh \
        /opt/fullish/bin/rollback.sh /opt/fullish/image-repository \
        /opt/fullish/bin/runtime_config.py \
        /opt/fullish/runtime.env /opt/fullish/state/release.env > "$1"
}
expect_preserved() {
    snapshot "$test_root/after"
    cmp "$test_root/before" "$test_root/after" || die 'Existing installation changed.'
    local leftovers=(/opt/fullish/setup-staging.*)
    [[ ! -e ${leftovers[0]} ]] || die 'Staging files leaked after setup.'
}

run_setup
expect_success
cmp "$test_root/source/deploy/compose.yaml" /opt/fullish/compose.yaml
cmp "$test_root/source/deploy/runtime.env.example" /opt/fullish/runtime.env
[[ -x /opt/fullish/bin/deploy.sh && -x /opt/fullish/bin/rollback.sh ]] || die 'Scripts are not executable.'
[[ ! -e /opt/fullish/state/release.env ]] || die 'Setup incorrectly created a release record.'
printf 'PASS first installation\n'

printf 'RUNTIME_VALUE=preserve-this\n' > /opt/fullish/runtime.env
printf 'CURRENT_IMAGE=test-preserved-state\nPREVIOUS_IMAGE=previous-state\n' \
    > /opt/fullish/state/release.env
cp /opt/fullish/runtime.env "$test_root/runtime.before"
cp /opt/fullish/state/release.env "$test_root/release.before"
printf '\n# Infrastructure update test\n' >> "$test_root/source/deploy/compose.yaml"
printf '\n# Updated deployment script\n' >> "$test_root/source/scripts/deploy.sh"
run_setup
expect_success
cmp "$test_root/runtime.before" /opt/fullish/runtime.env
cmp "$test_root/release.before" /opt/fullish/state/release.env
cmp "$test_root/source/deploy/compose.yaml" /opt/fullish/compose.yaml
cmp "$test_root/source/scripts/deploy.sh" /opt/fullish/bin/deploy.sh
printf 'PASS repeated setup updates infrastructure and preserves runtime/state\n'

snapshot "$test_root/before"
cp "$test_root/source/deploy/compose.yaml" "$test_root/valid-compose"
printf 'INVALID_TEST_COMPOSE\n' >> "$test_root/source/deploy/compose.yaml"
run_setup
expect_failure
expect_preserved
cp "$test_root/valid-compose" "$test_root/source/deploy/compose.yaml"
printf 'PASS invalid Compose leaves existing installation intact\n'

printf '\n# Must not survive failed installation\n' >> "$test_root/source/deploy/compose.yaml"
printf '\n# Must be rolled back\n' >> "$test_root/source/scripts/deploy.sh"
touch "$test_root/fail-install"
run_setup
expect_failure
expect_preserved
printf 'PASS interrupted installation restores previous infrastructure\n'
printf 'All 4 setup transaction scenarios passed.\n'
