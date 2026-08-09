from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MIGRATION_PATTERN = re.compile(r"^(?P<version>\d+)_.*\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path


class Connection(Protocol):
    def execute(self, sql: str, params: tuple | None = None): ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


def discover_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    seen: set[int] = set()
    for path in directory.glob("*.sql"):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            continue
        version = int(match.group("version"))
        if version in seen:
            raise MigrationError(f"doppelte Migrationsnummer {version}")
        seen.add(version)
        migrations.append(Migration(version=version, path=path))
    return sorted(migrations, key=lambda migration: migration.version)


def apply_migrations(connection: Connection, directory: Path) -> list[int]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    connection.commit()
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    applied_versions = {row[0] for row in rows}
    applied_now: list[int] = []
    for migration in discover_migrations(directory):
        if migration.version in applied_versions:
            continue
        try:
            connection.execute(migration.path.read_text())
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (%s)",
                (migration.version,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        applied_now.append(migration.version)
    return applied_now


def run_migrations(database_url: str, directory: Path | None = None) -> list[int]:
    import psycopg

    migrations_dir = directory or Path(__file__).resolve().parent.parent / "migrations"
    with psycopg.connect(database_url) as connection:
        return apply_migrations(connection, migrations_dir)


def current_schema_version(database_url: str) -> int:
    import psycopg

    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(row[0])
