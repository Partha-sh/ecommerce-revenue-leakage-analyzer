"""Run a read-only SQLAlchemy connection check against the configured database."""

from __future__ import annotations

from src.database.connection import (
    DatabaseConfigurationError,
    create_database_engine,
    verify_database_connection,
)


def main() -> int:
    """Print the connected database and user, without exposing credentials."""
    engine = None
    try:
        engine = create_database_engine()
        database_name, user_name = verify_database_connection(engine)
    except (DatabaseConfigurationError, ConnectionError) as exc:
        print(f"Connection check failed: {exc}")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(f"Connection successful: database={database_name}, user={user_name}")
    print("No data was loaded or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
