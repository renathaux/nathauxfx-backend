"""Array-backed immutable fact storage for long FAST jobs (same fact values)."""
from bisect import bisect_right, bisect_left
import numpy as np
import pandas as pd
from services.strategy_engine.market_facts import MarketFactsTimeline, _utc
from services.strategy_engine.types import CandleFacts, TrendFacts

class CandleStore:
    def __init__(self,frame,offset=pd.Timedelta(0)):
        self.time_scale = {'s': 10**9, 'ms': 10**6, 'us': 10**3, 'ns': 1}[frame.index.unit]
        self.times = frame.index.asi8
        if offset.value % self.time_scale:
            self.times = frame.index.as_unit('ns').asi8
            self.time_scale = 1
        if offset.value:
            self.times = self.times + offset.value // self.time_scale
        # Canonical history keeps float64 OHLC columns together. Share their
        # buffer instead of retaining a second full-history price array.
        columns = list(frame.columns)
        if (columns[:4] == ['Open', 'High', 'Low', 'Close']
                and all(dtype == np.dtype('float64') for dtype in frame.dtypes)):
            self.values = frame.to_numpy(dtype=float, copy=False)[:, :4]
        else:
            self.values = frame[['Open','High','Low','Close']].to_numpy(dtype=float, copy=False)
        self.times.flags.writeable=False;self.values.flags.writeable=False
    def __len__(self):return len(self.times)
    def __contains__(self,stamp):
        value, remainder = divmod(_utc(stamp).value, self.time_scale)
        if remainder:
            return False
        i=int(np.searchsorted(self.times,value));return i<len(self.times) and self.times[i]==value
    def get(self,stamp):
        stamp=_utc(stamp)
        value, remainder = divmod(stamp.value, self.time_scale)
        if remainder:
            return None
        i=int(np.searchsorted(self.times,value))
        if i>=len(self.times) or self.times[i]!=value:return None
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
        self._events=events;self._trends=TrendStore(trends)
        self._index=pd.DatetimeIndex(timestamps)
        self._highs=ConfirmedPriceIndex(trading_swings,'HIGH');self._lows=ConfirmedPriceIndex(trading_swings,'LOW')
    def trend(self, timestamp):return self._trends.at(timestamp)
    def timestamps(self):return list(self._index)
    def previous_timestamp(self,timestamp):
        i=int(self._index.searchsorted(_utc(timestamp),side='right'))-1
        return self._index[i-1] if i>0 else None
    def next_timestamp(self,timestamp):
        i=int(self._index.searchsorted(_utc(timestamp),side='right'))
        return self._index[i] if i<len(self._index) else None
    def opposite_swing(self,timestamp,direction,entry):
        return (self._highs if direction=='BUY' else self._lows).find(_utc(timestamp).value,entry,direction=='BUY')


class TrendStore:
    """Three-state directions in bytes, with exact as-of timestamp lookup."""
    def __init__(self, trends):
        keys = sorted(trends)
        self.times = np.asarray([stamp.value for stamp in keys], dtype=np.int64)
        codes = {None: 0, 'BUY': 1, 'SELL': 2}
        self.values = np.asarray([
            [codes[f.bos_choch_direction], codes[f.ema50_direction],
             codes[f.ema200_direction], codes[f.swing_structure_direction]]
            for f in (trends[stamp] for stamp in keys)
        ], dtype=np.uint8).reshape((-1, 4))
        self.times.flags.writeable = self.values.flags.writeable = False

    def at(self, timestamp):
        i = int(np.searchsorted(self.times, _utc(timestamp).value, side='right')) - 1
        if i < 0:
            return TrendFacts(None, None, None, None)
        directions = (None, 'BUY', 'SELL')
        return TrendFacts(*(directions[int(code)] for code in self.values[i]))


class SwingStore:
    """Numeric form of the default two-left/two-right confirmed pivots.

    The inequalities and HIGH-before-LOW tie order match the exported detector.
    Only a current window materializes legacy swing dictionaries.
    """
    def __init__(self, frame):
        data = frame.dropna(subset=['Open', 'High', 'Low', 'Close'])
        self.times = data.index
        high = data.High.to_numpy(dtype=float, copy=False)
        low = data.Low.to_numpy(dtype=float, copy=False)
        if len(data) < 5:
            self.indices = np.empty(0, dtype=np.int64)
            self.kinds = np.empty(0, dtype=np.uint8)
            self.prices = np.empty(0, dtype=np.float64)
        else:
            center_high, center_low = high[2:-2], low[2:-2]
            is_high = ((center_high > high[:-4]) & (center_high > high[1:-3]) &
                       (center_high >= high[3:-1]) & (center_high >= high[4:]))
            is_low = ((center_low < low[:-4]) & (center_low < low[1:-3]) &
                      (center_low <= low[3:-1]) & (center_low <= low[4:]))
            row, kind = np.nonzero(np.column_stack((is_high, is_low)))
            self.indices = row + 2
            self.kinds = kind.astype(np.uint8)
            self.prices = np.where(kind == 0, high[self.indices], low[self.indices])
        for array in (self.indices, self.kinds, self.prices):
            array.flags.writeable = False

    @property
    def nbytes(self):
        return sum(a.nbytes for a in (self.times, self.indices, self.kinds, self.prices))

    def window(self, first, last):
        left, right = np.searchsorted(self.indices, [first + 2, last - 2])
        return [dict(type='HIGH' if self.kinds[i] == 0 else 'LOW',
                     timestamp=self.times[pivot].isoformat(),
                     confirmed_timestamp=self.times[pivot + 2].isoformat(),
                     price=float(self.prices[i]), index=int(pivot - first),
                     confirmed_index=int(pivot + 2 - first))
                for i in range(int(left), int(right))
                for pivot in [self.indices[i]]]
