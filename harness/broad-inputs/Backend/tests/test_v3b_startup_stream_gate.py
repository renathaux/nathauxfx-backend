from services.v3b_startup_stream_gate import startup_stream_gate


def test_v3b_uses_only_ready_authoritative_5m_streams_at_startup():
    attempts = []

    def initialize(symbol, timeframe):
        attempts.append((symbol, timeframe))
        if timeframe == "15m":
            raise RuntimeError("15m reconciliation required")
        return {"symbol": symbol, "timeframe": timeframe}

    result = startup_stream_gate(initialize, v3b_enabled=True)
    assert result["ready"] is True
    assert result["streams_ready"] == 4
    assert len(result["ancillary_failures"]) == 2
    assert ("EURUSD", "5m") in attempts and ("XAUUSD", "5m") in attempts


def test_v3b_remains_fenced_if_either_authoritative_5m_stream_fails():
    def initialize(symbol, timeframe):
        if symbol == "EURUSD" and timeframe == "5m":
            raise RuntimeError("corrected 5m candle not reconciled")
        return {"symbol": symbol, "timeframe": timeframe}

    result = startup_stream_gate(initialize, v3b_enabled=True)
    assert result["ready"] is False
    assert "corrected 5m candle not reconciled" in result["reason"]


def test_legacy_mode_keeps_all_six_streams_required():
    def initialize(symbol, timeframe):
        if timeframe == "15m":
            raise RuntimeError("15m reconciliation required")
        return {"symbol": symbol, "timeframe": timeframe}

    result = startup_stream_gate(initialize, v3b_enabled=False)
    assert result["ready"] is False
    assert "15m reconciliation required" in result["reason"]
