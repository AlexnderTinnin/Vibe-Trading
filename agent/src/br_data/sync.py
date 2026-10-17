"""Run every Brazilian dataset sync.

The daily cadence is the protected system job ``system:br-data-sync``
(scheduler-unification T9; see :mod:`src.br_data.scheduling`) — the
standalone sleep-loop daemon is retired and :func:`run_daemon` only prints
a migration notice now.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from src.br_data import bcb, cotahist, cvm_companies, cvm_funds, tesouro
from src.br_data.store import connect, init_db
from src.config.accessor import get_env_config

logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/Sao_Paulo")
SOURCES = ("cotahist", "cvm_funds", "cvm_companies", "bcb", "tesouro")


def _today() -> date:
    return datetime.now(TZ).date()


def run_sync(only: Iterable[str] | None = None, *, cotahist_from: int | None = None,
             cvm_from: int | None = None) -> dict[str, object]:
    """Sync the selected sources; a failing source is reported, not fatal."""
    data_cfg = get_env_config().data
    selected = [s for s in SOURCES if not only or s in set(only)]
    today = _today()
    cot_year = cotahist_from or data_cfg.vibe_br_cotahist_start_year
    cvm_year = cvm_from or data_cfg.vibe_br_cvm_start_year
    jobs: dict[str, Callable] = {
        "cotahist": lambda c: cotahist.sync(c, cot_year, today),
        "cvm_funds": lambda c: cvm_funds.sync_register(c) + cvm_funds.sync_daily(c, cvm_year, today),
        "cvm_companies": lambda c: cvm_companies.sync(c, cvm_year, today),
        "bcb": lambda c: bcb.sync(c, min(cot_year, cvm_year), today),
        "tesouro": lambda c: tesouro.sync(c),
    }
    path = init_db()
    report: dict[str, object] = {"db": str(path)}
    for name in selected:
        started = time.monotonic()
        try:
            with connect(path, write=True) as conn:
                rows = jobs[name](conn)
            report[name] = {"rows": rows, "seconds": round(time.monotonic() - started, 1)}
            logger.info("br-data: %s ok (%s rows)", name, rows)
        except Exception as exc:  # noqa: BLE001 - keep the other sources going
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}
            logger.warning("br-data: %s failed: %s", name, exc)
    return report


def _next_run(hhmm: str) -> tuple[int, int]:
    """Parse a wall-clock "HH:MM" into cron ``(minute, hour)`` fields.

    Repurposed from the retired daemon's next-slot math (scheduler
    unification): the cross-day arithmetic now belongs to the scheduling
    kernel (``next_due``), and this helper is the "HH:MM"→cron-field parser consumed
    by :func:`src.br_data.scheduling.daily_cron_from_env`, which owns the
    invalid-value fallback policy.

    Raises:
        ValueError: *hhmm* is not exactly two colon-separated integers within
            clock range (hour 0-23, minute 0-59).
    """
    hour, minute = (int(part) for part in hhmm.split(":", 1))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid HH:MM wall time: {hhmm!r}")
    return minute, hour


def run_daemon(only: Iterable[str] | None = None) -> int:
    """Retired standalone daemon (scheduler unification): notice + ``0``.

    The daily sync now runs as the protected system job
    ``system:br-data-sync`` inside the main service's scheduler. Prints the
    migration notice and returns ``0`` in bounded time — exit code 0 is
    deliberate and test-pinned: under compose ``restart: unless-stopped`` a
    non-zero exit would crash-loop any deployment still invoking this
    command until its compose template drops the sidecar.

    Args:
        only: Accepted for CLI compatibility and ignored (the system job
            always syncs every source, as the daemon did by default).
    """
    print(
        "br-data: the standalone daemon is retired. The daily sync now runs as the\n"
        "protected system job 'system:br-data-sync' inside the main service's\n"
        "scheduler: set VIBE_TRADING_ENABLE_SCHEDULER=1 and VIBE_BR_SYNC_ENABLED=1;\n"
        "VIBE_BR_SYNC_TIME (HH:MM, America/Sao_Paulo, default 21:30) sets the daily\n"
        "slot and drives the first seed. One-off sync: python -m src.br_data sync."
    )
    return 0
