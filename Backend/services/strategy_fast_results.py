"""Compatibility with the former browser's ordered FAST result aggregation."""
from services.strategy_simulator import simulation_metrics

STAGES = ('trend', 'structure', 'break_validation', 'confirmation', 'entry', 'stop_loss', 'tp1', 'tp2', 'risk')


def aggregate_results(results, starting_balance):
    trades = [trade for result in results for trade in result['trades']]
    metrics = simulation_metrics(starting_balance, trades)
    curve = metrics.pop('equity_curve')
    # Match JavaScript reduce, including its ordered binary64 additions.
    gross_profit = gross_loss = r_sum = 0.0
    r_count = 0
    for trade in trades:
        if trade.get('resolved') is not True:
            continue
        pnl = float(trade.get('pnl_dollars') or 0)
        if pnl > 0:
            gross_profit += pnl
        elif pnl < 0:
            gross_loss += pnl
        if trade.get('r') is not None:
            r_sum += float(trade['r'])
            r_count += 1
    metrics['profit_factor'] = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    metrics['average_r'] = r_sum / r_count if r_count else 0
    first = results[0]['diagnostics']
    diagnostics = {key: sum(r['diagnostics'].get(key, 0) for r in results)
                   for key in ('candles_analyzed', 'evaluations', 'signals_emitted', 'trades_opened')}
    setups, no_setup = {}, {}
    for result in results:
        for reason, count in result['diagnostics'].get('no_setup_reasons', {}).items():
            no_setup[reason] = no_setup.get(reason, 0) + count
        for detail in result['diagnostics'].get('setup_details', []):
            identity = detail.get('setup_id')
            if not identity:
                continue
            current = setups.setdefault(identity, dict(setup_id=identity, passed_stages=set(), last_state=None, last_reason=None, signaled=False))
            current['passed_stages'].update(detail.get('passed_stages', []))
            for key in ('last_state', 'last_reason'):
                if detail.get(key) is not None:
                    current[key] = detail[key]
            current['signaled'] = current['signaled'] or bool(detail.get('signaled'))
    reasons = {}
    blocked = waiting = 0
    for setup in setups.values():
        setup['passed_stages'] = sorted(setup['passed_stages'])
        if setup['signaled']:
            continue
        blocked += setup['last_state'] == 'BLOCKED'
        waiting += setup['last_state'] == 'WAITING'
        reason = setup['last_reason'] or 'NO_VALID_ENTRY'
        reasons[reason] = reasons.get(reason, 0) + 1
    diagnostics.update(
        warmup_candles=first.get('warmup_candles', 0), history_start=first.get('history_start'),
        setups_detected=len(setups), resolved_trades=metrics['total_resolved_trades'],
        open_trades_at_end=results[-1]['diagnostics'].get('open_trades_at_end', 0),
        blocked_setups=blocked, waiting_setups=waiting,
        stage_pass_counts={stage: sum(stage in setup['passed_stages'] for setup in setups.values()) for stage in STAGES},
        rejection_reasons=reasons, no_setup_reasons=no_setup, setup_details=list(setups.values()),
        chunk_count=len(results),
    )
    result = dict(results[-1])
    result.pop('replay', None)
    result.update(starting_balance=starting_balance, trades=trades, metrics=metrics, equity_curve=curve, diagnostics=diagnostics, batch_chunks=len(results))
    return result


class DiskResults:
    """Reiterable window results; only the current decoded chunk resides in RAM."""
    def __init__(self, parent=None):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        self._temporary = TemporaryDirectory(prefix='chunks-', dir=parent)
        self.directory = Path(self._temporary.name)
        self.count = 0

    def append(self, result):
        from services.strategy_fast_jobs import write_json
        write_json(self.directory / f'{self.count}.json', result)
        self.count += 1

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        from services.strategy_fast_jobs import read_json
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        return read_json(self.directory / f'{index}.json')

    def __iter__(self):
        for index in range(self.count):
            yield self[index]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._temporary.cleanup()
