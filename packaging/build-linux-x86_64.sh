#!/usr/bin/env sh
set -eu
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || { echo "Linux x86_64 build host required" >&2; exit 1; }
VERSION=${1:-0.6.3}
OUTPUT=${2:-dist}
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STAGE="$ROOT/.package-stage/itsm-workflow-$VERSION-linux-x86_64"
RUNTIME="$ROOT/.package-python"
REQ="$ROOT/.package-requirements.txt"
ARCHIVE="$OUTPUT/itsm-workflow-$VERSION-linux-x86_64.tar.gz"
rm -rf "$STAGE" "$RUNTIME" "$REQ"
mkdir -p "$STAGE/frontend" "$OUTPUT"
cd "$ROOT"
[ -f frontend/dist/index.html ] || { echo "Run frontend build before packaging" >&2; exit 1; }
uv python install --install-dir "$RUNTIME" 3.12
PYTHON=$(find "$RUNTIME" -path '*/bin/python3' -print -quit)
PYTHON_ROOT=$(dirname "$(dirname "$PYTHON")")
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file "$REQ" >/dev/null
# Force the oldest supported enterprise Linux ABI. Without this flag uv sees
# the Debian build container's newer glibc and may select incompatible wheels.
uv pip install --python "$PYTHON" --python-platform x86_64-manylinux_2_17 --only-binary :all: --target "$STAGE/site-packages" --requirement "$REQ"
cp -R app migrations docs "$STAGE/"
cp -R frontend/dist "$STAGE/frontend/"
cp -R "$PYTHON_ROOT" "$STAGE/python"
cp pyproject.toml uv.lock alembic.ini README.md "$STAGE/"
cp packaging/install.sh packaging/service.env.example packaging/mcp.env.example packaging/itsm-workflow-api.service.in packaging/itsm-workflow-worker.service.in packaging/itsm-workflow-migrate.service.in packaging/itsm-workflow-mcp.service.in packaging/verify-release.py packaging/verify-glibc.py "$STAGE/"
# Never ship macOS Finder or AppleDouble metadata when the source workspace is
# prepared on macOS and mounted into the Linux release builder.
find "$STAGE" \( -name '.DS_Store' -o -name '._*' -o -name '.AppleDouble' -o -name '__MACOSX' \) -prune -exec rm -rf {} +
PYTHONPATH="$STAGE/site-packages:$STAGE" "$STAGE/python/bin/python3" "$STAGE/verify-release.py"
PYTHONPATH="$STAGE/site-packages:$STAGE" "$STAGE/python/bin/python3" "$ROOT/packaging/verify-glibc.py" "$STAGE"
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
COPYFILE_DISABLE=1 tar --no-xattrs --no-acls --no-selinux -C "$(dirname "$STAGE")" -czf "$ARCHIVE" "$(basename "$STAGE")"
(cd "$OUTPUT" && sha256sum "$(basename "$ARCHIVE")") > "$ARCHIVE.sha256"
rm -rf "$ROOT/.package-stage" "$RUNTIME" "$REQ"
echo "Created $ARCHIVE"
