"""Runtime configuration for the control service."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_MEMORY = re.compile(r"^\d+(\.\d+)?([bkmgBKMG])?$")


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    catalog_path: Path
    schema_path: Path
    src_dir: Path
    games_domain: str
    admin_token: str
    docker_network: str
    public_scheme: str
    memory_limit: str
    cpu_limit: float
    pids_limit: int
    health_timeout_seconds: float
    git_ssh_key_path: str | None
    publish_host: str | None

    @property
    def nano_cpus(self) -> int:
        return int(self.cpu_limit * 1_000_000_000)


def load_settings() -> Settings:
    publish_host = os.environ.get("PUBLISH_HOST", "").strip() or None
    if publish_host is not None and publish_host not in {"127.0.0.1", "localhost"}:
        raise SettingsError("PUBLISH_HOST must be 127.0.0.1 or localhost")

    if publish_host is None:
        domain = _required("GAMES_DOMAIN")
    else:
        domain = os.environ.get("GAMES_DOMAIN", "").strip() or "localhost"
    if publish_host is None and _DOMAIN.fullmatch(domain) is None:
        raise SettingsError("GAMES_DOMAIN must be a DNS name")

    scheme = os.environ.get("PUBLIC_SCHEME", "http" if publish_host else "https").strip().lower()
    if scheme not in {"http", "https"}:
        raise SettingsError("PUBLIC_SCHEME must be http or https")

    memory = os.environ.get("MEMORY_LIMIT", "512m").strip()
    if _MEMORY.fullmatch(memory) is None:
        raise SettingsError("MEMORY_LIMIT must be a size such as 512m")

    try:
        cpu_limit = float(os.environ.get("CPU_LIMIT", "1"))
        pids_limit = int(os.environ.get("PIDS_LIMIT", "256"))
        health_timeout = float(os.environ.get("HEALTH_TIMEOUT_SECONDS", "60"))
    except ValueError as exc:
        raise SettingsError("CPU_LIMIT, PIDS_LIMIT, and HEALTH_TIMEOUT_SECONDS must be numbers") from exc

    if cpu_limit <= 0 or pids_limit <= 0 or health_timeout <= 0:
        raise SettingsError("Resource limits and the health timeout must be positive")

    network = os.environ.get("DOCKER_NETWORK", "playground").strip()
    if network == "host" or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", network) is None:
        raise SettingsError("DOCKER_NETWORK must be a user-defined Docker network name")

    key_path = os.environ.get("GIT_SSH_KEY_PATH", "").strip() or None

    return Settings(
        catalog_path=Path(os.environ.get("CATALOG_PATH", "/etc/playground/catalog.yaml")),
        schema_path=Path(os.environ.get("CATALOG_SCHEMA_PATH", "/app/catalog.schema.json")),
        src_dir=Path(os.environ.get("SRC_DIR", "/var/lib/playground/src")),
        games_domain=domain,
        admin_token=_required("ADMIN_TOKEN"),
        docker_network=network,
        public_scheme=scheme,
        memory_limit=memory,
        cpu_limit=cpu_limit,
        pids_limit=pids_limit,
        health_timeout_seconds=health_timeout,
        git_ssh_key_path=key_path,
        publish_host=publish_host,
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SettingsError(f"{name} is required")
    return value
