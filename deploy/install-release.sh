#!/bin/sh
# Usage: sudo install-release.sh /path/to/extracted-release
# Existing worklog, nginx, other applications, and system Python are untouched.
set -eu
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
chown root:workbench /etc/workbench
chmod 0750 /etc/workbench
cd "$release"
UV_PYTHON_INSTALL_DIR=/opt/workbench-runtime/python UV_CACHE_DIR=/opt/workbench-runtime/cache /opt/workbench-runtime/uv sync --frozen --no-dev --no-editable --python 3.12
chown -R root:root "$release"
chmod -R go-w "$release"
install -o root -g workbench -m 0640 deploy/service.env.example /etc/workbench/service.env
chown root:workbench /etc/workbench/access.json
chmod 0640 /etc/workbench/access.json
chmod 0600 /etc/workbench/tunnel-token
install -o root -g root -m 0644 deploy/workbench.service /etc/systemd/system/workbench.service
install -o root -g root -m 0644 deploy/workbench-tunnel.service /etc/systemd/system/workbench-tunnel.service
ln -sfn "$release" /opt/workbench/current.next
mv -Tf /opt/workbench/current.next /opt/workbench/current
systemctl daemon-reload
systemctl enable --now workbench.service workbench-tunnel.service
systemctl restart workbench.service
systemctl is-active workbench.service workbench-tunnel.service
