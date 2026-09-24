"""Throwaway staging guard. Never import or enable this in production."""
import os
from pathlib import Path
import socket
import sys
import tempfile


def validate_database(url):
    if not url.startswith('sqlite:////tmp/capacity-isolated-') and not url.startswith('sqlite:////private/var/') and not url.startswith('sqlite:////var/folders/'):
        raise RuntimeError('Capacity staging requires isolated SQLite')


def install():
    if os.environ.get('CAPACITY_STAGING') != '1':
        raise RuntimeError('Not an explicitly isolated staging process')
    forbidden=[k for k in os.environ if k.startswith(('CTRADER_','DERIV_','SMTP_')) and os.environ[k]]
    if forbidden:
        raise RuntimeError('Broker/email credentials forbidden in staging')
    if os.environ.get('DATABASE_URL'):
        validate_database(os.environ['DATABASE_URL'])
    root=Path(os.environ.get('CAPACITY_STATE_ROOT') or tempfile.mkdtemp(prefix='capacity-isolated-'))
    if not root.name.startswith('capacity-isolated-'):
        raise RuntimeError('Invalid staging root')
    root.mkdir(parents=True,exist_ok=True)
    os.environ['CAPACITY_STATE_ROOT']=str(root)
    os.environ['DATABASE_URL']='sqlite:///'+str(root/'database'/'staging.db')
    import dotenv
    dotenv.load_dotenv=lambda *a,**k:False
    import paths
    for name,sub in [('DATA_DIR','data'),('DATABASE_DIR','database'),('CACHE_DIR','cache'),('CANDLE_CACHE_DIR','cache/candle_cache')]:
        setattr(paths,name,root/sub)
    paths.ensure_runtime_dirs()
    os.environ['SIMULATOR_FAST_JOB_DIR']=str(root/'jobs')
    os.environ['SIMULATOR_HISTORY_CACHE_DIR']=str(root/'history')
    os.environ['HEAVY_REPLAY_LOCK_PATH']=str(root/'heavy.lock')
    # DNS and connect both restricted. Raw GitHub is the public historical data
    # source; no broker, arbitrary proxy, or database sockets are allowed.
    allowed={'127.0.0.1','::1'}
    resolve=socket.getaddrinfo
    def safe_resolve(host,port,*args,**kwargs):
        if host not in ('raw.githubusercontent.com','localhost','127.0.0.1','::1'):
            raise PermissionError('Staging outbound host blocked')
        values=resolve(host,port,*args,**kwargs)
        if host=='raw.githubusercontent.com':
            allowed.update(row[4][0] for row in values)
        return values
    socket.getaddrinfo=safe_resolve
    def audit(event,args):
        if event=='socket.connect':
            target=args[1]
            if not isinstance(target,tuple) or target[0] not in allowed or (target[0] not in ('127.0.0.1','::1') and target[1]!=443):
                raise PermissionError('Staging outbound connection blocked')
    sys.addaudithook(audit)
