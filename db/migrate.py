"""Apply numbered SQL migrations in order. Run with `python -m db.migrate`.

Each file in `db/migrations/` is named `NNN_description.sql`. Applied versions are
recorded in `schema_migrations`, so re-running only applies what is new.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from db.connection import Connection, connect

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(\d+)_.+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path


def discover(directory: Path) -> list[Migration]:
    """Return the migrations in `directory`, ordered by version."""
    migrations: dict[int, Migration] = {}
    for path in directory.glob("*.sql"):
        match = _FILENAME.match(path.name)
        if match is None:
            raise ValueError(f"migration file name must look like 001_name.sql: {path.name}")
        version = int(match.group(1))
        if version in migrations:
            raise ValueError(
                f"duplicate migration version {version}: "
                f"{migrations[version].path.name} and {path.name}"
            )
        migrations[version] = Migration(version, path)
    return [migrations[version] for version in sorted(migrations)]


def pending(migrations: list[Migration], applied: set[int]) -> list[Migration]:
    return [m for m in migrations if m.version not in applied]


def applied_versions(conn: Connection) -> set[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def apply(conn: Connection, migration: Migration) -> None:
    """Run one migration and record it, atomically."""
    with conn.transaction():
        conn.execute(migration.path.read_bytes())
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (migration.version, migration.path.name),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with connect() as conn:
        todo = pending(discover(MIGRATIONS_DIR), applied_versions(conn))
        for migration in todo:
            apply(conn, migration)
            logger.info("applied migration %s", migration.path.name)
        logger.info("migrations up to date (%d applied this run)", len(todo))


if __name__ == "__main__":
    main()
