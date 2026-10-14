from pathlib import Path

import pytest

from app.catalog import Catalog, CatalogError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "catalog.schema.json"


def test_shipped_catalog_matches_schema():
    games = Catalog(ROOT / "catalog.yaml", SCHEMA).games()
    assert games
    assert [game.id for game in games] == [game.id.lower() for game in games]
    assert all(game.container_port > 0 for game in games)
    assert all(game.git.startswith(("https://", "git@", "ssh://")) for game in games)


def test_mixed_case_id_is_folded(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
games:
  - id: Cordon
    title: Cordon
    description: Jezzball-like game.
    git: https://github.com/frankhillard/jezzball.git
    ref: main
    dockerfile: Dockerfile
    containerPort: 8080
""",
        encoding="utf-8",
    )
    games = Catalog(catalog, SCHEMA).games()
    assert games[0].id == "cordon"
    assert games[0].title == "Cordon"


def test_duplicate_id_is_rejected(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
games:
  - id: example
    title: One
    description: First
    git: https://github.com/example/game.git
    ref: main
    dockerfile: Dockerfile
    containerPort: 8080
  - id: example
    title: Two
    description: Second
    git: https://github.com/example/other.git
    ref: main
    dockerfile: Dockerfile
    containerPort: 8080
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        Catalog(catalog, SCHEMA).games()


def test_dockerfile_escape_is_rejected(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
games:
  - id: example
    title: Example
    description: Escapes the repository
    git: https://github.com/example/game.git
    ref: main
    dockerfile: ../Dockerfile
    containerPort: 8080
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        Catalog(catalog, SCHEMA).games()
