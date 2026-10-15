"""Persisted cadence and measured model budgets for autonomous live decisions.

Trading authority remains in the mandate gate. These operator-owned settings
control model spending only. Usage arrives after a provider response, so token
and estimated-dollar stops can overshoot by that response, never silently reset
on runner restart. Budget days are UTC; daily schedules use an IANA timezone.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.live.paths import broker_dir


class DecisionSettings(BaseModel):
    """Operator settings shared by the REST, CLI and Web control surfaces."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    interval_seconds: int = Field(default=900, ge=60, le=86400, strict=True)
    daily_at: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "America/New_York"
    max_daily_turns: int = Field(default=26, ge=1, le=1440, strict=True)
    max_daily_tokens: int = Field(default=1_000_000, ge=1, strict=True)
    max_daily_cost_usd: float | None = Field(default=None, gt=0, strict=True)
    input_usd_per_million: float | None = Field(default=None, ge=0, strict=True)
    output_usd_per_million: float | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_pricing_and_zone(self) -> "DecisionSettings":
        """Reject unknown timezones and dollar limits without operator prices."""
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        rates = (self.input_usd_per_million, self.output_usd_per_million)
        if (rates[0] is None) != (rates[1] is None):
            raise ValueError("both input and output prices are required")
        if self.max_daily_cost_usd is not None and rates[0] is None:
            raise ValueError("a dollar limit requires input and output prices")
        return self


class DecisionBudgetError(RuntimeError):
    """Model execution must stop because its budget cannot be honored."""


