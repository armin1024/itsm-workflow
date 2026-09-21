from pathlib import Path
import re

from app import main
from app.config import Settings
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient


def test_base_path_is_normalized_and_rejects_unsafe_values():
    assert Settings(workflow_base_path="aops/itsm-workflow/").workflow_base_path == "/aops/itsm-workflow"
    try:
        Settings(workflow_base_path="/aops/<script>")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe base path must be rejected")


def test_spa_injects_runtime_base_and_build_uses_relative_assets(monkeypatch):
    monkeypatch.setattr(main.settings, "workflow_base_path", "/aops/itsm-workflow")
    response = main.spa_index()
    text = response.body.decode()
    assert '<base href="/aops/itsm-workflow/">' in text
    assert 'window.__ITSM_WORKFLOW_CONFIG__={"basePath":"/aops/itsm-workflow"}' in text
    built = Path("frontend/dist/index.html").read_text()
    assert 'src="./assets/' in built and 'href="./assets/' in built


def test_subpath_proxy_serves_spa_assets_and_api(monkeypatch):
    monkeypatch.setattr(main.settings, "workflow_base_path", "/aops/itsm-workflow")
    proxy = Starlette(routes=[Mount("/aops/itsm-workflow", app=main.app)])
    with TestClient(proxy) as client:
        page = client.get("/aops/itsm-workflow/studio")
        assert page.status_code == 200
        asset = re.search(r'src="(\./assets/[^"]+\.js)"', page.text).group(1).removeprefix("./")
        response = client.get("/aops/itsm-workflow/" + asset)
        assert response.status_code == 200 and "javascript" in response.headers["content-type"]
        assert client.get("/aops/itsm-workflow/api/v1/health").status_code == 200
