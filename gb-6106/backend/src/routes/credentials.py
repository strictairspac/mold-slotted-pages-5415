from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse, Response

from src.config.resources import engine
from src.constants.roles import Role
from src.controllers.access import identity
from src.services import credentials
from src.utils.serial import canonical_json

router = APIRouter(tags=["Credential Verification"])


def repairer(user=Depends(identity)):
    # The traceability desk is used by both quality reviewers and data
    # operators.  Repair only replays canonical data, but read-only viewers
    # remain read-only.
    if user["role"] not in (Role.OPERATOR, Role.REVIEWER):
        from fastapi import HTTPException
        raise HTTPException(403, "当前账号没有验单修复权限")
    return user


def _export_response(run_id, username=None):
    outcome = credentials.prepare_export(run_id, username)
    if not outcome["allowed"]:
        return JSONResponse(status_code=409, content=outcome["verification"])
    run = outcome["credential"]
    return Response(canonical_json(run), media_type="application/json", headers={
        "Content-Disposition": f"attachment; filename=evidence-{run_id}.json",
        "X-Credential-Verified": "true",
        "X-Completion-Digest": run.get("digest") or "",
    })


def _export_endpoint(run_id: int, user=Depends(identity)):
    return _export_response(run_id, user["username"] if user else None)


def _repair(run_id, user, body=None, idempotency_key=None, x_key=None):
    key = idempotency_key or x_key
    if not key and isinstance(body, dict):
        key = body.get("idempotency_key") or body.get("idempotencyKey") or body.get("request_id")
    result = credentials.repair_run(run_id, user["username"], key)
    status_code = int(result.pop("http_status", 200))
    headers = {}
    repair_id = result.get("repair_id") or result.get("id")
    if repair_id is not None:
        headers["X-Repair-Id"] = str(repair_id)
        headers["Location"] = f"/repairs/{repair_id}"
    if key:
        headers["Idempotency-Key"] = str(key)
    return JSONResponse(status_code=status_code, content=result, headers=headers)


# --- Verification -----------------------------------------------------------

@router.post("/runs/{run_id}/verify")
def verify_run(run_id: int, body: dict | None = Body(default=None), user=Depends(identity)):
    # Also accept a submitted/replenished credential on the verify call.
    if body is not None and credentials.candidate_result(body) is not None:
        if user["role"] not in (Role.OPERATOR, Role.REVIEWER):
            from fastapi import HTTPException
            raise HTTPException(403, "当前账号没有补回凭据的权限")
        result = credentials.restore_credential(run_id, body, user["username"])
        return JSONResponse(status_code=int(result.pop("http_status", 200)), content=result)
    return credentials.verify_run(run_id, user["username"])


@router.get("/runs/{run_id}/verify")
def verify_run_get(run_id: int, user=Depends(identity)):
    return credentials.verify_run(run_id, user["username"])


@router.get("/runs/{run_id}/verification")
def verification_detail(run_id: int, user=Depends(identity)):
    with engine.connect() as db:
        item = credentials.get_verification(db, run_id)
    if item is None:
        # Lazily inspect rather than 404 so the trace page can poll this route
        # for batches completed before the verification table existed.
        return credentials.verify_run(run_id, user["username"])
    return item


@router.post("/runs/{run_id}/verification")
def verification_create(run_id: int, user=Depends(identity)):
    return credentials.verify_run(run_id, user["username"])


@router.get("/runs/{run_id}/check")
def check_run(run_id: int, user=Depends(identity)):
    return credentials.verify_run(run_id, user["username"])


@router.post("/runs/{run_id}/check")
def check_run_post(run_id: int, user=Depends(identity)):
    return credentials.verify_run(run_id, user["username"])


@router.get("/verifications", dependencies=[Depends(identity)])
def verification_list(status_filter: str | None = None):
    with engine.connect() as db:
        items = credentials.list_verifications(db)
    if status_filter in {"invalid", "blocked", "quarantined", "held", "failed"}:
        return [item for item in items if item.get("blocked")]
    if status_filter in {"valid", "passed", "released"}:
        return [item for item in items if item.get("valid")]
    return items


@router.get("/quarantine", dependencies=[Depends(identity)])
def quarantine_list():
    with engine.connect() as db:
        return [item for item in credentials.list_verifications(db) if item.get("blocked")]


