import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import IndicatorCandle, IndicatorEvent, IndicatorStreamState
from services import indicator_event_stream_service as stream
from services.paper_v3b_bridge import build_paper_v3b_candidate


class _ClosedStrict:
    @staticmethod
    def closed_frame(frame, minutes):
        assert minutes == 5
        return frame.iloc[:-1].copy()

    @staticmethod
    def point_size(symbol):
        assert symbol == "EURUSD"
        return 0.00001


def _analysis(frame, **_kwargs):
    events = []
    if len(frame) >= 6:
        events.append({
            "event_type": "BOS",
            "direction": "BEARISH",
            "timestamp": frame.index[-2].isoformat(),
            "close": float(frame.iloc[-2]["Close"]),
            "broken_swing_timestamp": frame.index[-4].isoformat(),
            "broken_level": 1.15835,
            "event_invalidation_swing": {
                "type": "HIGH",
                "price": 1.15879,
                "swing_time": frame.index[-4].isoformat(),
                "confirmation_time": frame.index[-2].isoformat(),
            },
        })
    return {
        "bias": "BEARISH",
        "events": events,
        "current_structure": None,
        "fib_levels": [],
        "swings": [],
        "config": {},
    }


def test_closed_candle_refresh_advances_stream_and_preserves_identity_on_restart(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'v3b-authority.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    index = pd.date_range("2026-09-14T00:00:00Z", periods=7, freq="5min")
    frame = pd.DataFrame(
        {
            "Open": [1.1590, 1.1589, 1.1588, 1.1587, 1.15851, 1.15832, 1.15816],
            "High": [1.1591, 1.1590, 1.1589, 1.15879, 1.15853, 1.15837, 1.15822],
            "Low": [1.1588, 1.1587, 1.1586, 1.1585, 1.15832, 1.15811, 1.15810],
            "Close": [1.1589, 1.1588, 1.1587, 1.1586, 1.15833, 1.15816, 1.15818],
        },
        index=index,
    )
    stream.initialize_indicator_stream(
        frame.iloc[:4], "EURUSD", "5m", 0.00001,
        analyzer=_analysis, session_factory=Session,
    )

    def updater(source, symbol, timeframe, point_size, **_kwargs):
        return stream.get_authoritative_structure(
            source, symbol, timeframe, point_size,
            analyzer=_analysis, session_factory=Session,
        )

    first = build_paper_v3b_candidate(
        "EURUSD", frame,
        strict_trader_module=_ClosedStrict,
        authoritative_updater=updater,
    )
    assert first["paper_entry_ready"] is True
    assert first["five_m_break_time"] == "2026-09-14T00:20:00+00:00"
    assert first["five_m_closed_candle_time"] == "2026-09-14T00:30:00+00:00"

    with Session() as session:
        state = session.query(IndicatorStreamState).filter_by(
            symbol="EURUSD", timeframe="5m"
        ).one()
        assert pd.Timestamp(state.last_processed_candle, tz="UTC") == index[-2]
        assert session.query(IndicatorCandle).count() == 6
        assert session.query(IndicatorEvent).count() == 1
        event_id = session.query(IndicatorEvent.event_id).scalar()
        assert not session.query(IndicatorCandle).filter_by(
            candle_timestamp=index[-1].to_pydatetime().replace(tzinfo=None)
        ).count()

    restarted = build_paper_v3b_candidate(
        "EURUSD", frame,
        strict_trader_module=_ClosedStrict,
        authoritative_updater=updater,
    )
    assert restarted["source_indicator_event_id"] == event_id
    assert restarted["m5_confirmation_id"] == first["m5_confirmation_id"]
    with Session() as session:
        assert session.query(IndicatorEvent).count() == 1
        assert session.query(IndicatorEvent.event_id).scalar() == event_id
