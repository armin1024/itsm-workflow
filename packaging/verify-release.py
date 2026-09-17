import os
import asyncio
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    package_root = Path(__file__).parent
    forbidden_metadata = [path for path in package_root.rglob("*") if path.name == ".DS_Store" or path.name.startswith("._") or path.name in {".AppleDouble", "__MACOSX"}]
    assert not forbidden_metadata, f"macOS metadata found in release: {forbidden_metadata[:5]}"
    static_root = package_root / "frontend" / "dist"
    if not static_root.is_dir():
        static_root = package_root.parent / "frontend" / "dist"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + directory + "/verify.db"
    os.environ["STATIC_DIR"] = str(static_root)
    from fastapi.testclient import TestClient
    from app import __version__
    from app.main import app
    from app.mcp_server import mcp
    from mcp import Client

    installer = (package_root / "install.sh").read_text()
    assert 's|__SERVICE_GROUP__|$SERVICE_GROUP|g' in installer
    assert 'groupadd --system "$SERVICE_GROUP"' in installer
    assert '"$CONFIG_DIR/mcp.env"' in installer
    assert (package_root / "mcp.env.example").is_file()
    for unit in ("api", "worker", "migrate", "mcp"):
        rendered = (package_root / f"itsm-workflow-{unit}.service.in").read_text().replace("__SERVICE_GROUP__", "itsmworkflow")
        assert "Group=itsmworkflow" in rendered and "__SERVICE_GROUP__" not in rendered

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["version"] == __version__
        page = client.get("/")
        assert page.status_code == 200 and "ITSM Workflow" in page.text
        assert '<base href="/">' in page.text and 'window.__ITSM_WORKFLOW_CONFIG__={"basePath":""}' in page.text
        assert client.get("/api/v1/runs").status_code == 401
        metrics = client.get("/metrics")
        assert metrics.status_code == 200 and "itsm_workflow_runs" in metrics.text
    async def verify_mcp() -> None:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {item.name for item in tools.tools}
            assert {"knowledge_match", "workflow_plan", "workflow_run_wait", "workflow_run_cancel"} <= names
    asyncio.run(verify_mcp())
    print("Release acceptance passed", __version__)
