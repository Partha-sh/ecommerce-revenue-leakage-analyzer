"""Build an explicit PostgreSQL SQLAlchemy engine from environment settings."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DatabaseConfigurationError(ValueError):
    """Raised when required PostgreSQL connection settings are incomplete."""


def database_url_from_env(environ: Mapping[str, str] | None = None) -> URL:
    """Create a properly escaped SQLAlchemy URL; never log the password."""
    if environ is None:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        settings: Mapping[str, str] = os.environ
    else:
        settings = environ
    required = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise DatabaseConfigurationError(
            f"Missing PostgreSQL environment settings: {', '.join(missing)}. "
            "Copy .env.example to .env and configure it after PostgreSQL setup."
        )
    try:
        port = int(settings["DB_PORT"])
    except ValueError as exc:
        raise DatabaseConfigurationError("DB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise DatabaseConfigurationError("DB_PORT must be between 1 and 65535")
    return URL.create(
        "postgresql+psycopg2",
        username=settings["DB_USER"],
        password=settings["DB_PASSWORD"],
        host=settings["DB_HOST"],
        port=port,
        database=settings["DB_NAME"],
    )


def create_database_engine(environ: Mapping[str, str] | None = None) -> Engine:
    """Construct an engine without connecting to the server immediately."""
    url = database_url_from_env(environ)
    return create_engine(url, pool_pre_ping=True, future=True)


def verify_database_connection(engine: Engine) -> tuple[str, str]:
    """Explicitly check a configured server and return database/user; no writes."""
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT current_database(), current_user")).one()
    except Exception as exc:
        LOGGER.exception("PostgreSQL connection check failed")
        raise ConnectionError(
            "PostgreSQL connection failed. Check the server, port, database, username, and local .env password."
        ) from exc
    database_name, user_name = str(row[0]), str(row[1])
    LOGGER.info("PostgreSQL connection check succeeded for database %s as %s", database_name, user_name)
    return database_name, user_name
