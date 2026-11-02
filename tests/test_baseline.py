import pytest

from museroute import client
from scripts import baseline


def test_payment_error_stops_baseline_item(monkeypatch):
    def stop_for_payment(*_args, **_kwargs):
        raise client.FatalPaymentError("payment required")

    monkeypatch.setattr(baseline, "_call_with_rate_limit_retry", stop_for_payment)

    with pytest.raises(client.FatalPaymentError, match="payment required"):
        baseline._run_item(
            "cheap",
            {"id": "test", "band": "easy", "prompt": "test", "gold": "ok", "check": "exact"},
        )
