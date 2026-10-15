"""HTTP API and dashboard for the game launcher."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from app.catalog import GAME_ID_RE, CatalogError
from app.launcher import AlreadyStarting, GameNotFound, LaunchManager, picture_file

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"
_TITLE_FONT = Path(__file__).resolve().parent / "static" / "fonts" / "Caveat.ttf"


def create_app(manager: LaunchManager) -> FastAPI:
    app = FastAPI(title="Playground")

    @app.get("/")
    def dashboard() -> FileResponse:
        return FileResponse(_STATIC, headers={"Cache-Control": "no-store"})

    @app.get("/fonts/Caveat.ttf")
    def title_font() -> FileResponse:
        return FileResponse(
            _TITLE_FONT,
            media_type="font/ttf",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/games/{game_id}/image")
    def game_image(game_id: str) -> FileResponse:
        game_id = game_id.lower()
        if GAME_ID_RE.fullmatch(game_id) is None:
            raise HTTPException(status_code=404, detail="Unknown game")
        try:
            known = manager.catalog.get(game_id) is not None
        except CatalogError as exc:
            raise HTTPException(status_code=500, detail="Catalog is unavailable") from exc
        path = picture_file(manager.settings, game_id) if known else None
        if path is None:
            raise HTTPException(status_code=404, detail="Unknown game")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/api/games")
    def list_games() -> dict:
        try:
            return {"games": manager.list_games()}
        except CatalogError as exc:
            raise HTTPException(status_code=500, detail="Catalog is unavailable") from exc

    @app.post("/api/games/{game_id}/start", status_code=202)
    async def start_game(
        game_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require_admin(manager, authorization)
        return await _act(manager.start, game_id)

    @app.post("/api/games/{game_id}/stop")
    async def stop_game(
        game_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        _require_admin(manager, authorization)
        return await _act(manager.stop, game_id)

    return app


async def _act(action, game_id: str) -> dict:
    game_id = game_id.lower()
    if GAME_ID_RE.fullmatch(game_id) is None:
        raise HTTPException(status_code=404, detail="Unknown game")
    try:
        return await action(game_id)
    except GameNotFound as exc:
        raise HTTPException(status_code=404, detail="Unknown game") from exc
    except AlreadyStarting as exc:
        raise HTTPException(status_code=409, detail="Game is already starting") from exc
    except CatalogError as exc:
        raise HTTPException(status_code=500, detail="Catalog is unavailable") from exc


def _require_admin(manager: LaunchManager, authorization: str | None) -> None:
    expected = manager.settings.admin_token
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Admin token required")
