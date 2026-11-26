"""Verification, quarantine and deterministic repair of completion credentials.

A completion fingerprint is the SHA-256 digest stored on ``runs.digest`` when
the worker finishes a batch.  Download calls :func:`prepare_export`, which
re-hashes the stored result before emitting bytes.  A mismatch creates (or
updates) a durable ``invalid`` verification row and retains the exact bad
credential in ``credential_repairs``; the batch then cannot download.

Repair takes the run row lock, replays the original case payload with the
batch's original page capacity and only restores the result when the replay
digest equals the completion fingerprint.  The run lock plus the
``(run_id, bad_digest)`` unique key make concurrent repairs collapse onto one
ledger row and one successful replay.
"""
from hmac import compare_digest
from time import sleep, time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from src.constants.job_state import JobState
from src.domain import scenario
from src.models.cases import cases
from src.models.credentials import credential_repairs, credential_verifications
from src.models.runs import runs
from src.services import audit, reviews
from src.utils.serial import content_digest


VALID = "valid"
INVALID = "invalid"
REPAIRED = "repaired"
DETAINED = "detained"
PROCESSING = "processing"
INTERRUPTED = "interrupted"
FAILED = "failed"
FINGERPRINT_MISMATCH = "fingerprint_mismatch"

ACTIVE_BAD_STATUSES = {DETAINED, PROCESSING, INTERRUPTED, FAILED, FINGERPRINT_MISMATCH}


def now():
    return int(time())


def _digest(value):
    return content_digest(value)


def _normalize_fingerprint(value):
    if not isinstance(value, str):
        return value
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    return text if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text) else value


def evaluate(run):
    """Compare a stored result with its trusted completion fingerprint."""
    expected = run.get("digest") if isinstance(run, dict) else run["digest"]
    expected = _normalize_fingerprint(expected)
    result = run.get("result") if isinstance(run, dict) else run["result"]
    actual = _digest(result)
    matches = bool(expected) and isinstance(expected, str) and compare_digest(expected, actual)
    return {
        "applicable": True,
        "valid": matches,
        "expected_digest": expected,
        "actual_digest": actual,
        "status": VALID if matches else INVALID,
        "download_allowed": matches,
        "blocked": not matches,
    }


# Friendly alias for callers/tests that phrase the operation as a check.
check_credential = evaluate


def verify_credential(*args, **kwargs):
    """Polymorphic verification helper.

    Accepts the pure ``verify_credential(run_mapping)`` form as well as the
    service forms ``verify_credential(run_id)`` and
    ``verify_credential(db, run_id_or_mapping)``.
    """
    if args and isinstance(args[0], int):
        actor = kwargs.get("actor")
        if len(args) > 1 and isinstance(args[1], str):
            actor = args[1]
        return verify_run(args[0], actor)
    if len(args) == 1:
        return evaluate(args[0])
    if len(args) >= 2:
        db, target = args[0], args[1]
        actor = kwargs.get("actor") or (args[2] if len(args) > 2 and isinstance(args[2], str) else None)
        if isinstance(target, dict):
            return persist_verification(db, target, actor)
        run = db.execute(select(runs).where(runs.c.id == target)).mappings().first()
        if run is None:
            raise HTTPException(404, "执行批次不存在")
        return persist_verification(db, dict(run), actor)
    if "run_id" in kwargs:
        return verify_run(kwargs["run_id"], kwargs.get("actor"))
    return evaluate(kwargs.get("run") or kwargs.get("credential") or {})


def _verification_dict(row):
    if row is None:
        return None
    item = dict(row)
    status = item.get("status")
    valid = status == VALID
    held = status == INVALID or not bool(item.get("download_allowed"))
    item.update({
        "valid": valid,
        "ok": valid,
        "passed": valid,
        "verified": status in {VALID, REPAIRED},
        "repaired": bool(item.get("repaired_at")) or status == REPAIRED,
        "download_allowed": bool(item.get("download_allowed")),
        "blocked": held,
        "quarantined": held,
        "held": held,
        "stopped": held,
        "delivery": "released" if item.get("download_allowed") else "held",
        "delivery_status": "released" if item.get("download_allowed") else "held",
        "state": "verified" if valid else ("quarantined" if held else status),
        "verification_status": "passed" if valid else "failed",
    })
    return item


