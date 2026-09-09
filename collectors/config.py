"""Environment configuration.

Hard rule 5: no secrets in the repo. Everything sensitive comes from the process
environment or a gitignored ``.env``. Nothing here has a credential as a default,
and nothing here writes a credential anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load ``KEY=value`` pairs from ``.env`` into ``os.environ``.

    Existing environment variables win, so a real environment always overrides the
    file. Returns the keys that were loaded, never the values -- printing this is
    safe.
    """
    env_path = path or REPO_ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = "<set>"
    return loaded


@dataclass(frozen=True)
class Config:
    bitquery_token: str | None
    bitquery_endpoint: str
    db_path: Path
    parquet_dir: Path
    poll_seconds: int

    @property
    def has_bitquery_credentials(self) -> bool:
        return bool(self.bitquery_token)

    def require_bitquery(self) -> str:
        if not self.bitquery_token:
            raise RuntimeError(
                "BITQUERY_TOKEN is not set. Copy .env.example to .env and fill it in, "
                "or run with --replay to use recorded fixtures instead of the network."
            )
        return self.bitquery_token


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_config() -> Config:
    load_dotenv()
    return Config(
        bitquery_token=os.environ.get("BITQUERY_TOKEN") or None,
        bitquery_endpoint=os.environ.get(
            "BITQUERY_ENDPOINT", "https://streaming.bitquery.io/eap"
        ),
        db_path=_resolve(os.environ.get("SCREENER_DB_PATH", "data/screener.duckdb")),
        parquet_dir=_resolve(os.environ.get("SCREENER_PARQUET_DIR", "data/parquet")),
        poll_seconds=int(os.environ.get("WATCHER_POLL_SECONDS", "60")),
    )
