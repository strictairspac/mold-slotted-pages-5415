"""Dialect-aware insert helpers.

The application runs on MySQL in compose and on SQLite in lightweight checks.
These helpers keep idempotent writes portable without branching in services.
"""
from sqlalchemy import insert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def insert_once(db, table, values, key):
    """Insert a row and absorb a conflicting primary/unique key.

    Returns True only when this call inserted the row.
    """
    dialect = db.dialect.name
    if dialect == "mysql":
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(
            **{key: table.c[key]}
        )
    elif dialect == "postgresql":
        statement = pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    elif dialect == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    else:
        statement = insert(table).values(**values)
    result = db.execute(statement)
    return getattr(result, "rowcount", 0) == 1