def _repair_dict(row, *, duplicate=False, replayed=None):
    if row is None:
        return None
    item = dict(row)
    status = item.get("status")
    repaired = status == REPAIRED
    item.update({
        "id": item.get("id"),
        "repair_id": item.get("id"),
        "credential_id": item.get("id"),
        "valid": repaired,
        "ok": repaired,
        "passed": repaired,
        "repaired": repaired,
        "success": repaired,
        "succeeded": repaired,
        "download_allowed": repaired,
        "blocked": not repaired,
        "quarantined": not repaired,
        "duplicate": duplicate,
        "idempotent_replay": duplicate,
        "replayed": repaired if replayed is None else replayed,
        "repair_count": 1 if repaired else int(item.get("repair_count") or 0),
        "state": "succeeded" if repaired else str(status or "unknown"),
        "outcome": "repaired" if repaired else str(status or "unknown"),
    })
    return item


def get_verification(db, run_id):
    row = db.execute(
        select(credential_verifications).where(credential_verifications.c.run_id == run_id)
    ).mappings().first()
    item = _verification_dict(row)
    if item and not item["valid"]:
        repair = latest_repair(db, run_id)
        item["repair"] = repair
        if repair:
            item["repair_id"] = repair.get("id")
            item["bad_credential"] = repair.get("bad_credential")
    return item


def list_verifications(db):
    rows = db.execute(
        select(credential_verifications).order_by(credential_verifications.c.run_id.desc()).limit(200)
    ).mappings()
    return [_verification_dict(row) for row in rows]


def get_repair(db, repair_id):
    row = db.execute(
        select(credential_repairs).where(credential_repairs.c.id == repair_id)
    ).mappings().first()
    return _repair_dict(row)


def latest_repair(db, run_id):
    row = db.execute(
        select(credential_repairs)
        .where(credential_repairs.c.run_id == run_id)
        .order_by(credential_repairs.c.id.desc())
        .limit(1)
    ).mappings().first()
    return _repair_dict(row)


def list_repairs(db, run_id=None):
    statement = select(credential_repairs).order_by(credential_repairs.c.id.desc()).limit(200)
    if run_id is not None:
        statement = statement.where(credential_repairs.c.run_id == run_id)
    return [_repair_dict(row) for row in db.execute(statement).mappings()]


def reconcile_valid_repair(db, run_id, digest_value):
    """Mark a ledger row repaired when the stored credential now verifies."""
    repair = latest_repair(db, run_id)
    if repair is None or repair.get("status") == REPAIRED:
        return repair
    timestamp = now()
    db.execute(
        update(credential_repairs)
        .where(credential_repairs.c.id == repair["id"])
        .values(
            status=REPAIRED,
            replay_digest=digest_value,
            error=None,
            updated_at=timestamp,
            completed_at=timestamp,
        )
    )
    return get_repair(db, repair["id"])


def _credential_snapshot(db, run):
    # Keep the same envelope as /runs/{id}/export so the retained bad
    # credential is directly usable as evidence of the failed inspection.
    credential = dict(run)
    credential["review"] = reviews.get(db, run["id"])
    credential.setdefault("verification", None)
    return credential


def retain_bad_credential(db, run, evaluation, actor=None):
    bad_digest = evaluation["actual_digest"]
    existing = db.execute(
        select(credential_repairs)
        .where(
            credential_repairs.c.run_id == run["id"],
            credential_repairs.c.bad_digest == bad_digest,
        )
        .with_for_update()
    ).mappings().first()
    if existing is not None:
        return dict(existing), False

    timestamp = now()
    values = {
        "run_id": run["id"],
        "bad_digest": bad_digest,
        "expected_digest": evaluation.get("expected_digest"),
        "replay_digest": None,
        "bad_credential": _credential_snapshot(db, run),
        "status": DETAINED,
        "actor": actor,
        "attempt_count": 0,
        "repair_count": 0,
        "error": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
    }
    change = db.execute(credential_repairs.insert().values(**values))
    repair_id = change.inserted_primary_key[0]
    audit.record(db, "credential_detained", actor or "system", "run", run["id"])
    row = db.execute(
        select(credential_repairs).where(credential_repairs.c.id == repair_id)
    ).mappings().first()
    return dict(row), True


