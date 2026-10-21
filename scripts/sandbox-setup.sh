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
[[ -x /opt/fullish-sandbox/sandbox-runsc.py ]] || die 'Run sandbox-runtime-setup.sh first.'
# Keep the host Compose mounts and deployment gate in sync with the reviewed source.
bash "$source_root/scripts/server-setup.sh" "$(< /opt/fullish/image-repository)"
exec 9>/opt/fullish/.deploy.lock
flock -n 9 || die 'A deployment is running.'
if getent group 10001 >/dev/null; then
    [[ $(getent group 10001 | cut -d: -f1) == fullish-tools ]] || die 'GID 10001 is already in use; review the bot socket group manually.'
else
    groupadd --gid 10001 fullish-tools
fi
install -d -m 755 /opt/fullish-sandbox
backup=$(mktemp -d /opt/fullish-sandbox/setup-backup.XXXXXXXX)
was_active=false
systemctl is-active --quiet fullish-sandbox.service && was_active=true
for path in /opt/fullish-sandbox/server.py /opt/fullish-sandbox/group_policy.py /etc/systemd/system/fullish-sandbox.service /etc/fullish-sandbox.env; do
    [[ ! -f $path ]] || cp -p -- "$path" "$backup/${path##*/}"
done
accepted=false
finish() {
    result=$?
    trap - EXIT
    if [[ $accepted != true ]]; then
        systemctl stop fullish-sandbox.service || true
        for path in /opt/fullish-sandbox/server.py /opt/fullish-sandbox/group_policy.py /etc/systemd/system/fullish-sandbox.service /etc/fullish-sandbox.env; do
            if [[ -f $backup/${path##*/} ]]; then cp -p -- "$backup/${path##*/}" "$path"; fi
        done
        systemctl daemon-reload
        if [[ $was_active == true ]]; then systemctl start fullish-sandbox.service || true; fi
        printf 'Sandbox acceptance failed; bot configuration was not enabled. Backup: %s\n' "$backup" >&2
    fi
    exit "$result"
}
trap finish EXIT
install -m 644 "$source_root/src/fullish/sandbox_server.py" /opt/fullish-sandbox/server.py
install -m 644 "$source_root/src/fullish/group_policy.py" /opt/fullish-sandbox/group_policy.py
install -m 644 "$source_root/deploy/fullish-sandbox.service" /etc/systemd/system/fullish-sandbox.service
python3 - "$sandbox_group" "$sandbox_image" "$sandbox_host" "$sandbox_home" "$sandbox_uid" <<'PY'
import os, pathlib, secrets, sys, tempfile
sys.path.insert(0, '/opt/fullish-sandbox')
from group_policy import GroupPolicies, legacy_policy
group, image, host, home, uid = sys.argv[1:]
path = pathlib.Path('/etc/fullish-sandbox.env')
settings = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line) if path.exists() else {}
if settings and group not in settings.get('TOOLS_ALLOWED_GROUPS', '').split(','):
    raise SystemExit('Configured groups differ; review the broker allowlist first.')
settings.update({'TOOLS_ALLOWED_GROUPS': settings.get('TOOLS_ALLOWED_GROUPS', group),
                 'SANDBOX_IMAGE': image, 'SANDBOX_TOKEN': settings.get('SANDBOX_TOKEN') or secrets.token_urlsafe(48),
                 'SANDBOX_DOCKER_HOST': host, 'SANDBOX_SOCKET': '/run/fullish-sandbox/api.sock',
                 'HOME': home, 'XDG_RUNTIME_DIR': '/run/user/' + uid})
policy = GroupPolicies(pathlib.Path('/var/lib/fullish-control/policy.json'))
if not policy.path.exists():
    runtime = dict(line.split('=', 1) for line in pathlib.Path('/opt/fullish/runtime.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    os.environ.update(runtime)
    os.environ.update(settings)
    policy.write(legacy_policy())
    os.chown(policy.path, 10001, 10001)
settings['GROUP_POLICY_PATH'] = str(policy.path)
fd, name = tempfile.mkstemp(dir=path.parent, prefix='.sandbox-config-')
with os.fdopen(fd, 'w') as output:
    output.write('\n'.join(key+'='+value for key,value in settings.items())+'\n')
os.replace(name, path)
PY
systemctl daemon-reload
systemctl enable fullish-sandbox.service
systemctl restart fullish-sandbox.service
# The Type=simple unit being active alone is insufficient: wait for a live socket.
python3 - <<'PY'
import socket, time
for _ in range(60):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect('/run/fullish-sandbox/api.sock')
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit('Broker socket did not become ready.')
PY
python3 "$source_root/scripts/sandbox-smoke.py"
# Enable only after the real runtime has passed, retaining DeepSeek and QQ secrets.
python3 - <<'PY'
import os, pathlib, tempfile
settings = dict(line.split('=', 1) for line in pathlib.Path('/etc/fullish-sandbox.env').read_text().splitlines() if '=' in line)
runtime = pathlib.Path('/opt/fullish/runtime.env')
values = {key: settings[key] for key in ('TOOLS_ALLOWED_GROUPS','SANDBOX_TOKEN','SANDBOX_SOCKET')}
lines = [line for line in runtime.read_text().splitlines() if line.partition('=')[0] not in values]
fd, name = tempfile.mkstemp(dir=runtime.parent, prefix='.sandbox-env-')
with os.fdopen(fd, 'w') as output:
    output.write('\n'.join(lines + [key+'='+value for key,value in values.items()])+'\n')
    output.flush(); os.fsync(output.fileno())
os.replace(name, runtime)
PY
accepted=true
printf 'Sandbox accepted; Compose and deployment gates installed. Deploy the bot to apply its configuration.\n'
