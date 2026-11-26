from pydantic import BaseModel, Field
from src.constants.job_state import JobState


class RunInput(BaseModel):
    parameter: int = Field(strict=True, ge=1)


class RunEntry(BaseModel):
    id: int
    case_id: int
    case_title: str
    status: JobState
    parameter: int
    actor: str
    created_at: int
    finished_at: int | None
    result: dict | None
    failure: str | None
    digest: str | None
    verification_status: str | None = None
    credential_status: str | None = None
    integrity_status: str | None = None
    quarantine_status: str | None = None
    delivery_status: str | None = None
    download_allowed: bool | None = None
    blocked: bool | None = None
    quarantined: bool | None = None
    verified: bool | None = None
    repair_count: int | None = None
