from time import time

from fastapi import HTTPException
from sqlalchemy import insert, select

from src.constants.job_state import JobState
from src.domain.scenario import CONFIG
from src.models.cases import cases
from src.models.credentials import credential_verifications
from src.models.runs import runs
from . import audit, cases as case_service


def query():
    return select(runs, cases.c.title.label("case_title")).join(cases, cases.c.id == runs.c.case_id)


def _with_verification(entry, verification=None):
    if entry is None:
        return None
    item = dict(entry)
    row = dict(verification) if verification is not None else None
    if row is None:
        done = item.get("status") == JobState.DONE
        item.setdefault("verification_status", None)
        item.setdefault("credential_status", None)
        item.setdefault("integrity_status", None)
        item.setdefault("quarantine_status", "unknown" if done else None)
        item.setdefault("delivery_status", "unknown" if done else None)
        item.setdefault("download_allowed", None)
        item.setdefault("blocked", False if done else None)
        item.setdefault("quarantined", False if done else None)
        item.setdefault("verified", None)
        item.setdefault("repair_count", 0 if done else None)
        return item
    valid = row.get("status") == "valid"
    held = row.get("status") == "invalid" or not bool(row.get("download_allowed"))
    verification_status = row.get("status")
    item.update({
        "verification_status": verification_status,
        "credential_status": "valid" if valid else "bad",
        "integrity_status": "verified" if valid else "mismatch",
        "quarantine_status": "released" if valid else "quarantined",
        "delivery_status": "released" if valid else "held",
        "download_allowed": bool(row.get("download_allowed")),
        "blocked": held,
        "quarantined": held,
        "verified": valid,
        "repair_count": row.get("repair_count") or 0,
    })
    return item


def get(db, run_id):
    run = db.execute(query().where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    verification = db.execute(
        select(credential_verifications).where(credential_verifications.c.run_id == run_id)
    ).mappings().first()
    return _with_verification(run, verification)


def list_entries(db):
    rows = [dict(row) for row in db.execute(query().order_by(runs.c.id.desc()).limit(100)).mappings()]
    if not rows:
        return rows
    ids = [row["id"] for row in rows]
    verifications = {
        row["run_id"]: row
        for row in db.execute(
            select(credential_verifications).where(credential_verifications.c.run_id.in_(ids))
        ).mappings()
    }
    return [_with_verification(row, verifications.get(row["id"])) for row in rows]


def enqueue(db, case_id, parameter, actor):
    if parameter > CONFIG["max_parameter"]:
        raise HTTPException(422, f"参数不能超过 {CONFIG['max_parameter']}")
    case_service.get(db, case_id)
    change = db.execute(insert(runs).values(
        case_id=case_id, status=JobState.QUEUED, parameter=parameter, actor=actor, created_at=int(time())
    ))
    run_id = change.inserted_primary_key[0]
    audit.record(db, "enqueue", actor, "run", run_id)
    return get(db, run_id)


def verify_credential(run_id, actor="system"):
    # Local import avoids a service-module import cycle.
    from src.services.credentials import verify_run
    return verify_run(run_id, actor)


def repair_credential(run_id, actor="system", idempotency_key=None):
    from src.services.credentials import repair_run
    return repair_run(run_id, actor, idempotency_key)
