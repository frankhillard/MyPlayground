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


class Catalog:
    def __init__(self, path: Path, schema_path: Path) -> None:
        self.path = path
        self.schema_path = schema_path
        self._mtime_ns: int | None = None
        self._games: dict[str, Game] = {}

    def get(self, game_id: str) -> Game | None:
        self._reload()
        return self._games.get(game_id)

    def games(self) -> list[Game]:
        self._reload()
        return list(self._games.values())

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

        games: dict[str, Game] = {}
        host_ports: set[int] = set()
        for item in document["games"]:
            game_id = item["id"].lower()
            if GAME_ID_RE.fullmatch(game_id) is None:
                logger.error("Game id %s is not a DNS label", item["id"])
                raise CatalogError("Catalog is unavailable")
            if game_id in games:
                logger.error("Duplicate game id %s", game_id)
                raise CatalogError("Catalog is unavailable")
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
        self._games = games
        self._mtime_ns = mtime_ns
