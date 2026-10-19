#!/usr/bin/env bash
# Root entrypoint: ephemeral registry authentication, then the normal transaction.
set -euo pipefail
umask 077
[[ $EUID -eq 0 && $# -eq 1 ]] || { echo 'Root entrypoint requires one image.' >&2; exit 64; }
image=$1
[[ $image =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]+)?(/[a-z0-9][a-z0-9._-]*)+@sha256:[a-f0-9]{64}$ ]] || exit 64
repository=$(< /opt/fullish/image-repository)
[[ ${image%@sha256:*} == "$repository" ]] || { echo 'Image repository is not allowed.' >&2; exit 64; }
IFS= read -r username
IFS= read -r password
[[ $username =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$ && -n $password ]] || exit 64
authdir=$(mktemp -d /run/fullish-registry.XXXXXX)
cleanup() { rm -f -- "$authdir/config.json" "$authdir/qq.json"; rmdir -- "$authdir"; }
trap cleanup EXIT
# Older clients send only registry credentials. Missing QQ settings preserve the host config.
settings='{}'
IFS= read -r settings || settings='{}'
[[ ${#settings} -le 4096 ]] || exit 64
printf '%s' "$settings" | python3 /opt/fullish/bin/runtime_config.py validate > "$authdir/qq.json"
unset settings
if [[ -s $authdir/qq.json ]]; then export FULLISH_QQ_CONFIG_FILE="$authdir/qq.json"; fi
export DOCKER_CONFIG="$authdir"
printf '%s' "$password" | timeout 45 docker login "${repository%%/*}" --username "$username" --password-stdin >/dev/null
unset password
bash /opt/fullish/bin/deploy.sh "$image"