def persist_verification(db, run, actor=None):
    """Evaluate and durably persist the current state.

    The caller owns the transaction and normally holds the run row lock.
    """
    evaluation = evaluate(run)
    timestamp = now()
    existing = db.execute(
        select(credential_verifications)
        .where(credential_verifications.c.run_id == run["id"])
        .with_for_update()
    ).mappings().first()

    values = {
        "status": evaluation["status"],
        "download_allowed": evaluation["valid"],
        "expected_digest": evaluation.get("expected_digest"),
        "actual_digest": evaluation.get("actual_digest"),
        "checked_at": timestamp,
        "last_error": None if evaluation["valid"] else "凭据指纹与完工指纹不一致",
    }
    if existing is None:
        values.update({
            "run_id": run["id"],
            "repair_count": 0,
            "created_at": timestamp,
            "repaired_at": None,
        })
        db.execute(credential_verifications.insert().values(**values))
    else:
        db.execute(
            update(credential_verifications)
            .where(credential_verifications.c.run_id == run["id"])
            .values(**values)
        )

    if not evaluation["valid"]:
        retain_bad_credential(db, run, evaluation, actor)

    return get_verification(db, run["id"])


def mark_completed(db, run_id, digest_value, result):
    """Create a valid verification row when the worker finishes a batch."""
    timestamp = now()
    actual = _digest(result)
    digest_value = _normalize_fingerprint(digest_value)
    valid = bool(digest_value) and compare_digest(digest_value, actual)
    existing = db.execute(
        select(credential_verifications).where(credential_verifications.c.run_id == run_id)
    ).first()
    values = {
        "status": VALID if valid else INVALID,
        "download_allowed": valid,
        "expected_digest": digest_value,
        "actual_digest": actual,
        "replay_digest": actual if valid else None,
        "checked_at": timestamp,
        "last_error": None if valid else "完工指纹与结果摘要不一致",
    }
    if existing is None:
        db.execute(credential_verifications.insert().values(
            run_id=run_id, repair_count=0, created_at=timestamp, repaired_at=None, **values
        ))
    else:
        db.execute(
            update(credential_verifications)
            .where(credential_verifications.c.run_id == run_id)
            .values(**values)
        )


def _locked_run(db, run_id):
    return db.execute(
        select(runs).where(runs.c.id == run_id).with_for_update()
    ).mappings().first()


def verify_run(run_id, actor=None):
    from src.config.resources import engine

    with engine.begin() as db:
        run = _locked_run(db, run_id)
        if run is None:
            raise HTTPException(404, "执行批次不存在")
        if run["status"] != JobState.DONE:
            raise HTTPException(409, "只有已完成批次需要验单")
        verification = persist_verification(db, dict(run), actor)
        if not verification["valid"]:
            repair = latest_repair(db, run_id)
            verification["repair"] = repair
            if repair:
                verification["bad_credential"] = repair.get("bad_credential")
                verification["repair_id"] = repair.get("id")
        audit.record(db, "verify_credential", actor or "system", "run", run_id)
        return verification


def prepare_export(run_id, actor=None):
    """Return the export credential or a blocked verification report."""
    from src.config.resources import engine

    with engine.begin() as db:
        run = _locked_run(db, run_id)
        if run is None:
            raise HTTPException(404, "执行批次不存在")
        if run["status"] != JobState.DONE:
            raise HTTPException(409, "没有可导出的执行结果")
        run = dict(run)
        verification = persist_verification(db, run, actor)
        if not verification["valid"]:
            repair = latest_repair(db, run_id)
            verification["repair"] = repair
            verification["detail"] = "凭据验单未通过，该批次已停发；请用原变更单和原页容量修复后再下载"
            verification["message"] = verification["detail"]
            return {"allowed": False, "verification": verification}
        run["review"] = reviews.get(db, run_id)
        # Deliberately do not inject verification metadata into the credential:
        # export bytes must remain the same completion envelope as before.
        return {"allowed": True, "credential": run, "verification": verification}


def _replay_original(db, run):
    case = db.execute(select(cases).where(cases.c.id == run["case_id"])).mappings().first()
    if case is None:
        raise ValueError("原变更单不存在，无法重放")
    # scenario.run accepts the database handle for historical signature
    # compatibility but the replay itself is deterministic and page-local.
    return scenario.run(case["payload"], run["parameter"], db)


