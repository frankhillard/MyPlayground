"""Clone a game repository and run it as a container."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from app.catalog import Catalog, CatalogError, Game
from app.settings import Settings

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "playground-"
_REF_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")


class GameNotFound(Exception):
    pass


class AlreadyStarting(Exception):
    pass


class LaunchError(Exception):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class RuntimeUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ContainerState:
    running: bool
    public_error: str


def container_name(game_id: str) -> str:
    return f"{CONTAINER_PREFIX}{game_id}"


def game_url(settings: Settings, game: Game) -> str:
    if settings.publish_host:
        if game.host_port is None:
            return ""
        return f"{settings.public_scheme}://{settings.publish_host}:{game.host_port}"
    return f"{settings.public_scheme}://{game.id}.{settings.games_domain}"


def health_url(settings: Settings, game: Game) -> str:
    if settings.publish_host:
        if game.host_port is None:
            raise LaunchError("Set hostPort in the catalog to open this game on localhost.")
        return f"http://{settings.publish_host}:{game.host_port}{game.health_path}"
    return f"http://{container_name(game.id)}:{game.container_port}{game.health_path}"


def container_run_options(game: Game, settings: Settings) -> dict:
    options: dict = {
        "name": container_name(game.id),
        "detach": True,
        "mem_limit": settings.memory_limit,
        "nano_cpus": settings.nano_cpus,
        "pids_limit": settings.pids_limit,
        "init": True,
        "restart_policy": {"Name": "unless-stopped"},
    }
    if settings.publish_host:
        if game.host_port is None:
            raise LaunchError("Set hostPort in the catalog to open this game on localhost.")
        options["ports"] = {f"{game.container_port}/tcp": (settings.publish_host, game.host_port)}
        return options
    options["network"] = settings.docker_network
    options["labels"] = traefik_labels(game, settings)
    return options


def traefik_labels(game: Game, settings: Settings) -> dict[str, str]:
    router = container_name(game.id)
    host = f"{game.id}.{settings.games_domain}"
    return {
        "traefik.enable": "true",
        "traefik.docker.network": settings.docker_network,
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{router}.entrypoints": "websecure",
        f"traefik.http.routers.{router}.tls": "true",
        f"traefik.http.routers.{router}.tls.certresolver": "le",
        f"traefik.http.routers.{router}.service": router,
        f"traefik.http.services.{router}.loadbalancer.server.port": str(game.container_port),
    }


class GitOps:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def sync(self, game: Game) -> Path:
        if _REF_RE.fullmatch(game.ref) is None or any(char.isspace() for char in game.git):
            raise LaunchError("Could not fetch the game repository")
        root = self.settings.src_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        dest = root / game.id
        if dest.is_symlink() or (dest.exists() and not dest.resolve().is_relative_to(root)):
            raise LaunchError("Could not fetch the game repository")
        env = self._env()
        if (dest / ".git").is_dir():
            if not self._fetch(dest, game.ref, env):
                raise LaunchError("Could not fetch the game repository")
            self._run(["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"], env)
        else:
            if dest.exists():
                shutil.rmtree(dest)
            self._run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    game.ref,
                    "--",
                    game.git,
                    str(dest),
                ],
                env,
            )
        return dest

    def _fetch(self, dest: Path, ref: str, env: dict[str, str]) -> bool:
        branch = [
            "git",
            "-C",
            str(dest),
            "fetch",
            "--depth",
            "1",
            "origin",
            f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
        ]
        tag = [
            "git",
            "-C",
            str(dest),
            "fetch",
            "--depth",
            "1",
            "origin",
            f"+refs/tags/{ref}:refs/tags/{ref}",
        ]
        return self._ok(branch, env) or self._ok(tag, env)

    def _run(self, args: list[str], env: dict[str, str]) -> None:
        if not self._ok(args, env):
            raise LaunchError("Could not fetch the game repository")

    def _ok(self, args: list[str], env: dict[str, str]) -> bool:
        try:
            result = subprocess.run(
                args,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("git could not run: %s", exc)
            return False
        if result.returncode != 0:
            logger.warning("git exited %s: %s", result.returncode, result.stderr.strip())
            return False
        return True

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "safe.directory"
        env["GIT_CONFIG_VALUE_0"] = "*"
        key = self.settings.git_ssh_key_path
        if key:
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            )
        return env


class DockerOps:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def inspect(self, name: str) -> ContainerState | None:
        import docker

        try:
            container = docker.from_env().containers.get(name)
            container.reload()
        except docker.errors.NotFound:
            return None
        except docker.errors.DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc

        state = container.attrs.get("State", {})
        running = bool(state.get("Running"))
        if state.get("OOMKilled"):
            public_error = "The game ran out of memory"
        else:
            public_error = "The game container stopped"
        return ContainerState(running=running, public_error=public_error)

    def build_and_run(
        self,
        game: Game,
        context: Path,
        cancelled: threading.Event | None = None,
    ) -> None:
        root = context.resolve()
        dockerfile = _dockerfile_path(root, game.dockerfile)
        if cancelled is not None and cancelled.is_set():
            return
        import docker

        client = docker.from_env()
        image_tag = f"playground-game-{game.id}:latest"
        try:
            client.images.build(
                path=str(root),
                dockerfile=dockerfile,
                tag=image_tag,
                rm=True,
                forcerm=True,
            )
            if cancelled is not None and cancelled.is_set():
                return
            self.remove(container_name(game.id))
            client.containers.run(image_tag, **container_run_options(game, self.settings))
            if cancelled is not None and cancelled.is_set():
                self.remove(container_name(game.id))
        except LaunchError:
            raise
        except docker.errors.DockerException as exc:
            logger.warning("Docker failed for %s: %s", game.id, exc)
            raise LaunchError("Could not build or start the container") from exc

    def remove(self, name: str) -> None:
        import docker

        try:
            docker.from_env().containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            return
        except docker.errors.DockerException:
            logger.exception("Could not remove container %s", name)


def _dockerfile_path(context: Path, dockerfile: str) -> str:
    parts = Path(dockerfile).parts
    if not dockerfile or dockerfile.startswith("/") or ".." in parts:
        raise LaunchError("Dockerfile was not found in the repository")
    path = (context / dockerfile).resolve()
    if not path.is_relative_to(context) or not path.is_file():
        raise LaunchError("Dockerfile was not found in the repository")
    return path.relative_to(context).as_posix()


class LaunchManager:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        git: GitOps | None = None,
        docker: DockerOps | None = None,
        health_check=None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.git = git or GitOps(settings)
        self.docker = docker or DockerOps(settings)
        self._health_check = health_check or self._wait_until_healthy
        self._phases: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._lock = asyncio.Lock()

    def list_games(self) -> list[dict]:
        try:
            games = self.catalog.games()
        except CatalogError:
            raise
        return [self.describe(game) for game in games]

    def describe(self, game: Game) -> dict:
        status, error = self._status(game)
        return {
            "id": game.id,
            "title": game.title,
            "description": game.description,
            "status": status,
            "url": game_url(self.settings, game),
            "error": error,
        }

    async def start(self, game_id: str) -> dict:
        game = self._game(game_id)
        async with self._lock:
            task = self._tasks.get(game_id)
            if task is not None and not task.done():
                raise AlreadyStarting
            self._errors.pop(game_id, None)
            self._phases[game_id] = "cloning"
            cancelled = threading.Event()
            self._cancel_flags[game_id] = cancelled
            self._tasks[game_id] = asyncio.create_task(self._launch(game, cancelled))
        return self.describe(game)

    async def stop(self, game_id: str) -> dict:
        game = self._game(game_id)
        async with self._lock:
            task = self._tasks.get(game_id)
            cancelled = self._cancel_flags.get(game_id)
        if cancelled is not None:
            cancelled.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(self.docker.remove, container_name(game_id))
        self._errors.pop(game_id, None)
        self._phases.pop(game_id, None)
        return self.describe(game)

    async def _launch(self, game: Game, cancelled: threading.Event) -> None:
        try:
            if self.settings.publish_host and game.host_port is None:
                raise LaunchError("Set hostPort in the catalog to open this game on localhost.")
            self._phases[game.id] = "cloning"
            context = await asyncio.to_thread(self.git.sync, game)
            if cancelled.is_set():
                return
            self._phases[game.id] = "building"
            await asyncio.to_thread(self.docker.build_and_run, game, context, cancelled)
            if cancelled.is_set():
                await asyncio.to_thread(self.docker.remove, container_name(game.id))
                return
            healthy = await self._health_check(game)
            if cancelled.is_set() or not healthy:
                await asyncio.to_thread(self.docker.remove, container_name(game.id))
                if not cancelled.is_set():
                    self._errors[game.id] = "Game did not become healthy in time"
                return
            self._errors.pop(game.id, None)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.docker.remove, container_name(game.id))
            raise
        except LaunchError as exc:
            logger.warning("Launch failed for %s: %s", game.id, exc.public_message)
            await asyncio.to_thread(self.docker.remove, container_name(game.id))
            self._errors[game.id] = exc.public_message
        except Exception:
            logger.exception("Launch failed for %s", game.id)
            await asyncio.to_thread(self.docker.remove, container_name(game.id))
            self._errors[game.id] = "Launch failed"
        finally:
            self._phases.pop(game.id, None)

    def _status(self, game: Game) -> tuple[str, str | None]:
        phase = self._phases.get(game.id)
        if phase:
            return phase, None
        try:
            state = self.docker.inspect(container_name(game.id))
        except RuntimeUnavailable:
            logger.warning("Docker is unavailable while reading %s", game.id)
            return "error", "Container runtime is unavailable"
        if state is not None and state.running:
            return "running", None
        if state is not None:
            return "error", state.public_error
        stored = self._errors.get(game.id)
        if stored:
            return "error", stored
        return "stopped", None

    def _game(self, game_id: str) -> Game:
        try:
            game = self.catalog.get(game_id)
        except CatalogError:
            raise
        if game is None:
            raise GameNotFound
        return game

    async def _wait_until_healthy(self, game: Game) -> bool:
        import httpx

        url = health_url(self.settings, game)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.health_timeout_seconds
        async with httpx.AsyncClient() as client:
            while loop.time() < deadline:
                try:
                    response = await client.get(url, timeout=2.0)
                except httpx.HTTPError:
                    response = None
                if response is not None and response.status_code < 400:
                    return True
                await asyncio.sleep(1)
        return False
