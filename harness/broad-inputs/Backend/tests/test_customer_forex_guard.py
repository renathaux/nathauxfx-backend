from services.customer_forex_guard import _bearer, _sensitive


def test_owner_forex_mutation_paths_are_sensitive():
    assert _sensitive('/paper-auto-toggle', 'POST')
    assert _sensitive('/live-auto-toggle', 'POST')
    assert _sensitive('/execute-live-order', 'POST')
    assert _sensitive('/market-data-source', 'POST')
    assert _sensitive('/connect-ctrader', 'POST')
    assert _sensitive('/close-live-trade', 'POST')
    assert _sensitive('/modify-live-position-levels', 'POST')
    assert _sensitive('/ctrader/accounts/active', 'POST')
    assert _sensitive('/settings/news-trading-mode', 'PUT')
    assert _sensitive('/strategy/settings/reset', 'POST')


def test_signal_and_panel_reads_remain_available():
    assert not _sensitive('/panel-data', 'GET')
    assert not _sensitive('/dashboard-feed', 'GET')
    assert not _sensitive('/chart/live-ticks', 'GET')
    assert not _sensitive('/user/deriv/binary/v5/signal', 'GET')


def test_bearer_parser_requires_bearer_scheme():
    assert _bearer({'authorization': 'Bearer abc123'}) == 'abc123'
    assert _bearer({'authorization': 'bearer xyz'}) == 'xyz'
    assert _bearer({'authorization': 'Basic abc'}) == ''
    assert _bearer({}) == ''
