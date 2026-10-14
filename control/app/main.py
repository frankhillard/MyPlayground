"""Production entrypoint. Uvicorn loads ``app`` from this module."""

from __future__ import annotations

import logging

from app.api import create_app
from app.catalog import Catalog
from app.launcher import LaunchManager
from app.settings import load_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

_settings = load_settings()
_catalog = Catalog(_settings.catalog_path, _settings.schema_path)
_catalog.games()
app = create_app(LaunchManager(_settings, _catalog))