def prepare_repair_state(run_id, actor=None, idempotency_key=None):
    """Durably record a bad credential before replay starts.

    This short transaction is intentionally separate from the replay
    transaction.  If the process is interrupted during replay, the invalid
    verification and retained bad credential are already committed, so the
    batch remains held after restart.
    """
    from src.config.resources import engine

    with engine.begin() as db:
        run = _locked_run(db, run_id)
        if run is None:
            raise HTTPException(404, "执行批次不存在")
        if run["status"] != JobState.DONE:
            raise HTTPException(409, "只有验坏的已完成批次可以修复")
        run = dict(run)
        evaluation = evaluate(run)
        if evaluation["valid"]:
            verification = persist_verification(db, run, actor)
            repair = reconcile_valid_repair(db, run_id, evaluation["actual_digest"])
            body = {
                "http_status": 200,
                "detail": "凭据验单通过，无需修复",
                "message": "凭据验单通过，无需修复",
                "run_id": run_id,
                "verification": verification,
                "repair": repair,
                "already_valid": True,
                "duplicate": repair is not None,
                "idempotent_replay": True,
                "replayed": False,
                "repaired": True,
                "valid": True,
                "status": REPAIRED if repair else VALID,
                "download_allowed": True,
                "blocked": False,
            }
            if repair:
                body.update({key: repair.get(key) for key in ("id", "repair_id", "credential_id", "repair_count")})
            return {
                "already_valid": True,
                "repair_id": repair["id"] if repair else None,
                "body": body,
            }
        verification = persist_verification(db, run, actor)
        incident, _created = retain_bad_credential(db, run, evaluation, actor)

        # Atomically claim a non-in-flight damaged credential.  The
        # conditional update is the dialect-independent winner election;
        # MySQL's run row lock additionally serialises the replay transaction.
        claim_token = uuid4().hex
        claim_values = {
            "status": PROCESSING,
            "actor": actor,
            "claim_token": claim_token,
            "attempt_count": credential_repairs.c.attempt_count + 1,
            "error": None,
            "updated_at": now(),
        }
        if idempotency_key:
            claim_values["idempotency_key"] = idempotency_key
        claim_result = db.execute(
            update(credential_repairs)
            .where(
                credential_repairs.c.id == incident["id"],
                credential_repairs.c.status != PROCESSING,
            )
            .values(**claim_values)
        )
        claim_acquired = getattr(claim_result, "rowcount", 0) == 1
        return {
            "already_valid": False,
            "claim_acquired": claim_acquired,
            "claim_token": claim_token,
            "repair_id": incident["id"],
            "verification": verification,
        }


def _wait_for_claim(run_id, repair_id, actor=None, attempts=100, interval=0.02):
    """Wait for the winning concurrent repair to commit."""
    from src.config.resources import engine

    for _ in range(attempts):
        sleep(interval)
        with engine.connect() as db:
            run = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
            repair = get_repair(db, repair_id)
            if run is None or repair is None:
                return None
            if evaluate(dict(run))["valid"] and repair.get("status") == REPAIRED:
                verification = get_verification(db, run_id)
                duplicate = _repair_dict(repair, duplicate=True)
                return {
                    "http_status": 200,
                    "detail": "同一份坏凭据已经修复",
                    "message": "同一份坏凭据已经修复",
                    "run_id": run_id,
                    "repair": duplicate,
                    "verification": verification,
                    **duplicate,
                    "already_valid": True,
                    "duplicate": True,
                    "idempotent_replay": True,
                    "replayed": False,
                }
            if repair.get("status") in {FAILED, FINGERPRINT_MISMATCH, INTERRUPTED}:
                # Let this request make one more deterministic attempt.
                return None
    return None


