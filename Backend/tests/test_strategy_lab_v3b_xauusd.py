import pytest

from services.strategy_lab import v3b_xauusd_frozen_candidate as gold


def test_gold_v3b_constants_are_frozen_to_validated_rules():
    assert gold.POINT_SIZE == pytest.approx(0.01)
    assert gold.MIN_BOS_BODY_RATIO == pytest.approx(0.50)
    assert gold.SL_BUFFER_POINTS == 50
    assert gold.MIN_SL_POINTS == 100
    assert gold.TARGET_RR == pytest.approx(1.90)
    assert gold.PROTECTION_TRIGGER_TP2_FRACTION == pytest.approx(0.70)
    assert gold.PROTECTED_STOP_TP2_FRACTION == pytest.approx(0.60)


def test_gold_v3b_buy_levels_use_gold_point_math():
    levels = gold._fixed_levels(
        "BUY",
        entry=4400.00,
        invalidation={"type": "LOW", "price": 4398.00},
    )

    assert levels["ok"] is True
    assert levels["stop_loss"] == pytest.approx(4397.50)
    assert levels["tp2"] == pytest.approx(4404.75)
    assert levels["tp1"] == pytest.approx(4403.32)
    assert levels["protected_sl_price"] == pytest.approx(4402.85)
    assert levels["risk_reward_ratio"] == pytest.approx(1.90)


def test_gold_v3b_sell_levels_use_gold_point_math():
    levels = gold._fixed_levels(
        "SELL",
        entry=4400.00,
        invalidation={"type": "HIGH", "price": 4402.00},
    )

    assert levels["ok"] is True
    assert levels["stop_loss"] == pytest.approx(4402.50)
    assert levels["tp2"] == pytest.approx(4395.25)
    assert levels["tp1"] == pytest.approx(4396.68)
    assert levels["protected_sl_price"] == pytest.approx(4397.15)


def test_gold_v3b_rejects_stop_under_one_dollar():
    levels = gold._fixed_levels(
        "BUY",
        entry=4400.00,
        invalidation={"type": "LOW", "price": 4399.60},
    )

    assert levels == {"ok": False, "reason": "WAIT_SL_TOO_SMALL"}


def test_gold_identity_is_not_eurusd_identity():
    event = {
        "timestamp": "2026-09-01T08:00:00+00:00",
        "event_type": "BOS",
        "direction": "BULLISH",
        "broken_level": 4400.0,
    }
    assert gold._identity(event).startswith("lab_xauusd_5m_bos_")
