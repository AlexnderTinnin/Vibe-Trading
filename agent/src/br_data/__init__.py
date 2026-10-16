"""Free, official Brazilian market data synced into one local SQLite file.

Sources (all public, no key):

* B3 COTAHIST — daily quotes of every instrument traded on B3 (shares, units,
  FIIs, ETFs, BDRs, options, forward market) since 1986.
* CVM Dados Abertos — daily NAV/quota of every registered investment fund,
  the fund register, the listed-company register and annual/quarterly
  financial statements (DFP/ITR).
* Banco Central (SGS) — Selic, CDI, IPCA, IGP-M, PTAX and any other series.
* Tesouro Transparente — Tesouro Direto bond prices and yields.

``python -m src.br_data sync`` downloads/refreshes everything; the daily
cadence runs as the protected system job ``system:br-data-sync`` inside the
main service's scheduler (opt-in via ``VIBE_BR_SYNC_ENABLED`` — see
:mod:`src.br_data.scheduling`). ``python -m src.br_data daemon`` is retired:
it prints a migration notice and exits 0. Readers
(:mod:`backtest.loaders.b3_cotahist_loader`, ``get_br_market_data``) only
read the SQLite file.
"""
