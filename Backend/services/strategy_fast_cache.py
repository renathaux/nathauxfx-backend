"""Bounded disk cache of immutable market facts, never strategy decisions.

Private backend-owned cache only. No request value is used as a filesystem path.
"""
import hashlib
import os
import pickle
import tempfile
from pathlib import Path

FORMAT = 'facts-v1'

def facts_key(history_hash, symbol, start, end, trading_tf, structure_tf, trend_tf):
    import json
    # Changes to the fact authority invalidate previously serialized timelines.
    from services.strategy_engine import market_facts, market_facts_compact
    from services import strategy_fast_window_facts
    from indicators.smc import legacy_engine
    code = hashlib.sha256(Path(market_facts.__file__).read_bytes()+Path(legacy_engine.__file__).read_bytes()+Path(market_facts_compact.__file__).read_bytes()+Path(strategy_fast_window_facts.__file__).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps([FORMAT, code, history_hash, symbol, str(start), str(end), trading_tf, structure_tf, trend_tf]).encode()).hexdigest()

class FactsCache:
    def __init__(self, root, max_bytes=128*1024*1024):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.max_bytes=max_bytes
    def _path(self,key):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('Invalid cache key')
        return self.root/(key+'.pickle')
    def get(self,key):
        path=self._path(key)
        try:
            with path.open('rb') as stream:value=pickle.load(stream)
            path.touch();return value
        except (OSError,EOFError,pickle.UnpicklingError,AttributeError,ValueError):
            path.unlink(missing_ok=True);return None
    def put(self,key,value):
        path=self._path(key)
        fd,temp=tempfile.mkstemp(dir=self.root,prefix='.facts-')
        try:
            with os.fdopen(fd,'wb') as stream:pickle.dump(value,stream,protocol=pickle.HIGHEST_PROTOCOL)
            size=Path(temp).stat().st_size
            if size>self.max_bytes:return False
            entries=sorted(self.root.glob('*.pickle'),key=lambda p:p.stat().st_mtime)
            total=sum(p.stat().st_size for p in entries if p!=path)
            for entry in entries:
                if total+size<=self.max_bytes:break
                if entry!=path:total-=entry.stat().st_size;entry.unlink(missing_ok=True)
            os.replace(temp,path);return True
        finally:Path(temp).unlink(missing_ok=True)
