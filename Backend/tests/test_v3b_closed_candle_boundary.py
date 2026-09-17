"""The 5m close boundary must not manufacture or lose a confirmed candle."""
from datetime import datetime, timezone

import pandas as pd

from strategies import shared


def test_only_the_forming_5m_bar_is_excluded_at_the_close_boundary(monkeypatch):
    frame = pd.DataFrame({"Open": [1.1, 1.2], "High": [1.2, 1.3],
                          "Low": [1.0, 1.1], "Close": [1.15, 1.25]},
                         index=pd.to_datetime(["2026-09-17T10:00:00Z", "2026-09-17T10:05:00Z"]))

    class BeforeClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 17, 10, 9, 59, tzinfo=timezone.utc)

    class AtClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 17, 10, 10, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(shared, "datetime", BeforeClose)
    assert list(shared.remove_current_forming_candle(frame, 5).index) == [frame.index[0]]
    monkeypatch.setattr(shared, "datetime", AtClose)
    assert list(shared.remove_current_forming_candle(frame, 5).index) == list(frame.index)
