#!/usr/bin/env bash
# Opt-in infrastructure setup. Requires an already working dedicated rootless daemon.
set -euo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die 'Run with sudo.'
[[ $# -eq 2 ]] || die 'Usage: sandbox-setup.sh GROUP_OPENID PINNED_IMAGE'
sandbox_group=$1
sandbox_image=$2
[[ $sandbox_group =~ ^[A-Za-z0-9_-]{16,128}$ ]] || die 'Use a group OpenID.'
[[ $sandbox_image =~ ^([a-z0-9./:_-]+@)?sha256:[a-f0-9]{64}$ ]] || die 'Use a content-addressed image.'
id fullish-sandbox >/dev/null 2>&1 || die 'Create the dedicated fullish-sandbox user and rootless daemon first; see docs/tools.md.'
sandbox_uid=$(id -u fullish-sandbox)
(( sandbox_uid != 0 && sandbox_uid != 10001 )) || die 'Sandbox user must be separate from root and the bot UID.'
sandbox_home=$(getent passwd fullish-sandbox | cut -d: -f6)
sandbox_host="unix:///run/user/$sandbox_uid/docker.sock"
[[ -S /run/user/$sandbox_uid/docker.sock ]] || die 'The dedicated rootless Docker daemon is not running.'
[[ -f /opt/fullish/runtime.env ]] || die 'Set up Fullish first.'
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec 9>/opt/fullish/.deploy.lock
flock -n 9 || die 'A deployment is running.'
if getent group 10001 >/dev/null; then
    [[ $(getent group 10001 | cut -d: -f1) == fullish-tools ]] || die 'GID 10001 is already in use; review the bot socket group manually.'
else
    groupadd --gid 10001 fullish-tools
fi
install -d -m 755 /opt/fullish-sandbox
install -m 644 "$source_root/src/fullish/sandbox_server.py" /opt/fullish-sandbox/server.py
install -m 644 "$source_root/deploy/fullish-sandbox.service" /etc/systemd/system/fullish-sandbox.service
if [[ ! -f /etc/fullish-sandbox.env ]]; then
    python3 - "$sandbox_group" "$sandbox_image" "$sandbox_host" "$sandbox_home" "$sandbox_uid" <<'PY'
import os, pathlib, secrets, sys, tempfile
group, image, host, home, uid = sys.argv[1:]
token = secrets.token_urlsafe(48)
settings = {'TOOLS_ALLOWED_GROUPS': group, 'SANDBOX_IMAGE': image, 'SANDBOX_TOKEN': token,
            'SANDBOX_DOCKER_HOST': host, 'SANDBOX_SOCKET': '/run/fullish-sandbox/api.sock',
            'HOME': home, 'XDG_RUNTIME_DIR': '/run/user/' + uid}
path = pathlib.Path('/etc/fullish-sandbox.env')
with path.open('x') as stream:
    for key, value in settings.items():
        stream.write(key + '=' + value + '\n')
path.chmod(0o600)
runtime = pathlib.Path('/opt/fullish/runtime.env')
values = {key: settings[key] for key in ('TOOLS_ALLOWED_GROUPS','SANDBOX_TOKEN','SANDBOX_SOCKET')}
lines = [line for line in runtime.read_text().splitlines() if line.partition('=')[0] not in values]
fd, name = tempfile.mkstemp(dir=runtime.parent, prefix='.sandbox-env-')
with os.fdopen(fd, 'w') as stream:
    stream.write('\n'.join(lines + [key+'='+value for key,value in values.items()])+'\n')
    stream.flush()
    os.fsync(stream.fileno())
os.replace(name, runtime)
PY
else
    printf 'Existing broker configuration preserved; review image/group updates in /etc/fullish-sandbox.env.\n'
fi
systemctl daemon-reload
systemctl enable --now fullish-sandbox.service
systemctl is-active --quiet fullish-sandbox.service
printf 'Sandbox broker installed. Verify isolation with sandbox-smoke.py before deploying the bot.\n'
