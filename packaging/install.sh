#!/usr/bin/env sh
set -eu

PREFIX=/opt/itsm-workflow
CONFIG_DIR=/etc/itsm-workflow
SERVICE_USER=itsmworkflow
SERVICE_GROUP=itsmworkflow
START=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX=$2; shift 2 ;;
    --config-dir) CONFIG_DIR=$2; shift 2 ;;
    --no-start) START=0; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || { echo "Linux x86_64 required" >&2; exit 1; }
case "$PREFIX" in /*) ;; *) echo "--prefix must be absolute" >&2; exit 2;; esac
case "$PREFIX" in /|/opt|/usr) echo "Unsafe prefix" >&2; exit 2;; esac
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

systemctl stop itsm-workflow-api 2>/dev/null || true
for obsolete in itsm-workflow-compiler itsm-workflow-mcp itsm-workflow-worker itsm-workflow-migrate; do
  systemctl disable --now "$obsolete" 2>/dev/null || true
  rm -f "/etc/systemd/system/$obsolete.service"
done
if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then groupadd --system "$SERVICE_GROUP"; fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir /var/lib/itsm-workflow --shell /usr/sbin/nologin "$SERVICE_USER"
elif [ "$(id -gn "$SERVICE_USER")" != "$SERVICE_GROUP" ]; then
  usermod --gid "$SERVICE_GROUP" "$SERVICE_USER"
fi
mkdir -p "$PREFIX" "$CONFIG_DIR" /var/lib/itsm-workflow
if [ -n "$(find "$PREFIX" -mindepth 1 -maxdepth 1 -print -quit)" ] && [ ! -f "$PREFIX/.itsm-workflow-install" ]; then
  echo "Refusing unmanaged non-empty prefix: $PREFIX" >&2; exit 1
fi
find "$PREFIX" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -R "$PACKAGE_DIR/app" "$PACKAGE_DIR/frontend" "$PACKAGE_DIR/python" "$PACKAGE_DIR/site-packages" "$PREFIX/"
cp "$PACKAGE_DIR/pyproject.toml" "$PACKAGE_DIR/uv.lock" "$PACKAGE_DIR/README.md" "$PREFIX/"
cp -R "$PACKAGE_DIR/docs" "$PREFIX/"
find "$PREFIX" \( -name '.DS_Store' -o -name '._*' -o -name '.AppleDouble' -o -name '__MACOSX' \) -prune -exec rm -rf {} +
touch "$PREFIX/.itsm-workflow-install"
if [ ! -f "$CONFIG_DIR/service.env" ]; then
  cp "$PACKAGE_DIR/service.env.example" "$CONFIG_DIR/service.env"
  chmod 0640 "$CONFIG_DIR/service.env"
  chown root:"$SERVICE_GROUP" "$CONFIG_DIR/service.env"
  echo "Created $CONFIG_DIR/service.env; configure AOPS/LLM values before TEST execution." >&2
fi
if ! grep -q '^STUDIO_ADMIN_TOKEN=' "$CONFIG_DIR/service.env"; then
  printf '\nSTUDIO_ADMIN_TOKEN=replace-with-long-random-admin-token\nSTUDIO_SESSION_HOURS=8\nSTUDIO_COOKIE_SECURE=false\n' >> "$CONFIG_DIR/service.env"
  echo "Added Studio authentication settings; configure STUDIO_ADMIN_TOKEN before start." >&2
fi
for name in api; do
  sed -e "s|__PREFIX__|$PREFIX|g" -e "s|__CONFIG_DIR__|$CONFIG_DIR|g" -e "s|__SERVICE_USER__|$SERVICE_USER|g" -e "s|__SERVICE_GROUP__|$SERVICE_GROUP|g" "$PACKAGE_DIR/itsm-workflow-$name.service.in" > "/etc/systemd/system/itsm-workflow-$name.service"
done
chmod 0644 /etc/systemd/system/itsm-workflow-api.service
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$PREFIX" /var/lib/itsm-workflow
systemctl daemon-reload
systemctl enable itsm-workflow-api
if [ "$START" -eq 1 ]; then
  if grep -q '^STUDIO_ADMIN_TOKEN=replace-' "$CONFIG_DIR/service.env"; then
    echo "Configure STUDIO_ADMIN_TOKEN in $CONFIG_DIR/service.env before start." >&2
    exit 0
  fi
  cli_path=$(awk -F= '/^AOPS_CLI_PATH=/{print $2}' "$CONFIG_DIR/service.env")
  [ -x "$cli_path" ] || { echo "AOPS_CLI_PATH is not executable: $cli_path" >&2; exit 1; }
  systemctl restart itsm-workflow-api
fi
echo "Installed ITSM Workflow Runtime Studio"