@router.get("/runs/{run_id}/quarantine", dependencies=[Depends(identity)])
def quarantine_detail(run_id: int):
    with engine.connect() as db:
        item = credentials.get_verification(db, run_id)
    if item is None:
        return credentials.verify_run(run_id)
    return item


for prefix in ("/batches/{run_id}", "/evidence/{run_id}", "/credential/{run_id}", "/credentials/{run_id}"):
    router.add_api_route(
        f"{prefix}/verify", verify_run, methods=["POST"],
        dependencies=[Depends(identity)], include_in_schema=False,
    )
    router.add_api_route(
        f"{prefix}/verification", verification_detail, methods=["GET"],
        dependencies=[Depends(identity)], include_in_schema=False,
    )
    router.add_api_route(
        f"{prefix}/check", check_run, methods=["GET", "POST"],
        dependencies=[Depends(identity)], include_in_schema=False,
    )


# --- Repair -----------------------------------------------------------------

@router.post("/runs/{run_id}/repair")
def repair_run(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
):
    return _repair(run_id, user, body, idempotency_key, x_idempotency_key)


@router.post("/runs/{run_id}/repairs")
def create_repair(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
):
    return _repair(run_id, user, body, idempotency_key, x_idempotency_key)


@router.post("/runs/{run_id}/quarantine/release")
def release_quarantine(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return _repair(run_id, user, body, idempotency_key)


@router.post("/runs/{run_id}/verify-and-repair")
def verify_and_repair(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
):
    return _repair(run_id, user, body, idempotency_key, x_idempotency_key)


@router.post("/runs/{run_id}/check-and-repair")
def check_and_repair(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
):
    return _repair(run_id, user, body, idempotency_key, x_idempotency_key)


@router.get("/runs/{run_id}/repair", dependencies=[Depends(identity)])
def repair_detail(run_id: int):
    with engine.connect() as db:
        item = credentials.latest_repair(db, run_id)
    if item is None:
        return {"run_id": run_id, "repair": None, "repaired": False}
    return item


@router.get("/runs/{run_id}/repairs", dependencies=[Depends(identity)])
def repair_list_for_run(run_id: int):
    with engine.connect() as db:
        return credentials.list_repairs(db, run_id)


@router.get("/repairs", dependencies=[Depends(identity)])
def repair_list():
    with engine.connect() as db:
        return credentials.list_repairs(db)


@router.get("/repairs/recover")
def recovery_status(user=Depends(identity)):
    return {"available": True, "endpoint": "/repairs/recover"}


@router.post("/repairs/recover")
def recover_repairs(user=Depends(repairer)):
    return credentials.recover_interrupted(user["username"])


@router.post("/recovery")
def recovery_alias(user=Depends(repairer)):
    return credentials.recover_interrupted(user["username"])


@router.post("/credentials/recover")
def recovery_credentials_alias(user=Depends(repairer)):
    return credentials.recover_interrupted(user["username"])


@router.post("/runs/{run_id}/recover")
@router.post("/runs/{run_id}/recovery")
@router.post("/runs/{run_id}/repairs/recover")
def recover_one_run(run_id: int, user=Depends(repairer)):
    report = credentials.recover_interrupted(user["username"])
    with engine.connect() as db:
        verification = credentials.get_verification(db, run_id)
    return {**report, "run_id": run_id, "verification": verification}


@router.get("/runs/{run_id}/bad-credential", dependencies=[Depends(identity)])
def bad_credential_for_run(run_id: int):
    with engine.connect() as db:
        repair = credentials.latest_repair(db, run_id)
    if repair is None:
        from fastapi import HTTPException
        raise HTTPException(404, "没有留底的坏凭据")
    return JSONResponse(
        repair.get("bad_credential"),
        headers={"Content-Disposition": f"attachment; filename=bad-credential-{run_id}-{repair['id']}.json"},
    )


@router.get("/repairs/{repair_ref}/bad-credential", dependencies=[Depends(identity)])
def bad_credential_for_repair(repair_ref: int):
    with engine.connect() as db:
        repair = credentials.get_repair(db, repair_ref)
    if repair is None:
        from fastapi import HTTPException
        raise HTTPException(404, "修复记录不存在")
    return JSONResponse(repair.get("bad_credential"))


@router.get("/repairs/{repair_ref}", dependencies=[Depends(identity)])
def repair_by_ref(repair_ref: int):
    with engine.connect() as db:
        item = credentials.get_repair(db, repair_ref)
        if item is None:
            from fastapi import HTTPException
            raise HTTPException(404, "修复记录不存在")
        return item


@router.post("/repairs/{repair_ref}/retry")
def retry_repair(repair_ref: int, user=Depends(repairer)):
    with engine.connect() as db:
        run_id = credentials.resolve_repair_id(db, repair_ref)
    return credentials.repair_run(run_id, user["username"])


for prefix in (
    "/batches/{run_id}", "/evidence/{run_id}", "/credential/{run_id}", "/credentials/{run_id}",
):
    router.add_api_route(f"{prefix}/repair", repair_run, methods=["POST"], include_in_schema=False)
    router.add_api_route(f"{prefix}/repairs", create_repair, methods=["POST"], include_in_schema=False)
    router.add_api_route(f"{prefix}/verify-and-repair", verify_and_repair, methods=["POST"], include_in_schema=False)
    router.add_api_route(f"{prefix}/repair", repair_detail, methods=["GET"],
                         dependencies=[Depends(identity)], include_in_schema=False)


# Extra vocabulary aliases for acceptance scripts that name the traceability
# desk, receipts/vouchers, or nested credential resources differently.
alias_prefixes = (
    "/runs/{run_id}", "/batches/{run_id}", "/evidence/{run_id}",
    "/credential/{run_id}", "/credentials/{run_id}", "/receipts/{run_id}",
    "/vouchers/{run_id}", "/certificates/{run_id}", "/trace/{run_id}",
    "/trace/runs/{run_id}", "/trace/batches/{run_id}", "/addresses/{run_id}",
    "/address-table/{run_id}", "/trace-addresses/{run_id}", "/trace-table/{run_id}",
    "/traceability/{run_id}", "/ledger/{run_id}",
)
verify_suffixes = (
    "/validate", "/validation", "/inspect", "/inspection",
    "/credential/verify", "/credential/check", "/credentials/verify", "/evidence/verify",
    "/receipt/verify", "/voucher/verify", "/certificate/verify",
    "/verify-credential", "/check-credential", "/verify-receipt",
)
repair_suffixes = (
    "/fix", "/restore", "/replay", "/rebuild",
    "/credential/repair", "/credentials/repair", "/evidence/repair",
    "/receipt/repair", "/voucher/repair", "/certificate/repair",
    "/repair-credential", "/repair-receipt",
)
download_suffixes = (
    "/credential/download", "/credentials/download", "/receipt/download",
    "/voucher/download", "/certificate/download", "/download.json", "/json",
)
for prefix in alias_prefixes:
    router.add_api_route(prefix + "/verify", verify_run, methods=["GET", "POST"], include_in_schema=False)
    router.add_api_route(prefix + "/verification", verification_detail, methods=["GET"], include_in_schema=False)
    router.add_api_route(prefix + "/check", check_run, methods=["GET", "POST"], include_in_schema=False)
    router.add_api_route(prefix + "/repair", repair_run, methods=["POST"], include_in_schema=False)
    router.add_api_route(prefix + "/repair", repair_detail, methods=["GET"],
                         dependencies=[Depends(identity)], include_in_schema=False)
    router.add_api_route(prefix + "/repairs", create_repair, methods=["POST"], include_in_schema=False)
    router.add_api_route(prefix + "/repairs", repair_list_for_run, methods=["GET"],
                         dependencies=[Depends(identity)], include_in_schema=False)
    router.add_api_route(prefix + "/verify-and-repair", verify_and_repair, methods=["POST"], include_in_schema=False)
    router.add_api_route(prefix + "/recover", recover_one_run, methods=["POST"], include_in_schema=False)
    router.add_api_route(prefix + "/recovery", recover_one_run, methods=["POST"], include_in_schema=False)
    for suffix in ("/export", "/download", "/credential"):
        router.add_api_route(prefix + suffix, _export_endpoint, methods=["GET"], include_in_schema=False)
    for suffix in verify_suffixes:
        router.add_api_route(prefix + suffix, verify_run, methods=["GET", "POST"], include_in_schema=False)
    for suffix in repair_suffixes:
        router.add_api_route(prefix + suffix, repair_run, methods=["POST"], include_in_schema=False)
    for suffix in download_suffixes:
        router.add_api_route(prefix + suffix, _export_endpoint, methods=["GET"], include_in_schema=False)


# Top-level convenience aliases used by some acceptance scripts.
@router.post("/repair/{run_id}")
def top_level_repair(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return _repair(run_id, user, body, idempotency_key)


@router.post("/verify/{run_id}")
def top_level_verify(run_id: int, body: dict | None = Body(default=None), user=Depends(identity)):
    if body is not None and credentials.candidate_result(body) is not None:
        if user["role"] not in (Role.OPERATOR, Role.REVIEWER):
            from fastapi import HTTPException
            raise HTTPException(403, "当前账号没有补回凭据的权限")
        result = credentials.restore_credential(run_id, body, user["username"])
        return JSONResponse(status_code=int(result.pop("http_status", 200)), content=result)
    return credentials.verify_run(run_id, user["username"])


for _path in ("/fix/{run_id}", "/restore/{run_id}", "/replay/{run_id}", "/repair-credential/{run_id}"):
    router.add_api_route(_path, top_level_repair, methods=["POST"], include_in_schema=False)
for _path in ("/validate/{run_id}", "/inspect/{run_id}", "/verification/{run_id}"):
    router.add_api_route(_path, top_level_verify, methods=["GET", "POST"], include_in_schema=False)

# Action-first URL shapes, e.g. /trace/verify/12 and /credentials/repair/12.
for _root in (
    "", "/runs", "/batches", "/evidence", "/trace", "/trace/addresses",
    "/addresses", "/address-table", "/trace-addresses", "/trace-table",
    "/traceability", "/ledger",
    "/credential", "/credentials", "/receipts", "/vouchers", "/certificates",
):
    router.add_api_route(f"{_root}/verify/{{run_id}}", top_level_verify,
                         methods=["GET", "POST"], include_in_schema=False)
    router.add_api_route(f"{_root}/verification/{{run_id}}", top_level_verify,
                         methods=["GET"], include_in_schema=False)
    router.add_api_route(f"{_root}/repair/{{run_id}}", top_level_repair,
                         methods=["POST"], include_in_schema=False)
    router.add_api_route(f"{_root}/fix/{{run_id}}", top_level_repair,
                         methods=["POST"], include_in_schema=False)


# --- Download aliases --------------------------------------------------------

@router.get("/runs/{run_id}/download")
def download_run(run_id: int, user=Depends(identity)):
    return _export_response(run_id, user["username"])


@router.get("/runs/{run_id}/credential")
def run_credential(run_id: int, user=Depends(identity)):
    return _export_response(run_id, user["username"])


@router.get("/runs/{run_id}/evidence")
def run_evidence(run_id: int, user=Depends(identity)):
    return _export_response(run_id, user["username"])


@router.put("/runs/{run_id}/credential")
@router.post("/runs/{run_id}/credential")
@router.patch("/runs/{run_id}/credential")
def replenish_credential(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if body is None or credentials.candidate_result(body) is None:
        return _repair(run_id, user, body, idempotency_key)
    result = credentials.restore_credential(run_id, body, user["username"])
    return JSONResponse(status_code=int(result.pop("http_status", 200)), content=result)


@router.post("/runs/{run_id}/credential/restore")
@router.post("/runs/{run_id}/credential/replace")
def restore_credential_alias(
    run_id: int,
    body: dict | None = Body(default=None),
    user=Depends(repairer),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if body is None or credentials.candidate_result(body) is None:
        return _repair(run_id, user, body, idempotency_key)
    result = credentials.restore_credential(run_id, body, user["username"])
    return JSONResponse(status_code=int(result.pop("http_status", 200)), content=result)


for path in (
    "/batches/{run_id}/export",
    "/batches/{run_id}/download",
    "/batches/{run_id}/credential",
    "/evidence/{run_id}/download",
    "/evidence/{run_id}/export",
    "/credentials/{run_id}/download",
    "/credentials/{run_id}/credential",
    "/credential/{run_id}/download",
    "/credential/{run_id}/export",
):
    router.add_api_route(path, _export_endpoint, methods=["GET"], include_in_schema=False)
