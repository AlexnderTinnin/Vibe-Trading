"""Scheduled Brazilian dataset sync (scheduler unification).

Thin domain surface consumed by the scheduler, mirroring
:mod:`src.memory.consolidation`: it adds NO sync semantics of its own —
every behavior lives in :mod:`src.br_data.sync` and this module only decides
*whether* and *when* the protected system job runs:

* :func:`sync_job_enabled` is the ``enabled_predicate`` of the
  ``system:br-data-sync`` spec in :mod:`src.scheduling.system_jobs`. It reads
  ``VIBE_BR_SYNC_ENABLED`` (``DataConfig.vibe_br_sync_enabled``) through
  :func:`src.config.accessor.get_env_config` — the sanctioned config path.
  The flag defaults to ``False`` and belongs to no preset, so turning the
  scheduler on never silently seeds a daily network-sync job.
* :func:`daily_cron_from_env` is the spec's ``schedule_fn``: it derives the
  5-field cron schedule from the pre-existing ``VIBE_BR_SYNC_TIME``
  (``DataConfig.vibe_br_sync_time``, "HH:MM" on the ``sync.TZ`` São Paulo
  wall clock; default "21:30" → ``"30 21 * * *"``), parsed by
  ``sync._next_run`` — the "HH:MM"→cron-field parser repurposed from the
  retired daemon. It is evaluated lazily inside ``ensure_system_jobs`` (at
  lifespan startup, after dotenv load) — never at import time, which would
  freeze the value before ``.env`` is read. It NEVER raises: an invalid
  value ("25:99" / "" / "abc") falls back to :data:`DEFAULT_DAILY_CRON` with
  a warning, so one typo'd env var can never take the API server down.

Seed-time-only contract: ``ensure_system_jobs`` seeds a row once and a VALID
edited schedule/timezone on that row is trusted afterwards (the system_jobs
local-trust boundary), so ``VIBE_BR_SYNC_TIME`` drives the FIRST seed only.
The row is a protected system job — REST/CLI PATCH and DELETE refuse it — so
retiming an existing seed means editing the ``schedule`` field of the stored
row in the store JSON file (a valid edited schedule is trusted at the next
startup), or deleting the row from that file and restarting to re-seed from
the env value.

The runnable itself is ``sync.run_sync``, registered under the literal key
``"br_data.run_sync"`` in :mod:`src.scheduling.registry`. It is SYNC by
design: the executor's internal-run supervisor offloads sync callables via
``asyncio.to_thread``, so the (network-heavy) sync pass never runs on the
event loop. The registry wrapper converts per-source errors in ``run_sync``'s
report into an exception naming the failed sources. This reaches the executor's
existing failure path (``failure_kind="dispatch"``, bounded backoff). The retired
``python -m src.br_data daemon`` loop is replaced by this job (its
``run_daemon`` now prints a migration notice and returns 0).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Fallback cron when ``VIBE_BR_SYNC_TIME`` is missing or invalid — the same
#: slot the env field's own default ("21:30") derives to.
DEFAULT_DAILY_CRON = "30 21 * * *"


def sync_job_enabled() -> bool:
    """Whether the br-data daily-sync system job is enabled.

    Reads ``VIBE_BR_SYNC_ENABLED`` via the cached ``EnvConfig`` singleton
    (default ``False``; no preset raises it). Bound as the
    ``enabled_predicate`` of the ``system:br-data-sync`` spec.
    """
    from src.config.accessor import get_env_config

    return get_env_config().data.vibe_br_sync_enabled


def daily_cron_from_env() -> str:
    """Derive the daily cron (``"M H * * *"``) from ``VIBE_BR_SYNC_TIME``.

    Evaluated lazily at ``ensure_system_jobs`` time (the spec's
    ``schedule_fn``), so it must NEVER raise: any value ``sync._next_run``
    cannot parse as an in-range "HH:MM" falls back to
    :data:`DEFAULT_DAILY_CRON` with a warning.

    Returns:
        A 5-field cron string, e.g. ``"30 21 * * *"`` for the default
        ``"21:30"`` America/Sao_Paulo wall time.
    """
    from src.br_data.sync import _next_run
    from src.config.accessor import get_env_config

    raw = get_env_config().data.vibe_br_sync_time
    try:
        minute, hour = _next_run(raw)
    except (AttributeError, TypeError, ValueError):
        logger.warning(
            "br-data: invalid VIBE_BR_SYNC_TIME %r — falling back to %s",
            raw,
            DEFAULT_DAILY_CRON,
        )
        return DEFAULT_DAILY_CRON
    return f"{minute} {hour} * * *"
