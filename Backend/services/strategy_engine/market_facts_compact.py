"""Array-backed immutable fact storage for long FAST jobs (same fact values)."""
from bisect import bisect_right, bisect_left
import numpy as np
import pandas as pd
from services.strategy_engine.market_facts import MarketFactsTimeline, _utc
from services.strategy_engine.types import CandleFacts

class CandleStore:
    def __init__(self,frame,offset=pd.Timedelta(0)):
        self.times=frame.index.as_unit('ns').asi8.copy()+offset.value
        self.values=frame[['Open','High','Low','Close']].to_numpy(dtype=float,copy=True)
        self.times.flags.writeable=False;self.values.flags.writeable=False
    def __len__(self):return len(self.times)
    def __contains__(self,stamp):
        value=_utc(stamp).value;i=int(np.searchsorted(self.times,value));return i<len(self.times) and self.times[i]==value
    def get(self,stamp):
        stamp=_utc(stamp);i=int(np.searchsorted(self.times,stamp.value))
        if i>=len(self.times) or self.times[i]!=stamp.value:return None
        o,h,l,c=map(float,self.values[i]);return CandleFacts(stamp,o,h,l,c,abs(c-o)/max(h-l,1e-12)*100)

class ConfirmedPriceIndex:
    """Price-ordered tree of first confirmation times; queries cannot see future confirmations."""
    def __init__(self,swings,kind):
        first={}
        for s in swings:
            if s['type']==kind:
                price=float(s['price']);t=_utc(s['confirmed_timestamp']).value
                first[price]=min(first.get(price,t),t)
        self.prices=sorted(first);size=1
        while size<len(self.prices):size*=2
        self.size=size;self.tree=np.full(2*size,np.iinfo(np.int64).max,dtype=np.int64)
        for i,p in enumerate(self.prices):self.tree[size+i]=first[p]
        for i in range(size-1,0,-1):self.tree[i]=min(self.tree[2*i],self.tree[2*i+1])
        self.tree.flags.writeable=False
    def find(self,t,entry,above):
        lo=bisect_right(self.prices,entry) if above else 0
        hi=len(self.prices) if above else bisect_left(self.prices,entry)
        def visit(node,left,right):
            if right<=lo or left>=hi or self.tree[node]>t:return None
            if right-left==1:return self.prices[left] if left<len(self.prices) else None
            mid=(left+right)//2
            children=[(node*2,left,mid),(node*2+1,mid,right)]
            if not above:children.reverse()
            for child in children:
                result=visit(*child)
                if result is not None:return result
            return None
        return visit(1,0,self.size)

class CompactTimeline(MarketFactsTimeline):
    def __init__(self,*,candles,events,trends,timestamps,trading_swings,structure_candles=None):
        self._candles=candles;self._structure_candles=structure_candles or candles
        self._events=events;self._trends=trends;self._trend_keys=sorted(trends)
        self._index=pd.DatetimeIndex(timestamps)
        self._highs=ConfirmedPriceIndex(trading_swings,'HIGH');self._lows=ConfirmedPriceIndex(trading_swings,'LOW')
    def timestamps(self):return list(self._index)
    def previous_timestamp(self,timestamp):
        i=int(self._index.searchsorted(_utc(timestamp),side='right'))-1
        return self._index[i-1] if i>0 else None
    def next_timestamp(self,timestamp):
        i=int(self._index.searchsorted(_utc(timestamp),side='right'))
        return self._index[i] if i<len(self._index) else None
    def opposite_swing(self,timestamp,direction,entry):
        return (self._highs if direction=='BUY' else self._lows).find(_utc(timestamp).value,entry,direction=='BUY')
