"""Unit tests for the Phase-1 OKX Demo perpetual execution surface.

All OKX clients are in-memory fakes.  These tests never load user credentials,
access the network, or submit an exchange request.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.trading import service
from src.trading.connectors.okx import sdk as ox
from src.trading.connectors.okx.swap import (
    SwapInstrument,
    SwapOrderIntent,
    SwapValidationError,
    resolve_contract_size,
)
from src.tools import trading_connector_tool as connector_tools

pytestmark = pytest.mark.unit


def _instrument_row(symbol: str = "BTC-USDT-SWAP") -> dict[str, str]:
    base = symbol.split("-")[0]
    return {
        "instId": symbol,
        "instType": "SWAP",
        "state": "live",
        "ctType": "linear",
        "settleCcy": "USDT",
        "ctVal": "0.01" if base == "BTC" else "0.1",
        "ctValCcy": base,
        "lotSz": "0.01",
        "minSz": "0.01",
        "tickSz": "0.1",
        "maxMktSz": "10000",
        "maxLmtSz": "20000",
    }


def _position(
    *,
    symbol: str = "BTC-USDT-SWAP",
    side: str = "net",
    size: str = "2",
    margin_mode: str = "cross",
) -> dict[str, str]:
    return {
        "instId": symbol,
        "instType": "SWAP",
        "posId": "P1",
        "posSide": side,
        "pos": size,
        "availPos": size,
        "avgPx": "100000",
        "markPx": "100100",
        "notionalUsd": "2000",
        "mgnMode": margin_mode,
        "lever": "10",
    }


class _FakePublic:
    def __init__(self, row: dict[str, str] | None = None) -> None:
        self.row = row or _instrument_row()
        self.calls: list[dict] = []

    def get_instruments(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return {"code": "0", "data": [self.row]}


class _FakeMarket:
    def __init__(self, last: str = "100000") -> None:
        self.last = last

    def get_ticker(self, **kwargs):  # noqa: ANN003
        return {"code": "0", "data": [{"instId": kwargs["instId"], "last": self.last}]}


class _FakeAccount:
    def __init__(
        self,
        *,
        position_mode: str = "net_mode",
        leverage_rows: list[dict[str, str]] | None = None,
        position_snapshots: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.position_mode = position_mode
        self.leverage_rows = leverage_rows or [
            {
                "instId": "BTC-USDT-SWAP",
                "mgnMode": "cross",
                "posSide": "net",
                "lever": "10",
            }
        ]
        self.position_snapshots = position_snapshots or [[]]
        self.position_reads = 0
        self.set_leverage_calls: list[dict] = []

    def get_account_config(self):
        return {
            "code": "0",
            "data": [{"uid": "demo-uid", "acctLv": "2", "posMode": self.position_mode}],
        }

    def get_leverage(self, **kwargs):  # noqa: ANN003
        return {"code": "0", "data": list(self.leverage_rows)}

    def set_leverage(self, **kwargs):  # noqa: ANN003
        self.set_leverage_calls.append(kwargs)
        side = kwargs.get("posSide", "net")
        self.leverage_rows = [
            {
                "instId": kwargs["instId"],
                "mgnMode": kwargs["mgnMode"],
                "posSide": side,
                "lever": kwargs["lever"],
            }
        ]
        return {"code": "0", "data": [self.leverage_rows[0]]}

    def get_positions(self, **kwargs):  # noqa: ANN003
        index = min(self.position_reads, len(self.position_snapshots) - 1)
        self.position_reads += 1
        return {"code": "0", "data": list(self.position_snapshots[index])}


class _FakeTrade:
    def __init__(self, existing_orders: dict[str, dict] | None = None) -> None:
        self.orders = dict(existing_orders or {})
        self.place_calls: list[dict] = []
        self.close_calls: list[dict] = []

    def get_order(self, **kwargs):  # noqa: ANN003
        client_id = str(kwargs.get("clOrdId") or "")
        if not client_id and kwargs.get("ordId"):
            order_id = str(kwargs["ordId"])
            for row in self.orders.values():
                if str(row.get("ordId")) == order_id:
                    return {"code": "0", "data": [row]}
        row = self.orders.get(client_id)
        if row is None:
            return {"code": "51603", "msg": "Order does not exist", "data": []}
        return {"code": "0", "data": [row]}

    def place_order(self, **kwargs):  # noqa: ANN003
        self.place_calls.append(kwargs)
        order_id = f"O{len(self.place_calls)}"
        row = {
            **kwargs,
            "ordId": order_id,
            "clOrdId": kwargs["clOrdId"],
            "sCode": "0",
            "state": "live",
            "fillSz": "0",
        }
        self.orders[kwargs["clOrdId"]] = row
        return {"code": "0", "data": [{"ordId": order_id, "clOrdId": kwargs["clOrdId"], "sCode": "0"}]}

    def close_positions(self, **kwargs):  # noqa: ANN003
        self.close_calls.append(kwargs)
        order_id = f"C{len(self.close_calls)}"
        row = {
            "instId": kwargs["instId"],
            "ordId": order_id,
            "clOrdId": kwargs["clOrdId"],
            "sCode": "0",
            "state": "filled",
        }
        self.orders[kwargs["clOrdId"]] = row
        return {"code": "0", "data": [row]}


def _config(*, profile: str = "paper") -> ox.OKXConfig:
    return ox.OKXConfig(
        api_key="demo-key",
        api_secret="demo-secret",
        passphrase="demo-pass",
        profile=profile,
    )


def _patch_clients(
    monkeypatch,
    *,
    public: _FakePublic | None = None,
    market: _FakeMarket | None = None,
    account: _FakeAccount | None = None,
    trade: _FakeTrade | None = None,
):
    clients = {
        "public": public or _FakePublic(),
        "market": market or _FakeMarket(),
        "account": account or _FakeAccount(),
        "trade": trade or _FakeTrade(),
    }
    monkeypatch.setattr(ox, "_public_client", lambda cfg: clients["public"])
    monkeypatch.setattr(ox, "_market_client", lambda cfg: clients["market"])
    monkeypatch.setattr(ox, "_account_client", lambda cfg: clients["account"])
    monkeypatch.setattr(ox, "_trade_client", lambda cfg: clients["trade"])
    return clients


def _place_kwargs(**overrides):
    values = {
        "symbol": "BTC-USDT-SWAP",
        "side": "buy",
        "size": 500,
        "size_unit": "quote_ccy",
        "margin_mode": "cross",
        "position_side": "net",
        "leverage": 10,
        "client_order_id": "sig-btc-001",
        "order_type": "market",
    }
    values.update(overrides)
    return values


def _intent_kwargs(**overrides):
    values = _place_kwargs(**overrides)
    values["instrument_id"] = values.pop("symbol")
    return values


def test_swap_config_can_narrow_but_not_expand_allowlist() -> None:
    cfg = ox.OKXConfig.from_mapping({"allowed_swap_instruments": ["BTC-USDT-SWAP"]})
    assert cfg.allowed_swap_instruments == ("BTC-USDT-SWAP",)
    with pytest.raises(ox.OKXConfigError, match="supports only"):
        ox.OKXConfig.from_mapping({"allowed_swap_instruments": ["SOL-USDT-SWAP"]})
    with pytest.raises(ox.OKXConfigError, match="from 1 to 10"):
        ox.OKXConfig.from_mapping({"max_swap_leverage": 20})
    with pytest.raises(ox.OKXConfigError, match="timeout"):
        ox.OKXConfig.from_mapping({"timeout": 0})


def test_public_clients_are_unsigned_and_apply_timeout(monkeypatch) -> None:
    for name in ("OKX_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    cfg = ox.OKXConfig(timeout=2.5)
    market = ox._market_client(cfg)
    public = ox._public_client(cfg)
    try:
        assert market.API_KEY == "-1"
        assert public.API_KEY == "-1"
        assert market.timeout.connect == 2.5
        assert public.timeout.read == 2.5
    finally:
        market.close()
        public.close()


def test_proxy_resolution_prefers_config_and_status_redacts_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OKX_PROXY", "http://env-user:env-pass@127.0.0.1:7890")
    assert ox._proxy_for_config(ox.OKXConfig()) == "http://env-user:env-pass@127.0.0.1:7890"
    cfg = ox.OKXConfig(proxy="http://user:pass@127.0.0.1:1080")
    assert ox._proxy_for_config(cfg) == "http://user:pass@127.0.0.1:1080"
    public = ox._public_config(cfg)
    assert public["proxy"] == "***configured***"
    assert "http://user:pass" not in str(public)


def test_instrument_metadata_is_validated_and_exact(monkeypatch) -> None:
    public = _FakePublic()
    _patch_clients(monkeypatch, public=public)
    out = ox.get_swap_instrument("btc-usdt-swap", config=_config())
    assert out["status"] == "ok"
    assert out["instrument"]["contract_value"] == "0.01"
    assert out["instrument"]["lot_size"] == "0.01"
    assert public.calls == [{"instType": "SWAP", "instId": "BTC-USDT-SWAP"}]


def test_quote_and_margin_units_resolve_to_contracts() -> None:
    instrument = SwapInstrument.from_okx(
        _instrument_row(),
        expected_instrument_id="BTC-USDT-SWAP",
    )
    quote_intent = SwapOrderIntent.from_values(**_intent_kwargs())
    quote = resolve_contract_size(quote_intent, instrument, reference_price="100000")
    assert quote.contracts == Decimal("0.5")
    assert quote.estimated_notional_usdt == Decimal("500.000")

    margin_intent = SwapOrderIntent.from_values(
        **_intent_kwargs(size=50, size_unit="margin", client_order_id="sig-btc-002")
    )
    margin = resolve_contract_size(margin_intent, instrument, reference_price="100000")
    assert margin.contracts == Decimal("0.5")
    assert margin.estimated_margin_usdt == Decimal("50.000")


def test_explicit_contract_size_must_match_lot_size() -> None:
    instrument = SwapInstrument.from_okx(
        _instrument_row(),
        expected_instrument_id="BTC-USDT-SWAP",
    )
    intent = SwapOrderIntent.from_values(
        **_intent_kwargs(size="0.005", size_unit="contracts")
    )
    with pytest.raises(SwapValidationError, match="lotSz"):
        resolve_contract_size(intent, instrument, reference_price="100000")


def test_limit_price_must_match_tick_and_leverage_must_be_at_least_one() -> None:
    instrument = SwapInstrument.from_okx(
        _instrument_row(),
        expected_instrument_id="BTC-USDT-SWAP",
    )
    intent = SwapOrderIntent.from_values(
        **_intent_kwargs(order_type="limit", limit_price="100000.05")
    )
    with pytest.raises(SwapValidationError, match="tickSz"):
        resolve_contract_size(intent, instrument, reference_price=intent.limit_price)
    with pytest.raises(SwapValidationError, match="at least 1"):
        SwapOrderIntent.from_values(**_intent_kwargs(leverage="0.5"))


def test_place_swap_order_converts_size_attaches_tp_sl_and_verifies(monkeypatch) -> None:
    clients = _patch_clients(monkeypatch)
    out = ox.place_swap_order(
        _config(),
        **_place_kwargs(take_profit=110000, stop_loss=95000),
    )
    assert out["status"] == "ok"
    assert out["sizing"]["contracts"] == "0.5"
    assert out["verification"]["status"] == "verified"
    assert out["retry_safe"] is False
    assert len(clients["trade"].place_calls) == 1
    request = clients["trade"].place_calls[0]
    assert request["tdMode"] == "cross"
    assert request["sz"] == "0.5"
    assert "posSide" not in request
    assert request["clOrdId"] == "sig-btc-001"
    attached = request["attachAlgoOrds"][0]
    assert attached["tpTriggerPx"] == "110000"
    assert attached["slTriggerPx"] == "95000"
    assert attached["tpOrdPx"] == attached["slOrdPx"] == "-1"


def test_hedge_reduce_order_is_reduce_only_and_cannot_exceed_position(monkeypatch) -> None:
    account = _FakeAccount(
        position_mode="long_short_mode",
        leverage_rows=[
            {
                "instId": "BTC-USDT-SWAP",
                "mgnMode": "isolated",
                "posSide": "long",
                "lever": "10",
            }
        ],
        position_snapshots=[[_position(side="long", margin_mode="isolated")]],
    )
    clients = _patch_clients(monkeypatch, account=account)
    out = ox.place_swap_order(
        _config(),
        **_place_kwargs(
            side="sell",
            size=1,
            size_unit="contracts",
            margin_mode="isolated",
            position_side="long",
            reduce_only=True,
        ),
    )
    assert out["status"] == "ok"
    request = clients["trade"].place_calls[0]
    assert request["posSide"] == "long"
    assert request["reduceOnly"] == "true"

    too_large = ox.place_swap_order(
        _config(),
        **_place_kwargs(
            side="sell",
            size=3,
            size_unit="contracts",
            margin_mode="isolated",
            position_side="long",
            reduce_only=True,
            client_order_id="sig-btc-too-large",
        ),
    )
    assert too_large["status"] == "error"
    assert "exceeds position size" in too_large["error"]
    assert len(clients["trade"].place_calls) == 1


def test_existing_client_order_id_is_idempotent_and_not_resent(monkeypatch) -> None:
    existing = {
        "instId": "BTC-USDT-SWAP",
        "ordId": "EXISTING",
        "clOrdId": "sig-btc-001",
        "state": "live",
        "side": "buy",
        "sz": "0.5",
    }
    trade = _FakeTrade(existing_orders={"sig-btc-001": existing})
    clients = _patch_clients(monkeypatch, trade=trade)
    out = ox.place_swap_order(_config(), **_place_kwargs())
    assert out["status"] == "ok"
    assert out["idempotent_replay"] is True
    assert out["order_id"] == "EXISTING"
    assert clients["trade"].place_calls == []


def test_live_swap_write_is_rejected_before_any_client_call(monkeypatch) -> None:
    def _unexpected(_cfg):
        raise AssertionError("no SDK client should be constructed")

    monkeypatch.setattr(ox, "_public_client", _unexpected)
    monkeypatch.setattr(ox, "_account_client", _unexpected)
    monkeypatch.setattr(ox, "_trade_client", _unexpected)
    out = ox.place_swap_order(_config(profile="live"), **_place_kwargs())
    assert out["status"] == "error"
    assert "Demo only" in out["error"]
    assert out["retry_safe"] is True


def test_generic_spot_order_path_refuses_swap_symbol(monkeypatch) -> None:
    monkeypatch.setattr(
        ox,
        "_trade_client",
        lambda cfg: (_ for _ in ()).throw(AssertionError("spot path must not send a swap")),
    )
    out = ox.place_order(
        _config(),
        symbol="BTC-USDT-SWAP",
        side="buy",
        quantity=1,
    )
    assert out["status"] == "error"
    assert "place_swap_order" in out["error"]


def test_transport_error_after_submit_is_unknown_and_not_retryable(monkeypatch) -> None:
    class _UncertainTrade(_FakeTrade):
        def place_order(self, **kwargs):  # noqa: ANN003
            self.place_calls.append(kwargs)
            raise TimeoutError("response timed out")

    trade = _UncertainTrade()
    _patch_clients(monkeypatch, trade=trade)
    out = ox.place_swap_order(_config(), **_place_kwargs())
    assert out["status"] == "error"
    assert out["write_state"] == "unknown_needs_reconcile"
    assert out["retry_safe"] is False
    assert out["client_order_id"] == "sig-btc-001"
    assert len(trade.place_calls) == 1


def test_set_swap_leverage_writes_then_reads_back(monkeypatch) -> None:
    account = _FakeAccount(
        leverage_rows=[
            {
                "instId": "BTC-USDT-SWAP",
                "mgnMode": "cross",
                "posSide": "net",
                "lever": "5",
            }
        ]
    )
    _patch_clients(monkeypatch, account=account)
    out = ox.set_swap_leverage(
        _config(),
        symbol="BTC-USDT-SWAP",
        leverage=10,
        margin_mode="cross",
        position_side="net",
    )
    assert out["status"] == "ok"
    assert out["verification"]["status"] == "verified"
    assert account.set_leverage_calls == [
        {"instId": "BTC-USDT-SWAP", "lever": "10", "mgnMode": "cross"}
    ]


def test_full_close_uses_close_endpoint_and_verifies_zero_position(monkeypatch) -> None:
    account = _FakeAccount(
        position_mode="long_short_mode",
        leverage_rows=[
            {
                "instId": "BTC-USDT-SWAP",
                "mgnMode": "isolated",
                "posSide": "long",
                "lever": "10",
            }
        ],
        position_snapshots=[[_position(side="long", margin_mode="isolated")], []],
    )
    clients = _patch_clients(monkeypatch, account=account)
    out = ox.close_swap_position(
        _config(),
        symbol="BTC-USDT-SWAP",
        margin_mode="isolated",
        position_side="long",
        leverage=10,
        client_order_id="close-btc-long-001",
        auto_cancel=False,
    )
    assert out["status"] == "ok"
    assert out["verification"] == {"status": "verified", "remaining_contracts": "0"}
    assert clients["trade"].close_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "mgnMode": "isolated",
            "autoCxl": "false",
            "clOrdId": "close-btc-long-001",
            "posSide": "long",
        }
    ]


def test_invalid_tp_sl_direction_is_blocked_before_write(monkeypatch) -> None:
    clients = _patch_clients(monkeypatch)
    out = ox.place_swap_order(
        _config(),
        **_place_kwargs(stop_loss=101000),
    )
    assert out["status"] == "error"
    assert "long stop_loss must be below" in out["error"]
    assert clients["trade"].place_calls == []


def test_service_exposes_demo_swap_write_but_not_live(monkeypatch) -> None:
    monkeypatch.setattr(ox, "build_config", lambda *args, **kwargs: _config())
    monkeypatch.setattr(ox, "place_swap_order", lambda config, **kwargs: {"status": "ok", "seen": kwargs})
    paper = service.place_okx_swap_order(
        "BTC-USDT-SWAP",
        "okx-paper-trade",
        side="buy",
        size=100,
        size_unit="quote_ccy",
        margin_mode="cross",
        position_side="net",
        leverage=10,
        client_order_id="service-paper-001",
    )
    assert paper["status"] == "ok"
    assert paper["profile_id"] == "okx-paper-trade"

    live = service.place_okx_swap_order(
        "BTC-USDT-SWAP",
        "okx-live-trade",
        side="buy",
        size=100,
        size_unit="quote_ccy",
        margin_mode="cross",
        position_side="net",
        leverage=10,
        client_order_id="service-live-001",
    )
    assert live["status"] == "error"
    assert "does not support derivatives.orders.place" in live["error"]


def test_agent_swap_tool_requires_explicit_derivative_fields(monkeypatch) -> None:
    seen: dict = {}

    def _fake(symbol, connection, **kwargs):  # noqa: ANN001
        seen.update({"symbol": symbol, "connection": connection, **kwargs})
        return {"status": "ok"}

    monkeypatch.setattr(connector_tools, "place_okx_swap_order", _fake)
    payload = json.loads(
        connector_tools.OKXSwapPlaceOrderTool().execute(
            connection="okx-paper-trade",
            symbol="BTC-USDT-SWAP",
            side="buy",
            size=100,
            size_unit="quote_ccy",
            margin_mode="cross",
            position_side="net",
            leverage=10,
            client_order_id="tool-order-001",
            stop_loss=95000,
        )
    )
    assert payload["status"] == "ok"
    assert seen["size_unit"] == "quote_ccy"
    assert seen["margin_mode"] == "cross"
    assert seen["position_side"] == "net"
    assert seen["leverage"] == 10
    assert "host" not in connector_tools.OKXSwapPlaceOrderTool.parameters["properties"]
