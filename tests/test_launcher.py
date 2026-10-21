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
    container_run_options,
    game_url,
    parse_dev_exec,
    parse_image_command,
    parse_docker_run,
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
dev:
  - id: tracker
    title: Tracker
    description: Track a ball.
    git: https://github.com/example/tracker.git
    ref: main
    image: |-
      docker build -t newton-live .
      xhost +local:docker
    exec: docker run --rm -e DISPLAY=$DISPLAY --device /dev/video0 newton-live python live.py --camera 0
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
        publish_host=None,
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
        self.runs: list = []
        self.built: list[str] = []
        self.present_images: set[str] = set()
        self.images_removed: list[str] = []
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

    def image_exists(self, name: str) -> bool:
        return name in self.present_images

    def build_dev(self, context, build, cancelled=None) -> None:
        if cancelled is not None and cancelled.is_set():
            return
        self.built.append(build.tag)
        self.present_images.add(build.tag)

    def run_dev(self, repo, spec, cancelled=None) -> None:
        self.runs.append(spec)
        if cancelled is not None and cancelled.is_set():
            return
        self.running.add(container_name(repo.id))

    def remove_image(self, image: str) -> None:
        self.images_removed.append(image)

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


def test_local_mode_publishes_a_localhost_port(tmp_path: Path):
    from dataclasses import replace

    local = replace(settings(tmp_path), publish_host="127.0.0.1", public_scheme="http")
    game = replace(example_game(), host_port=8081)
    assert game_url(local, game) == "http://127.0.0.1:8081"
    options = container_run_options(game, local)
    assert options["ports"] == {"8080/tcp": ("127.0.0.1", 8081)}
    assert "network" not in options


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


def test_dev_launch_builds_a_missing_image_and_allows_the_display_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DISPLAY", ":0")
    calls: list[str] = []
    monkeypatch.setattr("app.launcher.allow_local_docker_display", lambda: calls.append("xhost"))
    git = FakeGit()
    docker = FakeDocker()

    def build_dev(context, build, cancelled=None) -> None:
        calls.append("build")
        FakeDocker.build_dev(docker, context, build, cancelled)

    docker.build_dev = build_dev
    control = manager(tmp_path, git=git, docker=docker)

    async def scenario():
        view = await control.start_dev("tracker")
        assert view["status"] == "starting"
        await control._tasks["tracker"]
        await control.start_dev("tracker")
        await control._tasks["tracker"]
        stopped = await control.stop_dev("tracker")
        cleaned = await control.clean_dev("tracker")
        return stopped, cleaned

    stopped, cleaned = asyncio.run(scenario())
    spec = docker.runs[0]
    assert spec.image == "newton-live"
    assert spec.command == ("python", "live.py", "--camera", "0")
    assert spec.environment == (("DISPLAY", ":0"),)
    assert spec.devices == ("/dev/video0",)
    assert git.synced == ["tracker"]
    assert docker.built == ["newton-live"]
    assert calls == ["build", "xhost"]
    assert stopped["status"] == "stopped"
    assert cleaned["error"] is None
    assert docker.images_removed == ["newton-live"]
    assert container_name("tracker") not in docker.running


def test_dev_launch_skips_the_build_when_the_image_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.launcher.allow_local_docker_display", lambda: None)
    git = FakeGit()
    docker = FakeDocker()
    docker.present_images.add("newton-live")
    control = manager(tmp_path, git=git, docker=docker)

    async def scenario():
        await control.start_dev("tracker")
        await control._tasks["tracker"]

    asyncio.run(scenario())
    assert git.synced == []
    assert docker.built == []
    assert docker.runs[0].image == "newton-live"


def test_dev_command_must_be_docker_run():
    with pytest.raises(LaunchError):
        parse_docker_run("python live.py")


def test_shipped_dev_command_names_the_newton_image(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISPLAY", ":1")
    repo = Catalog(ROOT / "catalog.yaml", ROOT / "catalog.schema.json").dev_repos()[0]
    spec = parse_dev_exec(repo.command).run
    build, allow_display = parse_image_command(repo.image_command)
    assert allow_display is True
    assert build.tag == "newton-live"
    assert repo.image_command.rstrip().endswith("xhost +local:docker")
    assert spec.image == "newton-live"
    assert spec.command == ("python", "live.py", "--max-poly-degree", "2")
    assert ("DISPLAY", ":1") in spec.environment
    assert spec.devices == ("/dev/video0",)
    assert spec.groups == ("video",)
    assert spec.volumes[0][:2] == ("/tmp/.X11-unix", "/tmp/.X11-unix")
