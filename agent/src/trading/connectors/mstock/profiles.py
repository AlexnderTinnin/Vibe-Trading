"""Read-only mStock Type B books; portfolio valuation is not yet mapped."""

from __future__ import annotations

from src.trading.types import TradingProfile

# Holdings/net positions can be read, but no canonical cash/net-equity account
# summary is known. Withhold account.read so portfolio eligibility stays false.
MSTOCK_READ_CAPABILITIES = ("positions.read",)

MSTOCK_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="mstock-live-rest-readonly",
        connector="mstock",
        label="mStock Live · REST Read-Only (India)",
        environment="live",
        transport="broker_sdk",
        capabilities=MSTOCK_READ_CAPABILITIES,
        readonly=True,
        config={"auth_flavor": "typeb"},
        notes=(
            "Read-only Type B holdings and net-position books using the API key "
            "and current session access token. Fund limits are not net assets; "
            "portfolio aggregation is unavailable. All order operations are disabled."
        ),
    ),
)
