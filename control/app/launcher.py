"""Clone a game repository and run it as a container."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from app.catalog import Catalog, CatalogError, DevRepo, Game
from app.settings import Settings

logger = logging.getLogger(__name__)

CONTAINER_PREFIX = "playground-"
_REF_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")


class GameNotFound(Exception):
    pass


class DevNotFound(Exception):
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


@dataclass(frozen=True)
class DockerRun:
    image: str
    command: tuple[str, ...]
    devices: tuple[str, ...]
    groups: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    volumes: tuple[tuple[str, str, str], ...]
    auto_remove: bool


@dataclass(frozen=True)
class DockerBuild:
    tag: str
    dockerfile: str


@dataclass(frozen=True)
class DevExec:
    allow_display: bool
    run: DockerRun


_ENV_REF = re.compile(r"\$(\w+)|\$\{(\w+)\}")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,200}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GROUP_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SWITCHES = {"--rm", "-d", "--detach", "--init", "--interactive", "--tty"}
_VALUE_FLAGS = {
    "--device": "device",
    "--group-add": "group",
    "-e": "env",
    "--env": "env",
    "-v": "volume",
    "--volume": "volume",
    "--name": "name",
}


def container_name(game_id: str) -> str:
    return f"{CONTAINER_PREFIX}{game_id}"


def parse_dev_exec(command: str) -> DevExec:
    """Read the catalog exec text. It is the docker run command."""
    return DevExec(allow_display=False, run=parse_docker_run(command))


def parse_image_command(command: str) -> tuple[DockerBuild, bool]:
    """Read the catalog image text. xhost, when present, follows the docker build."""
    lines = [line for line in command.splitlines() if line.strip()]
    allow_display = False
    if lines and lines[-1].strip() == "xhost +local:docker":
        allow_display = True
        lines = lines[:-1]
    if any(line.strip() == "xhost +local:docker" for line in lines):
        raise LaunchError("xhost must follow the docker build")
    if not lines:
        raise LaunchError("The image command must be a docker build")
    return parse_docker_build("\n".join(lines)), allow_display


def parse_docker_build(command: str) -> DockerBuild:
    """Read a catalog ``docker build`` command. The context is the cloned repository."""
    try:
        argv = shlex.split(command.strip(), posix=True)
    except ValueError as exc:
        raise LaunchError("The image command could not be read") from exc
    if len(argv) < 3 or argv[0] != "docker" or argv[1] != "build":
        raise LaunchError("The image command must be a docker build")
    tag: str | None = None
    dockerfile = "Dockerfile"
    context: str | None = None
    index = 2
    while index < len(argv):
        token = argv[index]
        if token in {"-t", "--tag"}:
            if index + 1 >= len(argv):
                raise LaunchError("The image command is missing a tag")
            tag = argv[index + 1]
            index += 2
            continue
        if token.startswith("--tag="):
            tag = token.split("=", 1)[1]
            index += 1
            continue
        if token in {"-f", "--file"}:
            if index + 1 >= len(argv):
                raise LaunchError("The image command is missing a Dockerfile")
            dockerfile = argv[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            raise LaunchError("The image command uses an unsupported option")
        if context is not None:
            raise LaunchError("The image command must build the repository")
        context = token
        index += 1
    if not tag or _IMAGE_RE.fullmatch(tag) is None:
        raise LaunchError("The image command must name an image tag")
    if context != ".":
        raise LaunchError("The image command must build the repository")
    return DockerBuild(tag=tag, dockerfile=dockerfile)


def allow_local_docker_display() -> None:
    try:
        result = subprocess.run(
            ["xhost", "+local:docker"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("xhost could not run: %s", exc)
        raise LaunchError("Could not allow Docker to use the display") from exc
    if result.returncode != 0:
        logger.warning("xhost exited %s: %s", result.returncode, result.stderr.strip())
        raise LaunchError("Could not allow Docker to use the display")


def parse_docker_run(command: str) -> DockerRun:
    """Read a catalog ``docker run`` command. The browser never sends this text."""
    text = re.sub(r"\\\s*\n", " ", command.strip())
    try:
        argv = shlex.split(text, posix=True)
    except ValueError as exc:
        raise LaunchError("The dev command could not be read") from exc
    if len(argv) < 3 or argv[0] != "docker" or argv[1] != "run":
        raise LaunchError("The dev command must be a docker run")

    devices: list[str] = []
    groups: list[str] = []
    environment: list[tuple[str, str]] = []
    volumes: list[tuple[str, str, str]] = []
    auto_remove = False
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in _SWITCHES or token in {"-i", "-t", "-it", "-d"}:
            auto_remove = auto_remove or token == "--rm"
            index += 1
            continue
        if token.startswith("-") and not token.startswith("--") and set(token[1:]) <= {"i", "t", "d"}:
            index += 1
            continue
        kind = _VALUE_FLAGS.get(token)
        if kind is None and token.startswith("--name="):
            index += 1
            continue
        if kind is None:
            if token.startswith("-"):
                raise LaunchError("The dev command uses an unsupported option")
            break
        if index + 1 >= len(argv):
            raise LaunchError("The dev command is missing an option value")
        value = argv[index + 1]
        index += 2
        if kind == "name":
            continue
        if kind == "device":
            devices.append(_device(value))
        elif kind == "group":
            if _GROUP_RE.fullmatch(value) is None:
                raise LaunchError("The dev command has an invalid group")
            groups.append(value)
        elif kind == "env":
            environment.append(_env_entry(value))
        else:
            volumes.append(_volume(value))
    if index >= len(argv):
        raise LaunchError("The dev command must name a docker image")
    image = argv[index]
    if _IMAGE_RE.fullmatch(image) is None:
        raise LaunchError("The dev command names an invalid image")
    return DockerRun(
        image=image,
        command=tuple(argv[index + 1 :]),
        devices=tuple(devices),
        groups=tuple(groups),
        environment=tuple(environment),
        volumes=tuple(volumes),
        auto_remove=auto_remove,
    )


def _expand_env(value: str) -> str:
    return _ENV_REF.sub(lambda match: os.environ.get(match.group(1) or match.group(2), ""), value)


def _device(value: str) -> str:
    path = value.split(":", 1)[0]
    if not path.startswith("/dev/") or ".." in Path(path).parts:
        raise LaunchError("The dev command has an invalid device")
    return value


def _env_entry(value: str) -> tuple[str, str]:
    key, separator, raw = value.partition("=")
    if _ENV_KEY_RE.fullmatch(key) is None:
        raise LaunchError("The dev command has an invalid environment name")
    if separator:
        return key, _expand_env(raw)
    return key, os.environ.get(key, "")


def _volume(value: str) -> tuple[str, str, str]:
    parts = value.split(":")
    if len(parts) == 2:
        host, container, mode = parts[0], parts[1], "rw"
    elif len(parts) == 3 and parts[2] in {"rw", "ro"}:
        host, container, mode = parts
    else:
        raise LaunchError("The dev command has an invalid volume")
    for path in (host, container):
        if not path.startswith("/") or ".." in Path(path).parts:
            raise LaunchError("The dev command has an invalid volume")
    return host, container, mode


def picture_file(settings: Settings, game_id: str) -> Path | None:
    root = settings.resolved_pictures_dir().resolve()
    path = (root / f"{game_id}.png").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _image_url(settings: Settings, game_id: str) -> str | None:
    if picture_file(settings, game_id) is None:
        return None
    return f"/api/games/{game_id}/image"


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

    def sync(self, game: Game | DevRepo) -> Path:
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
            public_error = "The container ran out of memory"
        else:
            public_error = "The container stopped"
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

    def image_exists(self, name: str) -> bool:
        import docker

        try:
            docker.from_env().images.get(name)
        except docker.errors.ImageNotFound:
            return False
        except docker.errors.DockerException as exc:
            logger.warning("Docker is unavailable while looking for %s: %s", name, exc)
            raise LaunchError("Container runtime is unavailable") from exc
        return True

    def build_dev(self, context: Path, build: DockerBuild, cancelled: threading.Event | None = None) -> None:
        root = context.resolve()
        dockerfile = _dockerfile_path(root, build.dockerfile)
        if cancelled is not None and cancelled.is_set():
            return
        import docker

        try:
            docker.from_env().images.build(
                path=str(root),
                dockerfile=dockerfile,
                tag=build.tag,
                rm=True,
                forcerm=True,
            )
        except docker.errors.DockerException as exc:
            logger.warning("Docker build failed for %s: %s", build.tag, exc)
            raise LaunchError("Could not build the image") from exc

    def run_dev(self, repo: DevRepo, spec: DockerRun, cancelled: threading.Event | None = None) -> None:
        if cancelled is not None and cancelled.is_set():
            return
        import docker

        self.remove(container_name(repo.id))
        if cancelled is not None and cancelled.is_set():
            return
        volumes = {
            host: {"bind": container, "mode": mode}
            for host, container, mode in spec.volumes
        }
        try:
            docker.from_env().containers.run(
                spec.image,
                list(spec.command) or None,
                name=container_name(repo.id),
                detach=True,
                auto_remove=spec.auto_remove,
                devices=list(spec.devices) or None,
                group_add=list(spec.groups) or None,
                environment=dict(spec.environment) or None,
                volumes=volumes or None,
            )
        except docker.errors.ImageNotFound as exc:
            raise LaunchError(f"Docker image {spec.image} was not found") from exc
        except docker.errors.DockerException as exc:
            logger.warning("Docker failed for %s: %s", repo.id, exc)
            raise LaunchError("Could not start the container") from exc
        if cancelled is not None and cancelled.is_set():
            self.remove(container_name(repo.id))

    def remove_image(self, image: str) -> None:
        import docker

        try:
            docker.from_env().images.remove(image, force=True)
        except docker.errors.ImageNotFound:
            return
        except docker.errors.DockerException as exc:
            logger.warning("Could not delete image %s: %s", image, exc)
            raise LaunchError("Could not delete the image") from exc

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
        self._display_ready = False
        self._display_lock = threading.Lock()

    def list_games(self) -> list[dict]:
        return [self.describe(game) for game in self.catalog.games()]

    def list_dev(self) -> list[dict]:
        return [self.describe_dev(repo) for repo in self.catalog.dev_repos()]

    def describe(self, game: Game) -> dict:
        status, error = self._status(game.id)
        return {
            "id": game.id,
            "title": game.title,
            "description": game.description,
            "status": status,
            "url": game_url(self.settings, game),
            "image": _image_url(self.settings, game.id),
            "error": error,
        }

    def describe_dev(self, repo: DevRepo) -> dict:
        status, error = self._status(repo.id)
        return {
            "id": repo.id,
            "title": repo.title,
            "description": repo.description,
            "exec": repo.command,
            "status": status,
            "image": _image_url(self.settings, repo.id),
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

    async def start_dev(self, repo_id: str) -> dict:
        repo = self._dev(repo_id)
        async with self._lock:
            task = self._tasks.get(repo_id)
            if task is not None and not task.done():
                raise AlreadyStarting
            self._errors.pop(repo_id, None)
            self._phases[repo_id] = "starting"
            cancelled = threading.Event()
            self._cancel_flags[repo_id] = cancelled
            self._tasks[repo_id] = asyncio.create_task(self._launch_dev(repo, cancelled))
        return self.describe_dev(repo)

    async def stop_dev(self, repo_id: str) -> dict:
        repo = self._dev(repo_id)
        await self._cancel(repo_id)
        await asyncio.to_thread(self.docker.remove, container_name(repo_id))
        self._errors.pop(repo_id, None)
        self._phases.pop(repo_id, None)
        return self.describe_dev(repo)

    async def clean_dev(self, repo_id: str) -> dict:
        repo = self._dev(repo_id)
        await self._cancel(repo_id)
        await asyncio.to_thread(self.docker.remove, container_name(repo_id))
        try:
            spec = parse_dev_exec(repo.command).run
            await asyncio.to_thread(self.docker.remove_image, spec.image)
        except LaunchError as exc:
            self._errors[repo_id] = exc.public_message
            return self.describe_dev(repo)
        self._errors.pop(repo_id, None)
        self._phases.pop(repo_id, None)
        return self.describe_dev(repo)

    def _prepare_display(self) -> None:
        with self._display_lock:
            if self._display_ready:
                return
            allow_local_docker_display()
            self._display_ready = True

    async def _launch_dev(self, repo: DevRepo, cancelled: threading.Event) -> None:
        try:
            spec = parse_dev_exec(repo.command).run
            build, allow_display = parse_image_command(repo.image_command)
            if cancelled.is_set():
                return
            exists = await asyncio.to_thread(self.docker.image_exists, spec.image)
            if not exists:
                self._phases[repo.id] = "cloning"
                context = await asyncio.to_thread(self.git.sync, repo)
                if cancelled.is_set():
                    return
                self._phases[repo.id] = "building"
                await asyncio.to_thread(self.docker.build_dev, context, build, cancelled)
                if cancelled.is_set():
                    return
            if allow_display:
                await asyncio.to_thread(self._prepare_display)
            if cancelled.is_set():
                return
            self._phases[repo.id] = "starting"
            await asyncio.to_thread(self.docker.run_dev, repo, spec, cancelled)
            self._errors.pop(repo.id, None)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.docker.remove, container_name(repo.id))
            raise
        except LaunchError as exc:
            logger.warning("Launch failed for %s: %s", repo.id, exc.public_message)
            self._errors[repo.id] = exc.public_message
        except Exception:
            logger.exception("Launch failed for %s", repo.id)
            self._errors[repo.id] = "Launch failed"
        finally:
            self._phases.pop(repo.id, None)

    async def _cancel(self, item_id: str) -> None:
        async with self._lock:
            task = self._tasks.get(item_id)
            cancelled = self._cancel_flags.get(item_id)
        if cancelled is not None:
            cancelled.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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

    def _status(self, item_id: str) -> tuple[str, str | None]:
        phase = self._phases.get(item_id)
        if phase:
            return phase, None
        try:
            state = self.docker.inspect(container_name(item_id))
        except RuntimeUnavailable:
            logger.warning("Docker is unavailable while reading %s", item_id)
            return "error", "Container runtime is unavailable"
        if state is not None and state.running:
            return "running", None
        if state is not None:
            return "error", state.public_error
        stored = self._errors.get(item_id)
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

    def _dev(self, repo_id: str) -> DevRepo:
        try:
            matches = [repo for repo in self.catalog.dev_repos() if repo.id == repo_id]
        except CatalogError:
            raise
        if not matches:
            raise DevNotFound
        return matches[0]

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
