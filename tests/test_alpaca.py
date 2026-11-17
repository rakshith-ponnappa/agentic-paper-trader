# ruff: noqa: E402
import pytest

pytest.importorskip("pandas")
pytest.importorskip("yfinance")

from unittest.mock import MagicMock

from autotrader import Alpaca


def _response(status: int, body: str):
    r = MagicMock(status_code=status, text=body, content=body.encode())
    r.json.return_value = {"id": "o1", "status": "filled"}
    return r


def test_duplicate_client_order_id_returns_existing_order():
    a = Alpaca("k", "s")
    a.session.request = MagicMock(side_effect=[
        _response(422, '{"message":"client_order_id must be unique"}'),
        _response(200, '{"id":"o1"}'),
    ])
    out = a._call("POST", "/v2/orders", json={"client_order_id": "ta-x"})
    assert out["id"] == "o1"
    assert "by_client_order_id?client_order_id=ta-x" in a.session.request.call_args_list[1].args[1]


def test_retry_adapter_is_mounted():
    adapter = Alpaca("k", "s").session.get_adapter("https://paper-api.alpaca.markets")
    assert adapter.max_retries.total == 4 and adapter.max_retries.allowed_methods is None
