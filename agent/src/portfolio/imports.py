"""Immutable, opt-in extraETF holdings imports, separate from broker connections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root
from src.portfolio.extraetf import parse_export
from src.portfolio.iso4217 import is_iso_currency

MAX_IMPORT_BYTES = 4 * 1024 * 1024


def aware_time(value: str) -> datetime:
    """Parse an explicit timezone-bearing timestamp, refusing naive dates."""
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("source_mtime must be an ISO timestamp with timezone") from exc
    if result.tzinfo is None:
        raise ValueError("source_mtime must include its timezone")
    return result.astimezone(timezone.utc)


class PortfolioImportStore:
    """Store validated exports locally without credentials or inferred identities."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_runtime_root() / "portfolio" / "imports.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        return db

    def create(
        self, *, csv_text: str, label: str, source_mtime: str,
        expected_positions: int, max_age_days: int, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate an entire export before persisting any part of it.

        Args:
            csv_text: Original user-exported Investments CSV, never transactions.
            label: User-visible source name.
            source_mtime: File modification time; not an observation timestamp.
            expected_positions: User-confirmed count, detecting whole-row truncation.
            max_age_days: User's freshness policy, measured from the file mtime.
            now: Injected clock for deterministic validation.

        Returns:
            Public import metadata, without portfolio identifiers or positions.

        Raises:
            ValueError: If any row, count, timestamp or valuation input is invalid.
        """
        now = now or datetime.now(timezone.utc)
        if not isinstance(csv_text, str) or len(csv_text.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise ValueError("extraETF export exceeds the 4 MiB import limit")
        label = label.strip()
        if not label or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise ValueError("import label must contain 1 to 80 printable characters")
        if type(expected_positions) is not int or not 0 <= expected_positions <= 20000:
            raise ValueError("expected_positions must be an integer from 0 to 20000")
        if type(max_age_days) is not int or not 1 <= max_age_days <= 365:
            raise ValueError("max_age_days must be an integer from 1 to 365")
        modified = aware_time(source_mtime)
        if modified > now:
            raise ValueError("file modification time is in the future")
        expires = modified + timedelta(days=max_age_days)
        if expires <= now:
            raise ValueError("extraETF export is stale under the selected freshness policy; export again")
        parsed = parse_export(csv_text)
        positions = list(parsed.require_positions())
        if len(positions) != expected_positions:
            raise ValueError(f"expected {expected_positions} holdings, but the export contains {len(positions)}")
        for row in positions:
            if not is_iso_currency(row["currency"]):
                raise ValueError(f"unsupported currency identity: {row['currency']}")
            price, qty = row.get("market_price"), row["quantity"]
            if price is None or Decimal(str(price)) <= 0 or Decimal(str(qty)) < 0:
                raise ValueError("every imported holding needs a positive exported price and non-negative quantity")
            # An unexplained FX conversion would mix the export's account value
            # with its instrument price. Only the price/quantity book is valued.
            row.update(source_mtime=modified.isoformat(), as_of_source="file_mtime",
                       expires_at=expires.isoformat(), identity_status="unresolved_venue")
        payload = {
            "id": "extraetf-" + uuid.uuid4().hex,
            "source_kind": "extraetf", "label": label,
            "source_mtime": modified.isoformat(), "as_of_source": "file_mtime",
            "imported_at": now.isoformat(), "expires_at": expires.isoformat(),
            "max_age_days": max_age_days, "expected_positions": expected_positions,
            "sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
            "valuation_basis": "exported_quantity_times_price", "cash_included": False,
            "positions": positions,
        }
        with self._connect() as db:
            db.execute("INSERT INTO imports VALUES (?, ?)", (payload["id"], json.dumps(payload, ensure_ascii=False)))
        return self.metadata(payload)

    @staticmethod
    def metadata(payload: dict[str, Any]) -> dict[str, Any]:
        """Return source metadata without underlying account identifiers."""
        return {k: v for k, v in payload.items() if k != "positions"}

    def get(self, source_id: str, *, require_fresh: bool = False) -> dict[str, Any]:
        """Read one import, optionally failing once its freshness policy expires."""
        with self._connect() as db:
            row = db.execute("SELECT payload FROM imports WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown portfolio import: {source_id}")
        payload = json.loads(row[0])
        if require_fresh and aware_time(payload["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("extraETF export is stale; import a new Investments export")
        return payload

    def list(self) -> list[dict[str, Any]]:
        """List import metadata; expired imports remain visible for replacement."""
        if not self.path.exists():
            return []
        with self._connect() as db:
            return [self.metadata(json.loads(row[0])) for row in db.execute("SELECT payload FROM imports ORDER BY rowid")]

    def collect(self, source: Any) -> dict[str, Any]:
        """Produce a holdings-only source; never read a broker or request a quote."""
        payload = self.get(source.id, require_fresh=True)
        positions = payload.pop("positions")
        for row in positions:
            row.update(source_id=source.id, source_kind="extraetf", source_label=source.label,
                       profile_id=None)
        return {"status": "ok", "positions": positions, "account": {}, "import_metadata": payload}
