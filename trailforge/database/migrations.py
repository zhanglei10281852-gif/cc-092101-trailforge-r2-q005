from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect

from trailforge.database.session import Database
from trailforge.models.audit import SchemaMigration


@dataclass(frozen=True)
class Migration:
    version: str
    description: str


MIGRATIONS = [
    Migration(version="0001", description="Initial TrailForge schema"),
    Migration(version="0002", description="Append-only incident timeline guards"),
]

# Incident timelines are legal records of the night shift: nobody may rewrite or
# remove history, so the database itself rejects UPDATE and DELETE on these tables.
APPEND_ONLY_TABLES = ("incident_timeline_entries", "handover_confirmations")


def initialize_database(database: Database) -> list[str]:
    database.create_schema()
    applied: list[str] = []
    with database.session() as session:
        known = {
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        }
        for migration in MIGRATIONS:
            if migration.version in known:
                continue
            session.add(
                SchemaMigration(
                    version=migration.version,
                    description=migration.description,
                )
            )
            applied.append(migration.version)
    _install_append_only_guards(database)
    return applied


def _install_append_only_guards(database: Database) -> None:
    with database.engine.begin() as connection:
        for table in APPEND_ONLY_TABLES:
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_reject_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )
            connection.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_reject_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )


def migration_status(database: Database) -> dict[str, object]:
    inspector = inspect(database.engine)
    if "schema_migrations" not in inspector.get_table_names():
        return {
            "initialized": False,
            "applied": [],
            "pending": [item.version for item in MIGRATIONS],
        }
    with database.session() as session:
        applied = [
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        ]
    pending = [item.version for item in MIGRATIONS if item.version not in set(applied)]
    return {"initialized": True, "applied": applied, "pending": pending}


def assert_database_integrity(database: Database) -> dict[str, object]:
    with database.engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        foreign_key_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "healthy": integrity == "ok" and not foreign_key_rows,
    }
