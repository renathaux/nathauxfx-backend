"""Bounded read of a server-selected settings file, only inside the child.

No production settings/path module imports (those can create directories).
No defaults authorize entry. Missing or malformed evidence fails closed.
"""
import hashlib
import json
import os
import stat


def read(path):
    try:
        if not isinstance(path,str) or not os.path.isabs(path):
            raise ValueError()
        descriptor=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>65536:
                raise ValueError()
            raw=stream.read(65537)
        if len(raw)>65536:
            raise ValueError()
        risk=json.loads(raw)['risk']
        values={key:risk[key] for key in ('maxDailyLoss','maxWeeklyLoss')}
        if any(type(v) not in (type(None),str,int,float) for v in values.values()):
            raise ValueError()
        return dict(values=values,identity=hashlib.sha256(raw).hexdigest())
    except Exception:
        raise ValueError('RISK_SETTINGS_READ_ONLY_UNAVAILABLE') from None
