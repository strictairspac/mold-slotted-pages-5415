"""Compatibility namespace for verification/repair acceptance scripts."""
from fastapi import HTTPException
from sqlalchemy import select

from src.models.runs import runs
from src.services.credentials import *  # noqa: F401,F403
from src.services.credentials import (  # noqa: F401
    evaluate,
    persist_verification,
    prepare_export,
    recover_interrupted,
    repair_run,
    verify_run,
)


def verify(db_or_run_id, run_id=None, actor=None):
    """Call ``verify_run`` by id or persist a check using an existing session.

    Supported forms:
    - ``verify(run_id)``
    - ``verify(db, run_id)``
    - ``verify(db, run_dict)``
    """
    if run_id is None:
        return verify_run(db_or_run_id, actor)
    db = db_or_run_id
    target = run_id
    if isinstance(target, dict):
        return persist_verification(db, target, actor)
    run = db.execute(select(runs).where(runs.c.id == target)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    return persist_verification(db, dict(run), actor)


def repair(db_or_run_id, run_id=None, actor=None, idempotency_key=None):
    """Convenience alias for :func:`repair_run` and db-session-style calls."""
    if run_id is None:
        return repair_run(db_or_run_id, actor=actor, idempotency_key=idempotency_key)
    # The durable implementation owns its transaction/locks; callers passing
    # a session still receive the same serialised service behaviour.
    return repair_run(run_id, actor=actor, idempotency_key=idempotency_key)
