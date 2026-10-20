from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.catalog import Catalog
from app.launcher import LaunchManager
from tests.test_launcher import FakeDocker, FakeGit, settings


def client(tmp_path: Path) -> TestClient:
    cfg = settings(tmp_path)
    control = LaunchManager(
        cfg,
        Catalog(cfg.catalog_path, cfg.schema_path),
        git=FakeGit(),
        docker=FakeDocker(),
        health_check=_healthy,
    )
    return TestClient(create_app(control))


async def _healthy(_game):
    return True


def test_dashboard_lists_launch_stop_and_open(tmp_path: Path):
    response = client(tmp_path).get("/")
    assert response.status_code == 200
    page = response.text
    assert "Launch" in page
    assert "Stop" in page
    assert "Play" in page


def test_list_is_public_and_hides_the_git_url(tmp_path: Path):
    response = client(tmp_path).get("/api/games")
    assert response.status_code == 200
    body = response.json()
    assert body["games"][0]["id"] == "example"
    assert body["games"][0]["status"] == "stopped"
    assert body["games"][0]["url"] == "https://example.play.example.com"
    assert body["games"][0]["image"] is None
    assert "github.com" not in response.text


def test_game_picture_is_served(tmp_path: Path):
    pictures = tmp_path / "pictures"
    pictures.mkdir()
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    (pictures / "example.png").write_bytes(png)
    api = client(tmp_path)
    listed = api.get("/api/games").json()["games"][0]
    assert listed["image"] == "/api/games/example/image"
    image = api.get(listed["image"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.content == png


def test_start_and_stop_require_the_admin_token(tmp_path: Path):
    api = client(tmp_path)
    denied = api.post("/api/games/example/start")
    assert denied.status_code == 401

    wrong = api.post(
        "/api/games/example/start",
        headers={"Authorization": "Bearer nope"},
    )
    assert wrong.status_code == 401

    started = api.post(
        "/api/games/example/start",
        headers={"Authorization": "Bearer test-token"},
    )
    assert started.status_code == 202
    assert started.json()["status"] == "cloning"

    unknown = api.post(
        "/api/games/missing/stop",
        headers={"Authorization": "Bearer test-token"},
    )
    assert unknown.status_code == 404