def repair_run(run_id, actor=None, idempotency_key=None):
    """Synchronously verify, retain and deterministically repair one run."""
    from src.config.resources import engine

    actor = actor or "system"
    preparation = prepare_repair_state(run_id, actor, idempotency_key)
    if preparation["already_valid"]:
        return preparation["body"]
    if not preparation.get("claim_acquired", True):
        settled = _wait_for_claim(run_id, preparation["repair_id"], actor)
        if settled is not None:
            return settled

    with engine.begin() as db:
        run = _locked_run(db, run_id)
        if run is None:
            raise HTTPException(404, "执行批次不存在")
        if run["status"] != JobState.DONE:
            raise HTTPException(409, "只有验坏的已完成批次可以修复")
        run = dict(run)
        evaluation = evaluate(run)

        # A concurrent repair may already have committed while this caller was
        # waiting on the run row lock.  Re-evaluate after acquiring the lock.
        if evaluation["valid"]:
            verification = persist_verification(db, run, actor)
            repair = reconcile_valid_repair(db, run_id, evaluation["actual_digest"])
            body = {
                "detail": "凭据验单通过，无需修复",
                "message": "凭据验单通过，无需修复",
                "run_id": run_id,
                "verification": verification,
                "repair": repair,
                "already_valid": True,
                "duplicate": repair is not None,
                "idempotent_replay": True,
                "replayed": False,
                "repaired": True,
                "valid": True,
                "status": REPAIRED if repair else VALID,
                "state": "succeeded" if repair else VALID,
                "download_allowed": True,
                "blocked": False,
                "http_status": 200,
            }
            if repair:
                body.update({k: repair[k] for k in ("id", "repair_id", "credential_id", "repair_count") if k in repair})
            return body

        verification = persist_verification(db, run, actor)
        bad_digest = evaluation["actual_digest"]
        incident_row, _created = retain_bad_credential(db, run, evaluation, actor)
        repair_id = incident_row["id"]

        # Lock the exact damaged-credential ledger row as the final guard.
        incident = db.execute(
            select(credential_repairs)
            .where(credential_repairs.c.id == repair_id)
            .with_for_update()
        ).mappings().first()
        # Do not trust a terminal "repaired" marker when the run body is still
        # invalid: crash recovery must release downloads only after the
        # credential actually verifies.  Falling through retries this one
        # ledger row rather than creating a second bad-credential record.

        timestamp = now()
        # prepare_repair_state already durably claimed this attempt and
        # incremented attempt_count.  Keep the marker/key current here without
        # counting a waiting concurrent request as a second replay attempt.
        db.execute(
            update(credential_repairs)
            .where(credential_repairs.c.id == repair_id)
            .values(
                status=PROCESSING,
                actor=actor,
                idempotency_key=idempotency_key or incident["idempotency_key"],
                updated_at=timestamp,
            )
        )

        try:
            replayed = _replay_original(db, run)
            replay_digest = _digest(replayed)
        except Exception as exc:  # noqa: BLE001 - persist every replay refusal
            reason = str(exc) or "原变更单重放失败"
            db.execute(
                update(credential_repairs)
                .where(credential_repairs.c.id == repair_id)
                .values(status=FAILED, error=reason[:320], updated_at=now())
            )
            db.execute(
                update(credential_verifications)
                .where(credential_verifications.c.run_id == run_id)
                .values(download_allowed=False, status=INVALID, last_error=reason[:320], checked_at=now())
            )
            audit.record(db, "repair_failed", actor, "run", run_id)
            repair = get_repair(db, repair_id)
            verification = get_verification(db, run_id)
            return {
                "http_status": 422 if isinstance(exc, ValueError) else 409,
                "detail": f"重放失败，坏批次继续停发：{reason}",
                "message": f"重放失败，坏批次继续停发：{reason}",
                "code": "repair_replay_failed",
                "run_id": run_id,
                "repair": repair,
                "verification": verification,
                "repaired": False,
                "valid": False,
                "download_allowed": False,
                "blocked": True,
            }

        expected_digest = evaluation.get("expected_digest")
        if not expected_digest or replay_digest != expected_digest:
            reason = "重放指纹与完工指纹不一致"
            db.execute(
                update(credential_repairs)
                .where(credential_repairs.c.id == repair_id)
                .values(
                    status=FINGERPRINT_MISMATCH,
                    replay_digest=replay_digest,
                    error=reason,
                    updated_at=now(),
                )
            )
            db.execute(
                update(credential_verifications)
                .where(credential_verifications.c.run_id == run_id)
                .values(
                    status=INVALID,
                    download_allowed=False,
                    replay_digest=replay_digest,
                    last_error=reason,
                    checked_at=now(),
                )
            )
            audit.record(db, "repair_fingerprint_mismatch", actor, "run", run_id)
            repair = get_repair(db, repair_id)
            verification = get_verification(db, run_id)
            return {
                "http_status": 409,
                "detail": reason + "，下载仍保持停发",
                "message": reason + "，下载仍保持停发",
                "code": "fingerprint_mismatch",
                "run_id": run_id,
                "expected_digest": expected_digest,
                "replay_digest": replay_digest,
                "repair": repair,
                "verification": verification,
                "repaired": False,
                "valid": False,
                "download_allowed": False,
                "blocked": True,
            }

        # Fingerprint matches: restore the canonical credential body.  The
        # damaged body remains untouched in credential_repairs.bad_credential.
        completed_at = now()
        db.execute(
            update(runs)
            .where(runs.c.id == run_id)
            .values(result=replayed, digest=expected_digest, failure=None)
        )
        db.execute(
            update(credential_repairs)
            .where(credential_repairs.c.id == repair_id)
            .values(
                status=REPAIRED,
                replay_digest=replay_digest,
                repair_count=int(incident["repair_count"] or 0) + 1,
                error=None,
                updated_at=completed_at,
                completed_at=completed_at,
            )
        )
        previous_count = int(verification.get("repair_count") or 0)
        db.execute(
            update(credential_verifications)
            .where(credential_verifications.c.run_id == run_id)
            .values(
                status=VALID,
                download_allowed=True,
                actual_digest=replay_digest,
                replay_digest=replay_digest,
                repair_id=repair_id,
                repair_count=previous_count + 1,
                repaired_at=completed_at,
                checked_at=completed_at,
                last_error=None,
            )
        )
        audit.record(db, "repair", actor, "run", run_id)
        repair = get_repair(db, repair_id)
        verification = get_verification(db, run_id)
        return {
            "http_status": 200,
            "detail": "凭据已修复并通过验单，恢复下载",
            "message": "凭据已修复并通过验单，恢复下载",
            "code": "credential_repaired",
            "run_id": run_id,
            "repair": repair,
            "verification": verification,
            **repair,
            "repaired": True,
            "valid": True,
            "download_allowed": True,
            "blocked": False,
        }


