"""Fresh isolated FAST worker measurement and exact frozen-result comparison.

Example (PYTHONPATH=Backend):
  python Backend/benchmarks/verify_fast_memory.py --definition gold831.json \
    --reference frozen-result.json --symbol XAUUSD --start 2021-09-25T00:00:00Z \
    --end 2026-09-24T00:00:00Z --history /canonical/replay-data --output report.json

Omit --history to exercise pinned remote history and its disk cache. Measure a
fresh empty SIMULATOR_HISTORY_CACHE_DIR for download-cold performance. Reference
JSON is loaded only AFTER recording RSS, so it cannot inflate worker memory.
"""
import argparse
import gc
import json
from pathlib import Path
import resource
import sys
import time

from fast_backtest_worker import initialize_worker_namespace


def peak_rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--symbol', choices=['XAUUSD', 'EURUSD'], required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    initialize_worker_namespace()
    from services.strategy_fast_worker import execute
    from services.strategy_fast_jobs import write_json

    payload = dict(strategy_id='offline-memory-parity', strategy_name='Frozen offline fixture',
                   strategy_definition=json.loads(args.definition.read_text()), symbol=args.symbol,
                   start=args.start, end=args.end, starting_balance=10000, account_scope='OFFLINE_PARITY')
    result = execute(payload, history_dir=args.history)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result_path = args.output.with_suffix('.result.json')
    started = time.perf_counter()
    write_json(result_path, result)
    report = dict(performance=result['performance'], trades=len(result['trades']),
                  result_bytes=result_path.stat().st_size,
                  serialization_seconds=time.perf_counter()-started,
                  peak_including_serialization_bytes=peak_rss())
    # Release the measured result too before recording end-of-job highwater.
    del result
    gc.collect()
    report['peak_through_cleanup_bytes'] = peak_rss()
    measured = json.loads(result_path.read_text())
    reference = json.loads(args.reference.read_text())
    report['parity'] = {key: measured[key] == reference[key]
                        for key in ('trades', 'metrics', 'diagnostics', 'equity_curve', 'continuation')}
    write_json(args.output, report)
    print(json.dumps(report, indent=2))
    if not all(report['parity'].values()):
        raise SystemExit('Exact parity failed')


if __name__ == '__main__':
    main()
