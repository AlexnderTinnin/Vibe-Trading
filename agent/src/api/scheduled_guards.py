"""HTTP guards for the scheduled-runs routes (scheduler unification).

Extracted from ``scheduled_routes.py`` so the system-job mutation rules and
the authorable-job-id rule live in one module instead of growing the routes
file (which is already over the repo's 800-line hard cap). Each guard raises
the ``HTTPException`` the route would have raised; the routes stay thin
one-line calls. The domain predicates and the shared id grammar come from
:mod:`src.scheduling.system_jobs` — this module adds only the HTTP mapping.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from src.scheduling.system_jobs import JOB_ID_RULE_MESSAGE, is_safe_job_id, is_system_job_id


def ensure_authorable_job_id(job_id: Optional[str]) -> None:
    """Reject a caller-supplied job id the address-based routes could not serve.

    A job whose id the delete/patch routes would refuse (anything outside
    :data:`~src.scheduling.system_jobs.SAFE_JOB_ID_RE`, including every
    ``system:``-prefixed id) can never be cancelled or edited through the
    API, so the id is held to that same rule at creation rather than at
    first attempted delete. ``None`` (no explicit id) is accepted.

    Raises:
        HTTPException: 422 when *job_id* is present and not authorable.
    """
    if job_id is not None and not is_safe_job_id(job_id):
        raise HTTPException(status_code=422, detail=JOB_ID_RULE_MESSAGE)


def guard_system_job_mutation(job_id: str, source_type: str) -> None:
    """Refuse to modify a protected system job.

    Defense in depth for non-REST callers (CLI, internal services, future
    entry points). UNREACHABLE via REST: every system job id carries ':'
    (SYSTEM_JOB_ID_PREFIX), which the host path-parameter validation rejects
    with a 400 before the store is even read — the REST-visible behavior for
    a system job is that 400, not this 403.

    Raises:
        HTTPException: 403 when the row is a system job by id or source.
    """
    if is_system_job_id(job_id) or source_type == "system":
        raise HTTPException(status_code=403, detail="system jobs cannot be modified")


def guard_system_job_deletion(job_id: str) -> None:
    """Refuse to delete a protected system job.

    Defense in depth, same contract as :func:`guard_system_job_mutation`:
    via REST the ':' in every system id already fails path-parameter
    validation with a 400 before this guard runs.

    Raises:
        HTTPException: 403 when *job_id* is a system job id.
    """
    if is_system_job_id(job_id):
        raise HTTPException(status_code=403, detail="system jobs cannot be deleted")
