#!/usr/bin/env bash
# Only the dedicated rootless daemon is configured or restarted.
set -euo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 && $# -eq 0 ]] || die 'Usage: sudo bash scripts/sandbox-runtime-setup.sh'
[[ $(uname -m) == x86_64 ]] || die 'The verified sandbox runtime currently requires x86_64.'
for tool in curl sha512sum tar python3 useradd loginctl runuser flock dockerd-rootless.sh rootlesskit newuidmap newgidmap; do
    command -v "$tool" >/dev/null || die "Install the rootless Docker prerequisites first: $tool"
done
[[ -f /sys/fs/cgroup/cgroup.controllers ]] || die 'cgroup v2 is required.'
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
root=/opt/fullish-sandbox
install -d -m 755 "$root"
exec 8>"$root/.runtime-setup.lock"
flock -n 8 || die 'Sandbox runtime setup is already running.'
if ! id fullish-sandbox >/dev/null 2>&1; then
    useradd --create-home --home-dir /var/lib/fullish-sandbox --shell /bin/bash fullish-sandbox
fi
sandbox_uid=$(id -u fullish-sandbox)
(( sandbox_uid != 0 && sandbox_uid != 10001 )) || die 'Use a dedicated non-root sandbox UID.'
[[ $(id -Gn fullish-sandbox) != *docker* ]] || die 'Sandbox user must not have production Docker access.'
sandbox_home=$(getent passwd fullish-sandbox | cut -d: -f6)
[[ $sandbox_home == /var/lib/fullish-sandbox ]] || die 'Unexpected sandbox home directory.'
for file in /etc/subuid /etc/subgid; do
    grep -Eq '^fullish-sandbox:[0-9]+:[0-9]+$' "$file" || die "Missing subordinate IDs in $file"
