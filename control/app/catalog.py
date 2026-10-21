"""Load and validate the game catalog."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import yaml

logger = logging.getLogger(__name__)

GAME_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class CatalogError(Exception):
    """The catalog file is missing or does not match the schema."""


@dataclass(frozen=True)
class Game:
    id: str
    title: str
    description: str
    git: str
    ref: str
    dockerfile: str
    container_port: int
    health_path: str
    host_port: int | None = None


@dataclass(frozen=True)
class DevRepo:
    id: str
    title: str
    description: str
    git: str
    ref: str
    image_command: str
    command: str


class Catalog:
    def __init__(self, path: Path, schema_path: Path) -> None:
        self.path = path
        self.schema_path = schema_path
        self._mtime_ns: int | None = None
        self._games: dict[str, Game] = {}
        self._dev: dict[str, DevRepo] = {}

    def get(self, game_id: str) -> Game | None:
        self._reload()
        return self._games.get(game_id)

    def games(self) -> list[Game]:
        self._reload()
        return list(self._games.values())

    def dev_repos(self) -> list[DevRepo]:
        self._reload()
        return list(self._dev.values())

    def has(self, item_id: str) -> bool:
        self._reload()
        return item_id in self._games or item_id in self._dev

    def _reload(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError as exc:
            raise CatalogError("Catalog is unavailable") from exc
        if mtime_ns == self._mtime_ns:
            return
        try:
            document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
            jsonschema.validate(document, schema)
        except jsonschema.ValidationError as exc:
            logger.error("Catalog schema violation: %s", exc.message)
            raise CatalogError("Catalog is unavailable") from exc
        except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
            logger.error("Catalog could not be read: %s", exc)
            raise CatalogError("Catalog is unavailable") from exc

        seen: set[str] = set()
        games: dict[str, Game] = {}
        host_ports: set[int] = set()
        for item in document["games"]:
            game_id = _catalog_id(item["id"], seen)
            host_port = item.get("hostPort")
            if host_port is not None:
                if host_port in host_ports:
                    logger.error("Duplicate host port %s", host_port)
                    raise CatalogError("Catalog is unavailable")
                host_ports.add(host_port)
            games[game_id] = Game(
                id=game_id,
                title=item["title"],
                description=item["description"],
                git=item["git"],
                ref=item["ref"],
                dockerfile=item["dockerfile"],
                container_port=item["containerPort"],
                health_path=item.get("healthPath", "/"),
                host_port=host_port,
            )
        dev: dict[str, DevRepo] = {}
        for item in document.get("dev") or []:
            repo_id = _catalog_id(item["id"], seen)
            dev[repo_id] = DevRepo(
                id=repo_id,
                title=item["title"],
                description=item["description"],
                git=item["git"],
                ref=item["ref"],
                image_command=item["image"],
                command=item["exec"],
            )
        self._games = games
        self._dev = dev
        self._mtime_ns = mtime_ns


def _catalog_id(raw: str, seen: set[str]) -> str:
    item_id = raw.lower()
    if GAME_ID_RE.fullmatch(item_id) is None:
        logger.error("Catalog id %s is not a DNS label", raw)
        raise CatalogError("Catalog is unavailable")
    if item_id in seen:
        logger.error("Duplicate catalog id %s", item_id)
        raise CatalogError("Catalog is unavailable")
    seen.add(item_id)
    return item_id
