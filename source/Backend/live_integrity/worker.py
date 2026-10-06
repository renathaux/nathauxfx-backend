"""Fresh-process verification entrypoint: no application/trading bootstrap."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class DenyExecutionImports:
    def find_spec(self,fullname,*args):
        if fullname.split('.')[0] in {'services','api','ctrader_connector','db','models','stream_generations'}:
            raise ImportError('DIAGNOSTIC_EXECUTION_IMPORT_FORBIDDEN')

sys.meta_path.insert(0,DenyExecutionImports())

import json
import os
import time
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from live_integrity.read_store import read_only,single_flight,Busy
from live_integrity.auth import read_admin,AccessDenied
from live_integrity.build_identity import capture
from live_integrity.verifier import verify

REASONS = frozenset('''BUILD_IDENTITY_UNVERIFIED STRATEGY_SAVED_ROW_MISSING STRATEGY_IDENTITY_MISSING
STRATEGY_DEFINITION_NOT_CANONICAL STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED LIVE_AUTO_DISABLED
WAIT_STUDIO_LIVE_DISABLED WAIT_STUDIO_LIVE_STRATEGY_MISMATCH WAIT_STUDIO_EVALUATOR WAIT_STUDIO_SYMBOL_DISABLED
WAIT_STUDIO_HISTORY_UNAVAILABLE WAIT_STUDIO_GENERATION_HISTORICAL STRATEGY_STUDIO_RECONCILIATION_UNRESOLVED
CTRADER_ACCOUNT_NOT_SELECTED CTRADER_ACCOUNT_IDENTITY_INVALID BROKER_AUTH_UNAVAILABLE
STUDIO_MARKET_SNAPSHOT_UNAVAILABLE STUDIO_MARKET_SNAPSHOT_STALE STUDIO_GENERATION_UNAVAILABLE
EXISTING_EXPOSURE_REQUIRES_EXECUTION_STATE RUNTIME_IDENTITY_CHANGED BROKER_ACCOUNT_STATE_CHANGED
BROKER_EXECUTABLE_SIZING_BLOCKED STRATEGY_OWNER_MISMATCH STRATEGY_VERSION_CHANGED STRATEGY_PLAN_CHANGED
BROKER_METADATA_CHANGED BROKER_METADATA_STALE BROKER_QUOTE_STALE BROKER_ENTRY_QUOTE_CHANGED
BROKER_DISTANCE_VIOLATION BROKER_PRICE_PRECISION_VIOLATION BROKER_VOLUME_STEP_VIOLATION
BROKER_METADATA_UNVERIFIED BROKER_METADATA_ACCOUNT_MISMATCH BROKER_METADATA_SYMBOL_MISMATCH
BROKER_QUOTE_IDENTITY_MISMATCH STUDIO_SETUP_NOT_ELIGIBLE STUDIO_SETUP_STATE_CHANGED
STUDIO_GENERATION_CHANGED STUDIO_CONFIRMATION_NOT_DURABLE EXECUTION_AUTHORITY_CHANGED
STRATEGY_STUDIO_LIVE_OWNER_AMBIGUOUS BROKER_EXPOSURE_UNVERIFIED
STUDIO_SETUP_GENERATION_INVALID RISK_SETTINGS_READ_ONLY_UNAVAILABLE'''.split())


def main():
    response = dict(decision='WOULD_BLOCK',REAL_ORDER_DISPATCH_AVAILABLE=False,block_reasons=['DIAGNOSTIC_UNAVAILABLE'])
    status = 503
    engine = None
    try:
        if os.getenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED')!='1':
            return dict(status=404,body=response)
        payload = json.loads(sys.stdin.buffer.readline(8193))
        deadline = float(payload['deadline'])
        if time.monotonic()>=deadline:
            raise TimeoutError()
        url = os.environ['DATABASE_URL']
        if url.startswith('postgres://'):
            url=url.replace('postgres://','postgresql://',1)
        engine = create_engine(url,poolclass=NullPool,connect_args={'connect_timeout':2,'options':'-c default_transaction_read_only=on'})
        with read_only(engine) as conn:
            owner = read_admin(conn,payload['token'],payload['csrf'])
            with single_flight(conn):
                build = capture()
                response['build']=build
                response = dict(verify(conn,owner,payload['strategy_id'],payload['symbol'],payload['runtime'],deadline),build=build)
                status = 200
    except AccessDenied as exc:
        status=exc.status; response['block_reasons']=['ACCESS_DENIED']
    except Busy:
        status=409; response['block_reasons']=['DIAGNOSTIC_BUSY']
    except TimeoutError:
        status=504; response['block_reasons']=['DIAGNOSTIC_TIMEOUT']
    except ValueError as exc:
        reason=str(exc)
        response['block_reasons']=[reason if reason in REASONS else 'DIAGNOSTIC_UNAVAILABLE']
        status=200 if reason in REASONS else 503
    except Exception:
        pass
    finally:
        if engine is not None:
            engine.dispose()
    return dict(status=status,body=response)


if __name__=='__main__':
    sys.stdout.write(json.dumps(main(),allow_nan=False,separators=(',',':')))
