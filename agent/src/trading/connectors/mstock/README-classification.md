# mStock read-only boundary

This connector sends only GET requests to the documented Type B resources:
`portfolio/holdings`, `portfolio/positions`, and `user/fundsummary`. Credentials
come from the connection's OS credential vault. It never logs in, refreshes a
session, follows a redirect, or calls a logout/order endpoint. Both order entry
points refuse before any network call. The curated classification map also pins
those entry points as writes.

The official login example reports `state: "live"`, but this connector accepts an
existing session token and has not verified any structural paper/live boundary.
It exposes no paper simulation or live trading capability. A new order transport
would require its own verified discriminator and mandate gate.

Holdings and net positions remain separate books. A holding's `tradingsymbol`
can be a company name and its exchange can be null; these are not invented ticker
symbols. Available funds, collateral and utilized margin do not establish cash
or net liquidation. The profile therefore withholds `account.read` and is not
eligible for portfolio aggregation. Raw fund limits remain inspectable through
`trading_account` / `vibe-trading connector account mstock-live-rest-readonly --connection-id <id>`.

Set up `mstock-live-rest-readonly` in the connection center, or use
`vibe-trading connector setup --help` for the shared CLI flow. Supply the API key
and current Type B session access token; the broker documents midnight token
expiry. The connection test reads both position books without placing orders.

References (checked 2026-10-13):
- [Official authentication and fund-summary contract](https://tradingapi.mstock.com/docs/v1/typeB/User/)
- [Official holdings contract](https://tradingapi.mstock.com/docs/v1/typeB/Portfolio/)
- [Official net-position contract](https://tradingapi.mstock.com/docs/v1/typeB/Position/)

Tests replay those documented shapes through the real HTTP client. An actual
mStock account was unavailable; account-value normalization remains unverified.
