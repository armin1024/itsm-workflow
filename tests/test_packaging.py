from pathlib import Path
import re


def test_installer_creates_and_renders_service_group():
    root = Path(__file__).parents[1]
    installer = (root / "packaging" / "install.sh").read_text()
    assert 'groupadd --system "$SERVICE_GROUP"' in installer
    assert 'usermod --gid "$SERVICE_GROUP" "$SERVICE_USER"' in installer
    assert 's|__SERVICE_GROUP__|$SERVICE_GROUP|g' in installer
    assert "._*" in installer and ".DS_Store" in installer
    assert "STUDIO_ADMIN_TOKEN=replace-with-long-random-admin-token" in installer
    for name in ("api",):
        template = (root / "packaging" / f"itsm-workflow-{name}.service.in").read_text()
        assert "Group=__SERVICE_GROUP__" in template
    assert not (root / "packaging" / "itsm-workflow-mcp.service.in").exists()
    assert not (root / "packaging" / "itsm-workflow-worker.service.in").exists()
    assert not (root / "packaging" / "itsm-workflow-migrate.service.in").exists()
    assert not (root / "packaging" / "itsm-workflow-compiler.service.in").exists()
    env_example = (root / "packaging" / "service.env.example").read_text()
    for removed in ("DATABASE_URL", "LANGGRAPH_DATABASE_URL", "WORKFLOW_ADMIN_UIDS", "WORKFLOW_OPERATOR_UIDS", "MCP_PORT", "EMBEDDING_BASE_URL", "RERANK_BASE_URL"):
        assert removed not in env_example
    assert "ReadWritePaths=/var/lib/itsm-workflow" in (root / "packaging" / "itsm-workflow-api.service.in").read_text()


def test_release_targets_enterprise_linux_glibc():
    root = Path(__file__).parents[1]
    builder = (root / "packaging" / "build-linux-x86_64.sh").read_text()
    assert "--python-platform x86_64-manylinux_2_17" in builder
    assert "--only-binary :all:" in builder
    assert "COPYFILE_DISABLE=1" in builder
    assert "._*" in builder and ".DS_Store" in builder and "--no-xattrs" in builder
    assert (root / "packaging" / "verify-glibc.py").is_file()