class DecisionBudget:
    """SQLite-backed settings, decision reservations and idempotent usage rows."""

    def __init__(self, broker: str, *, path: Path | None = None) -> None:
        """Resolve the broker's private runtime store without creating it."""
        self.path = path or broker_dir(broker) / "runtime" / "decisions.sqlite3"

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(
            "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS decisions ("
            "id TEXT PRIMARY KEY, started REAL NOT NULL, finished INTEGER NOT NULL DEFAULT 0, "
            "accounting_error INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS usage ("
            "decision TEXT NOT NULL REFERENCES decisions(id), iteration INTEGER NOT NULL, "
            "tokens INTEGER NOT NULL, cost REAL, recorded REAL NOT NULL, PRIMARY KEY(decision, iteration));"
            )
            with db:
                yield db
        finally:
            db.close()

    def settings(self) -> DecisionSettings:
        """Read validated settings; a corrupt store never resets to defaults."""
        if not self.path.exists():
            return DecisionSettings()
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return DecisionSettings.model_validate_json(row[0]) if row else DecisionSettings()

    def save_settings(self, settings: DecisionSettings) -> None:
        """Save validated operator settings without deleting spending history."""
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (settings.model_dump_json(),),
            )

    @staticmethod
    def _totals(db: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = db.execute(
            "SELECT d.id, d.finished, d.accounting_error, count(u.iteration) calls, coalesce(sum(u.tokens),0) tokens, "
            "sum(u.cost) cost, sum(u.cost IS NULL AND u.iteration IS NOT NULL) unpriced "
            "FROM decisions d LEFT JOIN usage u ON d.id=u.decision WHERE d.started>=? "
            "GROUP BY d.id", (start,),
        ).fetchall()
        measured = db.execute(
            "SELECT coalesce(sum(tokens),0) tokens, sum(cost) cost, "
            "sum(cost IS NULL) unpriced FROM usage WHERE recorded>=?", (start,),
        ).fetchone()
        unaccounted = sum(bool(r["accounting_error"] or (r["finished"] and not r["calls"])) for r in rows)
        return {
            "day": now.astimezone(timezone.utc).date().isoformat(),
            "turns": len(rows),
            "tokens": measured["tokens"],
            "estimated_cost_usd": (measured["cost"] or 0) if not measured["unpriced"] and not unaccounted else None,
            "unaccounted_turns": unaccounted,
        }

    @staticmethod
    def _check(settings: DecisionSettings, totals: dict[str, Any], *, starting: bool) -> None:
        if totals["unaccounted_turns"]:
            raise DecisionBudgetError("provider usage is unavailable for a prior decision today")
        if starting and totals["turns"] >= settings.max_daily_turns:
            raise DecisionBudgetError("daily model decision limit reached")
        if totals["tokens"] >= settings.max_daily_tokens:
            raise DecisionBudgetError("daily model token budget reached")
        if settings.max_daily_cost_usd is not None:
            cost = totals["estimated_cost_usd"]
            if cost is None:
                raise DecisionBudgetError("earlier model usage has no configured price")
            if cost >= settings.max_daily_cost_usd:
                raise DecisionBudgetError("daily estimated model cost budget reached")

    def begin(self, settings: DecisionSettings, now: datetime) -> str | None:
        """Atomically reserve a due decision, or return None between decisions.

        Raises:
            DecisionBudgetError: If a limit or an unfinished prior turn blocks it.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM decisions WHERE finished=0 LIMIT 1").fetchone():
                raise DecisionBudgetError("an unfinished model decision requires inspection before restart")
            self._check(settings, self._totals(db, now), starting=True)
            last = db.execute("SELECT max(started) FROM decisions").fetchone()[0]
            if settings.daily_at:
                local = now.astimezone(ZoneInfo(settings.timezone))
                if local.strftime("%H:%M") < settings.daily_at:
                    return None
                if last is not None and datetime.fromtimestamp(last, ZoneInfo(settings.timezone)).date() >= local.date():
                    return None
            elif last is not None and now.timestamp() - last < settings.interval_seconds:
                return None
            decision = uuid.uuid4().hex
            db.execute("INSERT INTO decisions(id, started) VALUES (?, ?)", (decision, now.timestamp()))
            return decision

    def record(self, decision: str, usage: dict[str, Any], settings: DecisionSettings, now: datetime) -> None:
        """Persist one provider usage event before enforcing a measured stop.

        Dollar figures use operator-supplied prices, without cache discounts.
        Set the input rate to the highest applicable input/cache-write rate.
        """
        values = [usage.get(k) for k in ("iter", "input_tokens", "output_tokens", "total_tokens")]
        if any(type(v) is not int or v < 0 for v in values) or not max(values[3], values[1] + values[2]):
            with self._connect() as db:
                db.execute("UPDATE decisions SET accounting_error=1 WHERE id=?", (decision,))
            raise DecisionBudgetError("provider usage is missing or invalid")
        iteration, tokens_in, tokens_out, reported = values
        tokens = max(reported, tokens_in + tokens_out)
        cost = None
        if settings.input_usd_per_million is not None:
            cost = (tokens_in * settings.input_usd_per_million + tokens_out * settings.output_usd_per_million) / 1_000_000
            # Some providers report additional tokens without a category.
            cost += (tokens - tokens_in - tokens_out) * max(settings.input_usd_per_million, settings.output_usd_per_million) / 1_000_000
            if not math.isfinite(cost):
                with self._connect() as db:
                    db.execute("UPDATE decisions SET accounting_error=1 WHERE id=?", (decision,))
                raise DecisionBudgetError("estimated model cost is not finite")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO usage VALUES (?, ?, ?, ?, ?)", (decision, iteration, tokens, cost, now.timestamp()))
            totals = self._totals(db, now)
        self._check(settings, totals, starting=False)

    def finish(self, decision: str, settings: DecisionSettings, now: datetime) -> None:
        """Close the reservation, refusing unreported usage rather than a zero bill."""
        with self._connect() as db:
            db.execute("UPDATE decisions SET finished=1 WHERE id=?", (decision,))
            accounted = db.execute(
                "SELECT accounting_error=0 AND EXISTS(SELECT 1 FROM usage WHERE decision=?) "
                "FROM decisions WHERE id=?", (decision, decision),
            ).fetchone()
            totals = self._totals(db, now)
        if not accounted or not accounted[0]:
            raise DecisionBudgetError("provider usage is unavailable for this decision")
        self._check(settings, totals, starting=False)

    def snapshot(self, now: datetime) -> dict[str, Any]:
        """Return control settings and today's measured usage for the operator."""
        settings = self.settings()
        if self.path.exists():
            with self._connect() as db:
                totals = self._totals(db, now)
        else:
            totals = {"day": now.astimezone(timezone.utc).date().isoformat(), "turns": 0,
                      "tokens": 0, "estimated_cost_usd": None, "unaccounted_turns": 0}
        projected = None
        if settings.input_usd_per_million is None:
            totals["estimated_cost_usd"] = None
        if totals["turns"] and totals["estimated_cost_usd"] is not None:
            projected = totals["estimated_cost_usd"] / totals["turns"] * (1 if settings.daily_at else settings.max_daily_turns)
        return {"settings": settings.model_dump(), "usage": totals, "projected_daily_cost_usd": projected}
