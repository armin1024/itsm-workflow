import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    package_root = Path(__file__).parent
    forbidden = [path for path in package_root.rglob("*") if path.name == ".DS_Store" or path.name.startswith("._") or path.name in {".AppleDouble", "__MACOSX"}]
    assert not forbidden, f"macOS metadata found: {forbidden[:5]}"
    static_root = package_root / "frontend" / "dist"
    if not static_root.is_dir(): static_root = package_root.parent / "frontend" / "dist"
    os.environ.update({"ENVIRONMENT": "test", "STATIC_DIR": str(static_root), "STUDIO_DATABASE_PATH": directory + "/studio.db"})
    from fastapi.testclient import TestClient
    from app import __version__
    from app.main import app

    installer = (package_root / "install.sh").read_text()
    assert "itsm-workflow-mcp" in installer and "disable --now" in installer
    assert not (package_root / "mcp.env.example").exists()
    assert not any(package_root.glob("itsm-workflow-mcp.service*"))
    assert not any(package_root.glob("itsm-workflow-worker.service*"))
    assert not any(package_root.glob("itsm-workflow-migrate.service*"))
    for unit in ("api", "compiler"):
        rendered = (package_root / f"itsm-workflow-{unit}.service.in").read_text().replace("__SERVICE_GROUP__", "itsmworkflow")
        assert "Group=itsmworkflow" in rendered
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200 and health.json()["version"] == __version__
        assert client.get("/api/v1/studio/catalog").status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 404
        assert client.get("/mcp").status_code != 200
        page = client.get("/")
        assert page.status_code == 200 and "ITSM Workflow" in page.text
    print("Release acceptance passed", __version__)
