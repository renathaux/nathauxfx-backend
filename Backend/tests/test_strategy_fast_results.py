from services.strategy_fast_results import aggregate_results


def test_cross_boundary_diagnostics_merge_once_and_keep_compounded_equity():
    def diagnostics(details, candles=10):
        return dict(candles_analyzed=candles, warmup_candles=7, history_start='2025-01-01',
                    evaluations=candles, signals_emitted=1, trades_opened=1,
                    no_setup_reasons={'NO_STRUCTURE': 2}, setup_details=details, open_trades_at_end=0)
    waiting = dict(setup_id='across-boundary', passed_stages=['trend', 'structure'],
                   last_state='WAITING', last_reason='CONFIRMATION_PENDING', signaled=False)
    signal = dict(setup_id='across-boundary', passed_stages=['confirmation', 'entry', 'risk'],
                  last_state='PASSED', last_reason=None, signaled=True)
    blocked = dict(setup_id='filtered', passed_stages=['trend'], last_state='BLOCKED',
                   last_reason='SL_DISTANCE_OUT_OF_RANGE', signaled=False)
    chunks = [
        dict(trades=[dict(resolved=True, pnl_dollars=100, r=1, outcome='TP2')], diagnostics=diagnostics([waiting])),
        dict(trades=[dict(resolved=True, pnl_dollars=-101, r=-1, outcome='SL')], diagnostics=diagnostics([signal, blocked])),
    ]
    result = aggregate_results(chunks, 10000)
    assert result['equity_curve'] == [dict(trade=0, balance=10000), dict(trade=1, balance=10100), dict(trade=2, balance=9999)]
    assert result['metrics']['net_pl'] == -1
    assert result['metrics']['win_rate'] == 50
    assert result['metrics']['max_drawdown_dollars'] == 101
    d = result['diagnostics']
    assert d['setups_detected'] == 2 and d['candles_analyzed'] == 20
    assert d['rejection_reasons'] == {'SL_DISTANCE_OUT_OF_RANGE': 1}
    assert d['waiting_setups'] == 0 and d['blocked_setups'] == 1
    assert d['stage_pass_counts']['trend'] == 2 and d['stage_pass_counts']['confirmation'] == 1
    assert d['warmup_candles'] == 7 and d['no_setup_reasons'] == {'NO_STRUCTURE': 4}
    # Aggregation must not mutate continuation/source diagnostics.
    assert waiting['passed_stages'] == ['trend', 'structure'] and waiting['signaled'] is False
