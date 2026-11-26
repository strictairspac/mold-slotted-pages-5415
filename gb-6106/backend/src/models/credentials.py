from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)

from .base import metadata


# Current download/verification state for one completed batch.  The row is
# deliberately separate from ``runs``: a damaged credential must retain its
# original completion fingerprint even while its stored body is quarantined.
credential_verifications = Table(
    "credential_verifications", metadata,
    Column("run_id", Integer, ForeignKey("runs.id"), primary_key=True),
    Column("status", String(24), nullable=False, index=True),
    Column("download_allowed", Boolean, nullable=False, default=False),
    Column("expected_digest", String(64), nullable=True),
    Column("actual_digest", String(64), nullable=True),
    Column("replay_digest", String(64), nullable=True),
    Column("repair_id", Integer, nullable=True),
    Column("repair_count", Integer, nullable=False, server_default="0", default=0),
    Column("last_error", String(320), nullable=True),
    Column("checked_at", BigInteger, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("repaired_at", BigInteger, nullable=True),
)


# Immutable-ish ledger for damaged credentials.  The exact damaged export is
# kept in ``bad_credential``; replay never overwrites it.  The unique key is
# the final concurrency guard ensuring the same damaged body is replayed once.
credential_repairs = Table(
    "credential_repairs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, ForeignKey("runs.id"), nullable=False, index=True),
    Column("bad_digest", String(64), nullable=False),
    Column("expected_digest", String(64), nullable=True),
    Column("replay_digest", String(64), nullable=True),
    Column("bad_credential", JSON(none_as_null=True), nullable=False),
    Column("status", String(24), nullable=False, index=True),
    Column("actor", String(48), nullable=True),
    Column("idempotency_key", String(128), nullable=True),
    Column("claim_token", String(64), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default="0", default=0),
    Column("repair_count", Integer, nullable=False, server_default="0", default=0),
    Column("error", String(320), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("completed_at", BigInteger, nullable=True),
    UniqueConstraint("run_id", "bad_digest", name="uq_credential_repair_run_digest"),
)
