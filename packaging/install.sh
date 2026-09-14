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

systemctl stop itsm-workflow-mcp itsm-workflow-api itsm-workflow-worker itsm-workflow-migrate 2>/dev/null || true
if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_GROUP"
fi
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
cp -R "$PACKAGE_DIR/app" "$PACKAGE_DIR/frontend" "$PACKAGE_DIR/migrations" "$PACKAGE_DIR/python" "$PACKAGE_DIR/site-packages" "$PREFIX/"
cp "$PACKAGE_DIR/alembic.ini" "$PACKAGE_DIR/pyproject.toml" "$PACKAGE_DIR/uv.lock" "$PACKAGE_DIR/README.md" "$PREFIX/"
cp -R "$PACKAGE_DIR/docs" "$PREFIX/"
touch "$PREFIX/.itsm-workflow-install"
if [ ! -f "$CONFIG_DIR/service.env" ]; then
  cp "$PACKAGE_DIR/service.env.example" "$CONFIG_DIR/service.env"
  chmod 0640 "$CONFIG_DIR/service.env"
  chown root:"$SERVICE_USER" "$CONFIG_DIR/service.env"
  echo "Created $CONFIG_DIR/service.env; configure required values before start." >&2
fi
if [ ! -f "$CONFIG_DIR/mcp.env" ]; then
  awk -F= '/^(ENVIRONMENT|WORKFLOW_API_TOKEN|WORKFLOW_PUBLIC_URL|MCP_INTERNAL_API_URL|MCP_BIND_HOST|MCP_PORT|MCP_PATH|MCP_ALLOWED_HOSTS|MCP_WAIT_MAX_SECONDS)=/{print}' "$CONFIG_DIR/service.env" > "$CONFIG_DIR/mcp.env"
  grep -q '^WORKFLOW_PUBLIC_URL=' "$CONFIG_DIR/mcp.env" || echo 'WORKFLOW_PUBLIC_URL=http://127.0.0.1:8089' >> "$CONFIG_DIR/mcp.env"
  grep -q '^MCP_INTERNAL_API_URL=' "$CONFIG_DIR/mcp.env" || echo 'MCP_INTERNAL_API_URL=http://127.0.0.1:8089/api/v1' >> "$CONFIG_DIR/mcp.env"
  grep -q '^MCP_BIND_HOST=' "$CONFIG_DIR/mcp.env" || echo 'MCP_BIND_HOST=127.0.0.1' >> "$CONFIG_DIR/mcp.env"
  grep -q '^MCP_PORT=' "$CONFIG_DIR/mcp.env" || echo 'MCP_PORT=8090' >> "$CONFIG_DIR/mcp.env"
  grep -q '^MCP_PATH=' "$CONFIG_DIR/mcp.env" || echo 'MCP_PATH=/mcp' >> "$CONFIG_DIR/mcp.env"
  grep -q '^MCP_ALLOWED_HOSTS=' "$CONFIG_DIR/mcp.env" || echo 'MCP_ALLOWED_HOSTS=127.0.0.1:*,localhost:*' >> "$CONFIG_DIR/mcp.env"
  grep -q '^MCP_WAIT_MAX_SECONDS=' "$CONFIG_DIR/mcp.env" || echo 'MCP_WAIT_MAX_SECONDS=15' >> "$CONFIG_DIR/mcp.env"
  chmod 0640 "$CONFIG_DIR/mcp.env"
  chown root:"$SERVICE_USER" "$CONFIG_DIR/mcp.env"
fi
for name in api worker migrate mcp; do
  sed -e "s|__PREFIX__|$PREFIX|g" -e "s|__CONFIG_DIR__|$CONFIG_DIR|g" -e "s|__SERVICE_USER__|$SERVICE_USER|g" -e "s|__SERVICE_GROUP__|$SERVICE_GROUP|g" "$PACKAGE_DIR/itsm-workflow-$name.service.in" > "/etc/systemd/system/itsm-workflow-$name.service"
done
chmod 0644 /etc/systemd/system/itsm-workflow-*.service
chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX" /var/lib/itsm-workflow
systemctl daemon-reload
systemctl enable itsm-workflow-migrate itsm-workflow-api itsm-workflow-worker itsm-workflow-mcp
if [ "$START" -eq 1 ]; then
  if grep -q 'replace-' "$CONFIG_DIR/service.env" || grep -q 'replace-' "$CONFIG_DIR/mcp.env"; then
    echo "Configuration placeholders remain; installed but not started." >&2
    exit 0
  fi
  cli_path=$(awk -F= '/^AOPS_CLI_PATH=/{print $2}' "$CONFIG_DIR/service.env")
  [ -x "$cli_path" ] || { echo "AOPS_CLI_PATH is not executable: $cli_path" >&2; exit 1; }
  systemctl start itsm-workflow-migrate
  systemctl start itsm-workflow-api itsm-workflow-worker itsm-workflow-mcp
fi
echo "Installed ITSM Workflow"