done
runtime="$root/runtime-20261012.0"
if [[ ! -f $runtime/.verified ]]; then
    staging=$(mktemp -d "$root/runtime-staging.XXXXXXXX")
    trap 'rm -f -- "$staging/gvisor.tar.bz2"' EXIT
    curl --fail --silent --show-error --location --retry 3 --max-time 180 \
        https://storage.googleapis.com/gvisor/releases/release/20261012.0/x86_64/gvisor.tar.bz2 \
        --output "$staging/gvisor.tar.bz2"
    printf '%s  %s\n' 9606445eed5aedfcf05310e7ccef61d994089914c6622fc82624c24d2b18db1b2210f40e40a7be27b8b7772859de7d95c501b4eba66423053c960bf671377c5e "$staging/gvisor.tar.bz2" | sha512sum --check --status
    install -d -m 755 "$runtime"
    tar -xjf "$staging/gvisor.tar.bz2" -C "$runtime"
    chmod 755 "$runtime/runsc" "$runtime/containerd-shim-runsc-v1" "$runtime/gvisor-bin" "$runtime"/gvisor-bin/*
    touch "$runtime/.verified"
    rm -f -- "$staging/gvisor.tar.bz2"
    rmdir "$staging"
    trap - EXIT
fi
ln -sfn runtime-20261012.0 "$root/runtime"
install -m 755 "$source_root/scripts/sandbox-runsc.py" "$root/sandbox-runsc.py"
loginctl enable-linger fullish-sandbox
install -d -m 755 "/etc/systemd/system/user@$sandbox_uid.service.d"
printf '[Service]\nDelegate=cpu memory pids\n' > "/etc/systemd/system/user@$sandbox_uid.service.d/fullish-delegate.conf"
systemctl daemon-reload
systemctl start "user@$sandbox_uid.service"
user_command=(runuser -u fullish-sandbox -- env -i PATH=/usr/local/bin:/usr/bin:/bin "HOME=$sandbox_home" USER=fullish-sandbox LOGNAME=fullish-sandbox "XDG_RUNTIME_DIR=/run/user/$sandbox_uid" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$sandbox_uid/bus")
unit_changed=false
sandbox_gid=$(id -g fullish-sandbox)
unit_directory="$sandbox_home/.config/systemd/user"
for directory in "$sandbox_home/.config" "$sandbox_home/.config/systemd" "$unit_directory" "$unit_directory/docker.service.d"; do
    [[ ! -L $directory ]] || die 'Dedicated configuration directories must not be symlinks.'
    install -d -o "$sandbox_uid" -g "$sandbox_gid" -m 700 "$directory"
done
if [[ ! -f $unit_directory/docker.service ]]; then
    install -o "$sandbox_uid" -g "$sandbox_gid" -m 600 "$source_root/deploy/fullish-rootless-docker.service" "$unit_directory/docker.service"
    unit_changed=true
fi
environment_file=$(mktemp "$root/runtime-environment.XXXXXXXX")
printf '[Service]\nEnvironment="HOME=%s" "XDG_CONFIG_HOME=%s/.config" "DOCKER_CONFIG=%s/.docker" "XDG_RUNTIME_DIR=/run/user/%s" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus" "DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/user/%s/bus" "USER=fullish-sandbox" "LOGNAME=fullish-sandbox"\n' \
    "$sandbox_home" "$sandbox_home" "$sandbox_home" "$sandbox_uid" "$sandbox_uid" "$sandbox_uid" > "$environment_file"
if ! cmp -s "$environment_file" "$unit_directory/docker.service.d/zz-fullish-environment.conf"; then
    install -o "$sandbox_uid" -g "$sandbox_gid" -m 600 "$environment_file" "$unit_directory/docker.service.d/zz-fullish-environment.conf"
    unit_changed=true
fi
rm -f -- "$environment_file"
"${user_command[@]}" systemctl --user daemon-reload
# Configuration changes are detected, so repeated setup does not restart a healthy sandbox.
changed=$(python3 - "$sandbox_home" "$sandbox_uid" <<'PY'
import json, os, pathlib, pwd, sys, tempfile
home, uid = pathlib.Path(sys.argv[1]), int(sys.argv[2])
gid = pwd.getpwnam('fullish-sandbox').pw_gid
directory = home / '.config/docker'
if directory.is_symlink() or not directory.resolve().is_relative_to(home):
    raise SystemExit('Invalid dedicated Docker config directory.')
directory.mkdir(parents=True, exist_ok=True)
os.chown(directory, uid, gid)
directory.chmod(0o700)
path = directory / 'daemon.json'
before = path.read_text() if path.exists() else '{}'
config = json.loads(before)
config.setdefault('runtimes', {})['runsc'] = {
    'path': '/opt/fullish-sandbox/sandbox-runsc.py', 'runtimeArgs': ['--platform=systrap']}
config['exec-opts'] = ['native.cgroupdriver=systemd']
# Workload containers always use network=none. A default bridge/NAT is neither
# needed nor permitted; rootless daemons cannot assume host netfilter access.
config.update({'bridge': 'none', 'iptables': False, 'ip6tables': False,
               'ip-forward': False, 'ip-masq': False})
if json.loads(before) != config:
    fd, name = tempfile.mkstemp(dir=directory)
    with os.fdopen(fd, 'w') as output:
        output.write(json.dumps(config) + '\n')
    os.chown(name, uid, gid)
    os.replace(name, path)
    print('yes')
PY
)
if [[ $changed == yes || $unit_changed == true ]]; then
    action=restart
else
    action=start
fi
"${user_command[@]}" systemctl --user enable docker
if ! "${user_command[@]}" systemctl --user "$action" docker; then
    "${user_command[@]}" journalctl --user --unit docker.service -n 40 --no-pager >&2 || true
    die 'Dedicated rootless Docker initialization failed.'
fi
"${user_command[@]}" docker --host "unix:///run/user/$sandbox_uid/docker.sock" info --format '{{.CgroupVersion}} {{.CgroupDriver}}' | grep -qx '2 systemd' || die 'Rootless cgroup v2/systemd is required.'
printf 'Pinned rootless gVisor runtime installed. Broker acceptance is still required.\n'
