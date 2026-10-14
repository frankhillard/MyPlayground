import asyncio
import subprocess
import threading
from pathlib import Path

import pytest

from app.catalog import Catalog, Game
from app.launcher import (
    AlreadyStarting,
    ContainerState,
    GitOps,
    LaunchError,
    LaunchManager,
    container_name,
    traefik_labels,
)
from app.settings import Settings, SettingsError, load_settings

ROOT = Path(__file__).resolve().parents[1]


def fixture_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(
        """
games:
  - id: example
    title: Example
    description: A sample game
    git: https://github.com/example/game.git
    ref: main
    dockerfile: Dockerfile
    containerPort: 8080
    healthPath: /
""",
        encoding="utf-8",
    )
    return path


def settings(tmp_path: Path) -> Settings:
    return Settings(
        catalog_path=fixture_catalog(tmp_path),
        schema_path=ROOT / "catalog.schema.json",
        src_dir=tmp_path / "src",
        games_domain="play.example.com",
        admin_token="test-token",
        docker_network="playground",
        public_scheme="https",
        memory_limit="512m",
        cpu_limit=0.5,
        pids_limit=256,
        health_timeout_seconds=5,
        git_ssh_key_path=None,
    )


def example_game() -> Game:
    return Game(
        id="example",
        title="Example",
        description="A sample game",
        git="https://github.com/example/game.git",
        ref="main",
        dockerfile="Dockerfile",
        container_port=8080,
        health_path="/",
    )


class FakeGit:
    def __init__(self) -> None:
        self.synced: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def sync(self, game: Game) -> Path:
        self.synced.append(game.id)
        if self.block:
            self.entered.set()
            if not self.release.wait(5):
                raise LaunchError("Could not fetch the game repository")
        return Path("/tmp") / game.id


class FakeDocker:
    def __init__(self) -> None:
        self.running: set[str] = set()
        self.removed: list[str] = []
        self.fail = False

    def inspect(self, name: str) -> ContainerState | None:
        if name in self.running:
            return ContainerState(running=True, public_error="")
        return None

    def build_and_run(self, game: Game, context: Path, cancelled=None) -> None:
        if self.fail:
            raise LaunchError("Could not build or start the container")
        if cancelled is not None and cancelled.is_set():
            return
        self.running.add(container_name(game.id))
        if cancelled is not None and cancelled.is_set():
            self.remove(container_name(game.id))

    def remove(self, name: str) -> None:
        self.removed.append(name)
        self.running.discard(name)


def manager(tmp_path: Path, git: FakeGit | None = None, docker: FakeDocker | None = None, healthy=True):
    async def health(_game):
        return healthy

    cfg = settings(tmp_path)
    return LaunchManager(
        cfg,
        Catalog(cfg.catalog_path, cfg.schema_path),
        git=git or FakeGit(),
        docker=docker or FakeDocker(),
        health_check=health,
    )


def test_labels_publish_the_game_host(tmp_path: Path):
    labels = traefik_labels(example_game(), settings(tmp_path))
    assert labels["traefik.http.routers.playground-example.rule"] == "Host(`example.play.example.com`)"
    assert labels["traefik.http.services.playground-example.loadbalancer.server.port"] == "8080"
    assert labels["traefik.docker.network"] == "playground"
    assert "git" not in "".join(labels.values())


def test_host_network_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GAMES_DOMAIN", "play.example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DOCKER_NETWORK", "host")
    with pytest.raises(SettingsError):
        load_settings()


def test_clone_uses_the_catalog_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    GitOps(settings(tmp_path)).sync(example_game())
    assert calls[0][:6] == ["git", "clone", "--depth", "1", "--branch", "main"]
    assert calls[0][-2:] == ["https://github.com/example/game.git", str((tmp_path / "src").resolve() / "example")]


def test_existing_clone_is_fetched_then_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dest = tmp_path / "src" / "example"
    (dest / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    GitOps(settings(tmp_path)).sync(example_game())
    assert "fetch" in calls[0]
    assert calls[0][-1] == "+refs/heads/main:refs/remotes/origin/main"
    assert calls[1][-3:] == ["reset", "--hard", "FETCH_HEAD"]


def test_unsafe_ref_is_not_fetched(tmp_path: Path):
    game = example_game()
    game = Game(**{**game.__dict__, "ref": "main;touch"})
    with pytest.raises(LaunchError):
        GitOps(settings(tmp_path)).sync(game)


def test_start_builds_and_reports_running(tmp_path: Path):
    docker = FakeDocker()
    git = FakeGit()
    control = manager(tmp_path, git, docker)

    async def scenario():
        view = await control.start("example")
        assert view["status"] == "cloning"
        assert "git" not in view
        await control._tasks["example"]
        return control.describe(control.catalog.get("example"))

    view = asyncio.run(scenario())
    assert view["status"] == "running"
    assert view["url"] == "https://example.play.example.com"
    assert view["error"] is None
    assert git.synced == ["example"]
    assert container_name("example") in docker.running


def test_second_start_while_launching_is_rejected(tmp_path: Path):
    git = FakeGit()
    git.block = True
    control = manager(tmp_path, git, FakeDocker())

    async def scenario():
        await control.start("example")
        for _ in range(50):
            if git.entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert git.entered.is_set()
        with pytest.raises(AlreadyStarting):
            await control.start("example")
        git.release.set()
        await control._tasks["example"]

    asyncio.run(scenario())


def test_unhealthy_game_is_removed(tmp_path: Path):
    docker = FakeDocker()
    control = manager(tmp_path, FakeGit(), docker, healthy=False)

    async def scenario():
        await control.start("example")
        await control._tasks["example"]
        return control.describe(control.catalog.get("example"))

    view = asyncio.run(scenario())
    assert view["status"] == "error"
    assert view["error"] == "Game did not become healthy in time"
    assert container_name("example") not in docker.running
    assert container_name("example") in docker.removed


def test_stop_removes_the_container_and_keeps_the_clone(tmp_path: Path):
    docker = FakeDocker()
    docker.running.add(container_name("example"))
    control = manager(tmp_path, FakeGit(), docker)

    async def scenario():
        return await control.stop("example")

    view = asyncio.run(scenario())
    assert view["status"] == "stopped"
    assert view["error"] is None
    assert docker.removed == [container_name("example")]
    assert not (tmp_path / "src" / "example").exists()
