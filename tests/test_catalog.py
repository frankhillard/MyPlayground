from pathlib import Path

import pytest

from app.catalog import Catalog, CatalogError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "catalog.schema.json"


def test_shipped_catalog_matches_schema():
    catalog = Catalog(ROOT / "catalog.yaml", SCHEMA)
    games = catalog.games()
    assert games
    assert [game.id for game in games] == [game.id.lower() for game in games]
    assert all(game.container_port > 0 for game in games)
    assert all(game.git.startswith(("https://", "git@", "ssh://")) for game in games)
    dev = catalog.dev_repos()
    assert [repo.id for repo in dev] == ["ball-tracker"]
    assert "docker build -t newton-live ." in dev[0].image_command
    assert dev[0].image_command.rstrip().endswith("xhost +local:docker")
    assert dev[0].command.startswith("docker run --rm -it")
    assert "newton-live python live.py --max-poly-degree 2" in dev[0].command


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


def test_id_shared_by_a_game_and_a_dev_repo_is_rejected(tmp_path: Path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
games:
  - id: example
    title: Example
    description: A game
    git: https://github.com/example/game.git
    ref: main
    dockerfile: Dockerfile
    containerPort: 8080
dev:
  - id: Example
    title: Example
    description: Same id
    git: https://github.com/example/tool.git
    ref: main
    image: docker build -t tool .
    exec: python live.py
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError):
        Catalog(catalog, SCHEMA).dev_repos()


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
