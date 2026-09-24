"""Compare an owned FAST worker against frozen profile_fast.py output.

Run in a fresh process so worker peak RSS is meaningful. The baseline must have
been produced with the original source tree, not regenerated after optimization.
Example: PYTHONPATH=Backend python Backend/benchmarks/verify_fast.py \
  --baseline /path/to/frozen --history /path/to/replay-data --output /path/to/report.json
"""
import argparse
import json
from pathlib import Path
import time

from services.strategy_fast_results import aggregate_results
from services.strategy_fast_worker import execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path)
    args = parser.parse_args()
    timing = json.loads((args.baseline / 'timing.json').read_text())
    definition = json.loads((args.baseline / 'definition.json').read_text())
    payload = dict(strategy_id='offline-parity', strategy_name='Definition from frozen baseline',
                   strategy_definition=definition, symbol=timing['symbol'], start=timing['start'],
                   end=timing['end'], starting_balance=10000, account_scope='OFFLINE_PARITY')
    result = execute(payload, history_dir=args.history, cache_dir=args.cache_dir)
    serialization_start = time.perf_counter()
    serialized = json.dumps(result)
    serialization_seconds = time.perf_counter() - serialization_start
    chunks = [json.loads(path.read_text()) for path in sorted(args.baseline.glob('chunk-*.json'))]
    expected = aggregate_results(chunks, 10000)
    parity = {key: result[key] == expected[key] for key in ('trades', 'metrics', 'equity_curve', 'diagnostics')}
    report = dict(baseline=str(args.baseline.resolve()), symbol=timing['symbol'], definition=definition,
                  old_seconds=timing['wall_seconds'], performance=result['performance'],
                  speedup=timing['wall_seconds']/result['performance']['total_seconds'],
                  parity=parity, trades=len(result['trades']), result_bytes=len(serialized.encode()),
                  result_serialization_seconds=serialization_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    args.output.with_suffix('.result.json').write_text(serialized)
    print(json.dumps(report, indent=2))
    if not all(parity.values()):
        raise SystemExit('PARITY FAILED: ' + ', '.join(key for key, passed in parity.items() if not passed))


if __name__ == '__main__':
    main()
