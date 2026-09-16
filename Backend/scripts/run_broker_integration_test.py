#!/usr/bin/env python3
"""Explicit operator entry point; no scheduler or HTTP route."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--test-id', required=True)
    parser.add_argument('--symbol', required=True, choices=['EURUSD'])
    parser.add_argument('--confirm-demo-broker-test', required=True, action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--recover', action='store_true')
    args = parser.parse_args(argv)
    from db import SessionLocal
    from services.broker_integration_test_adapter import CTraderTestAdapter
    from services.broker_integration_test_service import BrokerIntegrationTestService, TestRequest
    from services.broker_integration_test_errors import safe_error_code
    request = TestRequest(args.account_id, args.test_id, args.symbol, args.confirm_demo_broker_test)
    service = BrokerIntegrationTestService(SessionLocal, CTraderTestAdapter())
    try:
        result = service.preflight(request) if args.preflight else service.run(request, recover=args.recover)
        print(json.dumps(result, sort_keys=True))
        return 0 if args.preflight or result['state'] == 'CLOSED' else 2
    except Exception as exc:
        print(json.dumps({'state':'BLOCKED','error_code':safe_error_code(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
