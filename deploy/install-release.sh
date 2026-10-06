#!/bin/sh
# Usage: sudo install-release.sh /path/to/extracted-release [--init-service-env]
# Existing worklog, nginx, other applications, and system Python are untouched.
set -eu
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || { echo 'Usage: install-release.sh RELEASE [--init-service-env]' >&2; exit 1; }
init_service_env=no
if [ "$#" -eq 2 ]; then
    [ "$2" = --init-service-env ] || { echo 'Unknown installer option' >&2; exit 1; }
    init_service_env=yes
fi
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
release=$(realpath "$1")
case "$release" in /opt/workbench/releases/*) ;; *) echo 'Release must be under /opt/workbench/releases'; exit 1;; esac
[ -f "$release/uv.lock" ]
[ -f /etc/workbench/access.json ]
[ -f /etc/workbench/tunnel-token ]
getent passwd workbench >/dev/null || useradd --system --user-group --no-create-home --home-dir /var/lib/workbench --shell /usr/sbin/nologin workbench
getent passwd workbench-tunnel >/dev/null || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin workbench-tunnel
[ "$(id -u workbench)" -ne 0 ]
[ "$(id -u workbench-tunnel)" -ne 0 ]
[ ! -L /etc/workbench ] && [ -d /etc/workbench ] || { echo 'Unsafe service configuration directory' >&2; exit 1; }
chown root:workbench /etc/workbench
chmod 0750 /etc/workbench
"$release/deploy/check-service-env.sh" /etc/workbench/service.env "$release/deploy/service.env.example" 0 "$(getent group workbench | cut -d: -f3)" "$init_service_env"
cd "$release"
UV_PYTHON_INSTALL_DIR=/opt/workbench-runtime/python UV_CACHE_DIR=/opt/workbench-runtime/cache /opt/workbench-runtime/uv sync --frozen --no-dev --no-editable --python 3.12
chown -R root:root "$release"
chmod -R go-w "$release"
chown root:workbench /etc/workbench/access.json
chmod 0640 /etc/workbench/access.json
chmod 0600 /etc/workbench/tunnel-token
"$release/deploy/check-service-env.sh" /etc/workbench/service.env "$release/deploy/service.env.example" 0 "$(getent group workbench | cut -d: -f3)" no
database=$("$release/.venv/bin/python" "$release/deploy/service-database.py" /etc/workbench/service.env)
for dropin in /etc/systemd/system/workbench.service.d/*.conf \
              /run/systemd/system/workbench.service.d/*.conf \
              /usr/lib/systemd/system/workbench.service.d/*.conf \
              /lib/systemd/system/workbench.service.d/*.conf; do
    if [ -e "$dropin" ] || [ -L "$dropin" ]; then
        echo "Unsupported workbench.service override: $dropin" >&2
        exit 1
    fi
done
install -o root -g root -m 0644 deploy/workbench.service /etc/systemd/system/workbench.service
install -o root -g root -m 0644 deploy/workbench-tunnel.service /etc/systemd/system/workbench-tunnel.service
previous=none
if [ -L /opt/workbench/current ]; then
    previous=$(readlink -f /opt/workbench/current)
    case "$previous" in /opt/workbench/releases/*) ;; *) echo 'Unsafe previous release target' >&2; exit 1;; esac
elif [ -e /opt/workbench/current ]; then
    echo 'Current release is not a symlink' >&2
    exit 1
fi
if [ "$previous" != none ] && [ ! -e "$database" ] && [ ! -L "$database" ]; then
    echo "Configured database is missing: $database" >&2
    exit 1
fi
if [ "$previous" = none ]; then
    systemctl stop workbench.service 2>/dev/null || :
else
    systemctl stop workbench.service
fi
backup=none
if [ -e "$database" ] || [ -L "$database" ]; then
    if backup=$(runuser -u workbench -- "$release/.venv/bin/python" "$release/deploy/backup-state.py" --database "$database" --dest /var/lib/workbench/backups); then
        echo "Database source: $database; backup: $backup"
    else
        echo 'Database backup failed; release was not switched' >&2
        if [ "$previous" != none ]; then systemctl start workbench.service; fi
        exit 1
    fi
fi
umask 077
rollback_tmp=$(mktemp /opt/workbench/.rollback-target.XXXXXX)
printf '%s\n' "$previous" > "$rollback_tmp"
mv -Tf "$rollback_tmp" /opt/workbench/rollback-target
echo 'Previous release recorded in /opt/workbench/rollback-target'
ln -sfn "$release" /opt/workbench/current.next
mv -Tf /opt/workbench/current.next /opt/workbench/current
systemctl daemon-reload
systemctl enable --now workbench.service workbench-tunnel.service
systemctl restart workbench.service
attempt=0
while [ "$attempt" -lt 15 ]; do
    if curl --noproxy '*' --fail --silent --connect-timeout 1 --max-time 2 http://127.0.0.1:8027/healthz >/dev/null; then
        systemctl is-active workbench.service workbench-tunnel.service
        echo 'Workbench health check passed'
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done
echo 'Workbench health check failed. Inspect the service before manual rollback.' >&2
echo "Rollback target: /opt/workbench/rollback-target; database backup: $backup" >&2
echo 'To roll back: stop workbench.service; relink /opt/workbench/current to the recorded target;' >&2
echo 'restore the backup only if the new release migrated the database schema; restart workbench.service.' >&2
exit 1
