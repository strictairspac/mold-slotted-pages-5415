"""Pure credential/fingerprint verification helpers."""
from src.domain.scenario import run as replay_scenario
from src.utils.serial import content_digest


def fingerprint(value):
    return content_digest(value)


def verify_credential(credential=None, expected_digest=None, fingerprint_value=None, **kwargs):
    """Verify either a run mapping or ``(credential, digest)`` values.

    Accepts the common keyword aliases used by acceptance scripts and returns
    a boolean.  Use :func:`inspect_credential` for the structured report.
    """
    if expected_digest is None:
        expected_digest = fingerprint_value or kwargs.get("completion_digest") or kwargs.get("digest")
    if credential is None and "result" in kwargs:
        credential = kwargs["result"]
    if isinstance(credential, dict) and expected_digest is None and "digest" in credential:
        expected_digest = credential["digest"]
        credential = credential.get("result", credential)
    if expected_digest is None:
        return False
    return content_digest(credential) == expected_digest


# English verb alias.
verify = verify_credential


def inspect_credential(run):
    expected = run.get("digest") if isinstance(run, dict) else None
    actual = content_digest(run.get("result") if isinstance(run, dict) else run)
    valid = bool(expected) and actual == expected
    return {
        "valid": valid,
        "ok": valid,
        "status": "valid" if valid else "invalid",
        "expected_digest": expected,
        "actual_digest": actual,
        "download_allowed": valid,
        "blocked": not valid,
    }


def replay_matches(replayed, expected_digest):
    return bool(expected_digest) and content_digest(replayed) == expected_digest


def replay_credential(payload, page_capacity):
    """Replay the original change order at its original page capacity."""
    return replay_scenario(payload, page_capacity, None)
