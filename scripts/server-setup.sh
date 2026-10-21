#!/usr/bin/env bash
# Run manually on the Linux host: sudo bash scripts/server-setup.sh REGISTRY/REPO
set -euo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die 'Run server-setup.sh with sudo.'
[[ $# -eq 1 ]] || die 'Usage: server-setup.sh registry.example/namespace/fullish'
repository=$1
[[ $repository =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]+)?(/[a-z0-9][a-z0-9._-]*)+$ ]] || die 'Use a lowercase repository without a tag or digest.'

root=/opt/fullish
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for tool in docker flock timeout install; do
    command -v "$tool" >/dev/null || die "Required command missing: $tool"
done
docker info >/dev/null
compose_version=$(docker compose version --short)
compose_version=${compose_version#v}
IFS=. read -r major minor _ <<< "$compose_version"
[[ $major =~ ^[0-9]+$ && $minor =~ ^[0-9]+$ ]] || die 'Cannot determine Docker Compose version.'
(( major > 2 || (major == 2 && minor >= 30) )) || die 'Docker Compose >= 2.30 is required.'
if [[ -f $root/image-repository ]]; then
    [[ $(< "$root/image-repository") == "$repository" ]] || die 'Repository differs from the existing installation. Review the host configuration manually.'
fi

install -d -m 700 "$root" "$root/bin" "$root/state"
install -d -m 750 -o 10001 -g 10001 /var/lib/fullish-control
exec 9>"$root/.deploy.lock"
flock -n 9 || die 'A deployment is running. Try setup again after it finishes.'
staging="$root/setup-staging.$$"
install -d -m 700 "$staging"
backup=''
applying=0
finish() {
    result=$?
    trap - EXIT INT TERM
    if (( result != 0 && applying == 1 )) && [[ -n $backup ]]; then
        printf 'Setup failed; restoring previous infrastructure.\n' >&2
        install -m 644 "$backup/compose.yaml" "$root/compose.yaml"
        cp -a -- "$backup/bin/." "$root/bin/"
    fi
    rm -f -- "$staging/compose.yaml" "$staging/deploy.sh" "$staging/rollback.sh" "$staging/runtime.env" "$staging/runtime_config.py"
    rmdir -- "$staging"
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
install -m 644 "$source_root/deploy/compose.yaml" "$staging/compose.yaml"
install -m 755 "$source_root/scripts/deploy.sh" "$staging/deploy.sh"
install -m 755 "$source_root/scripts/rollback.sh" "$staging/rollback.sh"
install -m 644 "$source_root/scripts/runtime_config.py" "$staging/runtime_config.py"
if [[ -f $root/runtime.env ]]; then
    install -m 600 "$root/runtime.env" "$staging/runtime.env"
else
    install -m 600 "$source_root/deploy/runtime.env.example" "$staging/runtime.env"
fi
bash -n "$staging/deploy.sh"
bash -n "$staging/rollback.sh"
FULLISH_IMAGE="$repository@sha256:$(printf '%064d' 0)" docker compose -p fullish -f "$staging/compose.yaml" config --quiet
if [[ -f $root/compose.yaml ]]; then
    backup="$root/setup-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    install -d -m 700 "$backup"
    cp -- "$root/compose.yaml" "$backup/compose.yaml"
    cp -a -- "$root/bin" "$backup/bin"
    printf 'Previous infrastructure files saved to %s\n' "$backup"
fi
applying=1
install -m 644 "$staging/compose.yaml" "$root/compose.yaml"
install -m 755 "$staging/deploy.sh" "$root/bin/deploy.sh"
install -m 755 "$staging/rollback.sh" "$root/bin/rollback.sh"
install -m 644 "$staging/runtime_config.py" "$root/bin/runtime_config.py"
if [[ ! -f $root/runtime.env ]]; then
    install -m 600 "$staging/runtime.env" "$root/runtime.env"
fi
printf '%s\n' "$repository" > "$root/image-repository"
chmod 600 "$root/image-repository" "$root/runtime.env"
applying=0
printf 'Installed infrastructure at %s; no application was started.\n' "$root"
printf 'Next: configure restricted SSH or the saved TAT deployment command.\n'