def candidate_result(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("result", "credential", "evidence", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            if isinstance(value.get("result"), dict):
                return value["result"]
            if "rows" in value or "summary" in value or "metrics" in value:
                return value
    if "rows" in payload or "summary" in payload or "metrics" in payload:
        return payload
    return None


_candidate_result = candidate_result


def restore_credential(run_id, payload, actor=None):
    """Accept a replenished credential only when its fingerprint verifies."""
    from src.config.resources import engine

    actor = actor or "system"
    candidate = candidate_result(payload)
    if candidate is None:
        raise HTTPException(422, "请求体必须包含可验单的 result/credential 凭据")

    with engine.begin() as db:
        run = _locked_run(db, run_id)
        if run is None:
            raise HTTPException(404, "执行批次不存在")
        if run["status"] != JobState.DONE:
            raise HTTPException(409, "只有已完成批次可以补回凭据")
        run = dict(run)

        # A concurrent restoration may already have committed while waiting.
        if evaluate(run)["valid"]:
            verification = persist_verification(db, run, actor)
            return {
                "http_status": 200,
                "detail": "凭据已通过验单",
                "run_id": run_id,
                "verification": verification,
                "repaired": True,
                "valid": True,
                "download_allowed": True,
                "duplicate": True,
            }

        expected_digest = evaluate(run)["expected_digest"]
        candidate_digest = _digest(candidate)
        if not expected_digest or candidate_digest != expected_digest:
            verification = persist_verification(db, run, actor)
            return {
                "http_status": 409,
                "detail": "补回凭据的指纹与完工指纹不一致，批次继续停发",
                "message": "补回凭据的指纹与完工指纹不一致，批次继续停发",
                "code": "fingerprint_mismatch",
                "run_id": run_id,
                "expected_digest": expected_digest,
                "actual_digest": candidate_digest,
                "verification": verification,
                "valid": False,
                "repaired": False,
                "download_allowed": False,
                "blocked": True,
            }

        db.execute(update(runs).where(runs.c.id == run_id).values(result=candidate, failure=None))
        restored_run = {**run, "result": candidate}
        verification = persist_verification(db, restored_run, actor)
        latest = db.execute(
            select(credential_repairs)
            .where(credential_repairs.c.run_id == run_id)
            .order_by(credential_repairs.c.id.desc())
            .limit(1)
            .with_for_update()
        ).mappings().first()
        if latest is not None and latest["status"] != REPAIRED:
            completed_at = now()
            db.execute(
                update(credential_repairs)
                .where(credential_repairs.c.id == latest["id"])
                .values(status=REPAIRED, replay_digest=candidate_digest, completed_at=completed_at,
                        updated_at=completed_at, error=None)
            )
        audit.record(db, "credential_restored", actor, "run", run_id)
        return {
            "http_status": 200,
            "detail": "补回凭据通过验单，恢复下载",
            "message": "补回凭据通过验单，恢复下载",
            "run_id": run_id,
            "verification": verification,
            "repair": latest_repair(db, run_id),
            "repaired": True,
            "valid": True,
            "download_allowed": True,
            "blocked": False,
        }


def recover_interrupted(actor="recovery"):
    """Reconcile durable state after a process interruption.

    A non-terminal repair ledger row never releases a batch by itself.  Each
    interrupted/processing incident is rechecked against the actual stored
    credential.  A batch is released only if its current credential passes
    verification; otherwise it remains held.  The final scan also reconciles
    every persisted invalid verification, so restart recovery reports and
    preserves all bad batches even if the interruption happened before a
    processing marker was committed.
    """
    from src.config.resources import engine

    interrupted_runs = []
    released_runs = []
    with engine.begin() as db:
        stale_rows = db.execute(
            select(credential_repairs).where(credential_repairs.c.status.in_([PROCESSING, "repairing", "pending"]))
        ).mappings().all()
        for row in stale_rows:
            interrupted_runs.append(row["run_id"])
            run = _locked_run(db, row["run_id"])
            if run is None:
                continue
            run = dict(run)
            evaluation = evaluate(run)
            timestamp = now()
            if evaluation["valid"]:
                db.execute(
                    update(credential_repairs)
                    .where(credential_repairs.c.id == row["id"])
                    .values(status=REPAIRED, completed_at=timestamp, updated_at=timestamp)
                )
                persist_verification(db, run, actor)
                released_runs.append(row["run_id"])
            else:
                db.execute(
                    update(credential_repairs)
                    .where(credential_repairs.c.id == row["id"])
                    .values(status=INTERRUPTED, updated_at=timestamp,
                            error=row["error"] or "修复过程中断，等待补回凭据后重新验单")
                )
                existing = get_verification(db, run["id"])
                if existing is None:
                    persist_verification(db, run, actor)
                else:
                    db.execute(
                        update(credential_verifications)
                        .where(credential_verifications.c.run_id == run["id"])
                        .values(status=INVALID, download_allowed=False, checked_at=timestamp,
                                last_error="修复过程中断，批次继续停发")
                    )

        # Reconcile every held verification against the credential bytes now
        # present in the database.  Manual restoration followed by recovery is
        # accepted only when the completion fingerprint verifies.
        held_runs = []
        held_rows = db.execute(
            select(credential_verifications).where(
                (credential_verifications.c.status == INVALID)
                | (credential_verifications.c.download_allowed == False)  # noqa: E712
            )
        ).mappings().all()
        for item in held_rows:
            run = _locked_run(db, item["run_id"])
            if run is None or run["status"] != JobState.DONE:
                continue
            run = dict(run)
            if evaluate(run)["valid"]:
                persist_verification(db, run, actor)
                released_runs.append(run["id"])
            else:
                held_runs.append(run["id"])

        held_runs = sorted(set(held_runs))
        released_runs = sorted(set(released_runs) - set(held_runs))
        if interrupted_runs:
            audit.record(db, "credential_recovery", actor, "run", 0)
    return {
        "recovered": len(interrupted_runs),
        "interrupted_count": len(interrupted_runs),
        "interrupted_runs": interrupted_runs,
        "released_runs": released_runs,
        "held": len(held_runs),
        "held_count": len(held_runs),
        "held_runs": held_runs,
        "blocked_runs": held_runs,
        "quarantined_runs": held_runs,
        "detail": "中断修复已恢复；未通过验单的批次继续停发",
    }


def resolve_repair_id(db, repair_ref):
    """Accept either a ledger id or a run/batch id for retry endpoints."""
    value = int(repair_ref)
    repair = get_repair(db, value)
    if repair is not None:
        return repair["run_id"]
    run = db.execute(select(runs.c.id).where(runs.c.id == value)).first()
    if run is not None:
        return value
    raise HTTPException(404, "修复记录不存在")


# Common service-name aliases used by route modules and acceptance scripts.
repair_credential = repair_run
repair_evidence = repair_run
verify_evidence = verify_run
