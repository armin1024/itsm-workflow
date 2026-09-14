from pathlib import Path


def test_installer_creates_and_renders_service_group():
    root = Path(__file__).parents[1]
    installer = (root / "packaging" / "install.sh").read_text()
    assert 'groupadd --system "$SERVICE_GROUP"' in installer
    assert 'usermod --gid "$SERVICE_GROUP" "$SERVICE_USER"' in installer
    assert 's|__SERVICE_GROUP__|$SERVICE_GROUP|g' in installer
    for name in ("api", "worker", "migrate"):
        template = (root / "packaging" / f"itsm-workflow-{name}.service.in").read_text()
        assert "Group=__SERVICE_GROUP__" in template


def test_release_targets_enterprise_linux_glibc():
    root = Path(__file__).parents[1]
    builder = (root / "packaging" / "build-linux-x86_64.sh").read_text()
    assert "--python-platform x86_64-manylinux_2_17" in builder
    assert "--only-binary :all:" in builder
    assert (root / "packaging" / "verify-glibc.py").is_file()
