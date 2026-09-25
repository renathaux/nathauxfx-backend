"""Read-only, versioned canonical simulator history; no broker or database access.

Mount Frontend/replay-data via SIMULATOR_HISTORY_DIR, or fetch the public,
commit-pinned dataset. Local files are reread on each job to detect changes;
remote immutable file bytes share a 128 MiB disk LRU across worker
subprocesses. FAST jobs bypass the optional process RAM cache. A mounted canonical dataset remains preferred.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import tempfile
from urllib.request import HTTPRedirectHandler, Request, build_opener

import numpy as np
import pandas as pd

DEFAULT_HISTORY_REVISION = "b09874420ce975ee83583d3ba35dbaaf36a5f1a3"
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_DISK_CACHE_BYTES = 128 * 1024 * 1024
DEFAULT_CACHE_DIRECTORY = "/tmp/nathauxfx-fast-history-cache"
_RELATIVE_PATTERN = r"(?:manifest\.json|(?:XAUUSD|EURUSD)/[0-9]{4}-(?:0[1-9]|1[0-2])\.json)"
MAX_FILE_BYTES = 16 * 1024 * 1024
_RAW_ROOT = "https://raw.githubusercontent.com/renathaux/nathauxfx-frontend/"
_FILES: OrderedDict[tuple[str, str], bytes] = OrderedDict()
_CACHE_BYTES = 0
_LOCK = threading.Lock()
_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class LoadedHistory:
    frame_5m: pd.DataFrame
    history_hash: str
    revision: str
    source: str
    months: tuple[str, ...]
    raw_bytes: int

    @property
    def frame(self) -> pd.DataFrame:
        return self.frame_5m

    @property
    def version(self) -> str:
        return self.history_hash


class HistoryFingerprint:
    """Hash each source month once, without retaining its bytes across windows."""
    def __init__(self, symbol, end):
        self.symbol = symbol
        self.end = _utc(end)
        self.digest = None
        self.seen = {}
        self.manifest_hash = None

    def begin(self, revision, history_start, manifest):
        content_hash = hashlib.sha256(manifest).digest()
        if self.digest is None:
            self.digest = hashlib.sha256()
            self.digest.update(f"{revision}:{self.symbol}:{history_start.isoformat()}:{self.end.isoformat()}".encode())
            self.digest.update(manifest)
            self.manifest_hash = content_hash
        elif self.manifest_hash != content_hash:
            raise ValueError('STATIC_HISTORY_CHANGED_DURING_JOB')

    def month(self, month, raw):
        content_hash = hashlib.sha256(raw).digest()
        if month in self.seen:
            if self.seen[month] != content_hash:
                raise ValueError('STATIC_HISTORY_CHANGED_DURING_JOB')
            return
        self.seen[month] = content_hash
        self.digest.update(month.encode())
        self.digest.update(raw)

    def hexdigest(self):
        return self.digest.hexdigest() if self.digest is not None else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("STATIC_HISTORY_REDIRECT_REJECTED")


def _validate_revision(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("SIMULATOR_HISTORY_REVISION must be a full 40-character commit SHA")
    return value.lower()


def _validate_relative(relative: str) -> str:
    if not re.fullmatch(_RELATIVE_PATTERN, relative):
        raise ValueError("STATIC_HISTORY_SOURCE_NOT_ALLOWED")
    return relative


@contextmanager
def _disk_lock():
    # This directory is server configuration, never a request field. Do not
    # follow a pre-created symlink or use a shared/non-owned cache directory.
    directory = Path(os.environ.get("SIMULATOR_HISTORY_CACHE_DIR", DEFAULT_CACHE_DIRECTORY))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise OSError("STATIC_HISTORY_CACHE_DIRECTORY_UNSAFE")
    directory.chmod(0o700)
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _disk_key(revision: str, relative: str) -> str:
    return hashlib.sha256(f"{revision}:{relative}".encode()).hexdigest() + ".json"


def _read_disk(revision: str, relative: str) -> bytes | None:
    try:
        with _disk_lock() as directory:
            path = directory / _disk_key(revision, relative)
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                value = handle.read(MAX_FILE_BYTES + 1)
            if len(value) > min(MAX_FILE_BYTES, MAX_DISK_CACHE_BYTES):
                path.unlink()
                return None
            os.utime(path, None)  # mtime explicitly tracks LRU, independent of atime mount flags.
            return value
    except OSError:
        # An unwritable/ephemeral disk never makes otherwise valid history fail.
        return None


def _touch_disk(revision: str, relative: str) -> None:
    try:
        with _disk_lock() as directory:
            path = directory / _disk_key(revision, relative)
            if not path.is_symlink():
                os.utime(path, None)
    except OSError:
        pass


def _write_disk(revision: str, relative: str, value: bytes) -> None:
    if len(value) > MAX_DISK_CACHE_BYTES:
        return
    try:
        with _disk_lock() as directory:
            target = directory / _disk_key(revision, relative)
            entries = []
            for path in directory.iterdir():
                if path.name.endswith(".tmp"):
                    path.unlink(missing_ok=True)  # Interrupted writer; lock excludes live writers.
                elif re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                    if path.is_symlink():
                        path.unlink()
                    else:
                        stat = path.stat()
                        entries.append((stat.st_mtime_ns, path, stat.st_size))
            size = sum(item[2] for item in entries)
            # Evict before staging the new bytes: transient raw disk use is also bounded.
            for _, path, length in sorted(entries):
                if path == target or size + len(value) > MAX_DISK_CACHE_BYTES:
                    path.unlink(missing_ok=True)
                    size -= length
            fd, temporary = tempfile.mkstemp(suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
    except OSError:
        pass


def _download(url: str) -> bytes:
    # Defense in depth: even internal callers cannot supply another origin/path.
    if not re.fullmatch(re.escape(_RAW_ROOT) + r"[0-9a-f]{40}/Frontend/replay-data/" + _RELATIVE_PATTERN, url):
        raise ValueError("STATIC_HISTORY_SOURCE_NOT_ALLOWED")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "FlowSignalSimulator/1"})
    with build_opener(_NoRedirect()).open(request, timeout=30) as response:
        value = response.read(MAX_FILE_BYTES + 1)
    if len(value) > MAX_FILE_BYTES:
        raise ValueError("STATIC_HISTORY_FILE_TOO_LARGE")
    return value


def clear_history_file_cache() -> None:
    """Clear process RAM only; immutable disk entries intentionally survive jobs."""
    global _CACHE_BYTES
    with _LOCK:
        _FILES.clear()
        _CACHE_BYTES = 0


def history_file_cache_info() -> dict:
    with _LOCK:
        return {"bytes": _CACHE_BYTES, "entries": len(_FILES), "limit_bytes": MAX_CACHE_BYTES}


def _read_file(relative: str, directory: Path | None, revision: str, *, retain_in_memory: bool = True, use_disk_cache: bool = True) -> bytes:
    global _CACHE_BYTES
    relative = _validate_relative(relative)
    revision = _validate_revision(revision)
    if directory is not None:
        with (directory / relative).open("rb") as handle:
            value = handle.read(MAX_FILE_BYTES + 1)
        if len(value) > MAX_FILE_BYTES:
            raise ValueError("STATIC_HISTORY_FILE_TOO_LARGE")
        return value
    if not use_disk_cache:
        return _download(f"{_RAW_ROOT}{revision}/Frontend/replay-data/{relative}")
    key = (revision, relative)
    with _LOCK:
        if retain_in_memory and key in _FILES:
            _FILES.move_to_end(key)
            value = _FILES[key]
            _touch_disk(revision, relative)
            return value
    value = _read_disk(revision, relative)
    if value is None:
        value = _download(f"{_RAW_ROOT}{revision}/Frontend/replay-data/{relative}")
        _write_disk(revision, relative, value)
    if not retain_in_memory:
        return value
    with _LOCK:
        previous = _FILES.pop(key, None)
        if previous is not None:
            _CACHE_BYTES -= len(previous)
        while _FILES and _CACHE_BYTES + len(value) > MAX_CACHE_BYTES:
            _, removed = _FILES.popitem(last=False)
            _CACHE_BYTES -= len(removed)
        if len(value) <= MAX_CACHE_BYTES:
            _FILES[key] = value
            _CACHE_BYTES += len(value)
    return value


def _utc(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("SIMULATION_RANGE_INVALID")
    return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")


def _month_frame(payload: dict, symbol: str, month: str, history_start, end) -> pd.DataFrame:
    if not isinstance(payload, dict) or payload.get("symbol") != symbol or payload.get("timeframe") != "5m" or not isinstance(payload.get("candles"), list):
        raise ValueError(f"STATIC_HISTORY_INVALID_FILE: {symbol} {month}")
    rows = payload["candles"]
    if not rows:
        raise ValueError(f"STATIC_HISTORY_EMPTY_MONTH: {symbol} {month}")
    try:
        timestamps = pd.DatetimeIndex(pd.to_datetime([row["timestamp"] for row in rows], utc=True))
        if timestamps.hasnans or timestamps.has_duplicates:
            raise ValueError("invalid or duplicate candle timestamp")
        if len(timestamps) and not (timestamps.strftime("%Y-%m") == month).all():
            raise ValueError("candle outside its monthly file")
        selected = (timestamps >= history_start) & (timestamps < end)
        values = np.empty((int(selected.sum()), 5), dtype=np.float64)
        cursor = 0
        for row, keep in zip(rows, selected):
            if not keep:
                continue
            for index, key in enumerate(("open", "high", "low", "close")):
                number = float(row[key])
                if not np.isfinite(number):
                    raise ValueError(f"nonfinite {key}")
                values[cursor, index] = number
            try:
                volume = float(row.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            values[cursor, 4] = volume if np.isfinite(volume) else 0.0
            cursor += 1
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"STATIC_HISTORY_INVALID_CANDLES: {symbol} {month}: {error}") from error
    return pd.DataFrame(values, index=timestamps[selected], columns=_COLUMNS).sort_index()


def load_fast_history(symbol: str, start, end, warmup_days: int = 7, *, history_dir=None, revision=None, progress=None, use_disk_cache=True, fingerprint=None, require_evaluation=True) -> LoadedHistory:
    """Load one numeric 5m frame, with [start-warmup, end) candles.

    Configuration is server-owned. Requests must never forward user-selected
    history_dir/revision values. history_hash covers revision, manifest, every
    selected monthly file and requested bounds; use it in immutable fact keys.
    The returned frame is job-owned, never shared through the file cache.
    """
    symbol = str(symbol).upper().replace("/", "")
    if symbol not in {"XAUUSD", "EURUSD"}:
        raise ValueError("SIMULATOR_SYMBOL_UNSUPPORTED")
    start, end = _utc(start), _utc(end)
    if end <= start or (end - start) > pd.Timedelta(days=5 * 366):
        raise ValueError("SIMULATION_RANGE_INVALID")
    if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or not 0 <= warmup_days <= 366:
        raise ValueError("SIMULATION_WARMUP_INVALID")
    revision = _validate_revision(revision or os.environ.get("SIMULATOR_HISTORY_REVISION", DEFAULT_HISTORY_REVISION))
    directory_value = history_dir if history_dir is not None else os.environ.get("SIMULATOR_HISTORY_DIR")
    directory = Path(directory_value).expanduser().resolve() if directory_value else None
    cache_options = {} if use_disk_cache else {"use_disk_cache": False}
    manifest_raw = _read_file("manifest.json", directory, revision, retain_in_memory=False, **cache_options)
    manifest = json.loads(manifest_raw)
    if manifest.get("version") != 1 or manifest.get("base_timeframe") != "5m":
        raise ValueError("STATIC_HISTORY_MANIFEST_INVALID")
    available = manifest.get("symbols", {}).get(symbol)
    if not isinstance(available, dict) or not available.get("months"):
        raise ValueError("STATIC_HISTORY_SYMBOL_UNAVAILABLE")
    history_start = start - pd.Timedelta(days=warmup_days)
    firsts = [_utc(available[m]["first_timestamp"]) for m in available["months"] if available.get(m, {}).get("first_timestamp")]
    if firsts:
        history_start = max(history_start, min(firsts))
    if history_start >= end:
        raise ValueError("STATIC_HISTORY_RANGE_UNAVAILABLE")
    months = tuple(pd.period_range(history_start.tz_localize(None).to_period("M"), (end - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M"), freq="M").astype(str))
    missing = set(months) - set(available["months"])
    if missing:
        raise ValueError(f"STATIC_HISTORY_MONTH_UNAVAILABLE: {','.join(sorted(missing))}")
    if fingerprint is not None:
        fingerprint.begin(revision, history_start, manifest_raw)
    digest = hashlib.sha256()
    digest.update(f"{revision}:{symbol}:{history_start.isoformat()}:{end.isoformat()}".encode())
    digest.update(manifest_raw)
    raw_bytes = len(manifest_raw)
    # Manifest counts are sizing hints only; never trust them for validation or
    # bounds. Grow for unusual/off-grid data, and trim after loading. Keeping
    # only one monthly frame avoids concat + global-sort copies of five years.
    dense_count = max(1, int((end - history_start) / pd.Timedelta(minutes=5)) + 1)
    hinted_count = sum(available.get(month, {}).get("count", 0)
                       if isinstance(available.get(month, {}).get("count"), int) else 0
                       for month in months)
    capacity = min(hinted_count, dense_count) if hinted_count > 0 else dense_count
    values = np.empty((capacity, 5), dtype=np.float64)
    stamps = np.empty(capacity, dtype=np.int64)
    position = 0
    timestamp_unit = None
    for month_index, month in enumerate(months):
        raw = _read_file(f"{symbol}/{month}.json", directory, revision, retain_in_memory=False, **cache_options)
        if fingerprint is not None:
            fingerprint.month(month, raw)
        digest.update(month.encode())
        digest.update(raw)
        raw_bytes += len(raw)
        monthly = _month_frame(json.loads(raw), symbol, month, history_start, end)
        del raw
        unit = monthly.index.unit
        if timestamp_unit is None:
            timestamp_unit = unit
        elif np.dtype(f"datetime64[{unit}]") != np.dtype(f"datetime64[{timestamp_unit}]"):
            common_unit = np.datetime_data(np.result_type(f"datetime64[{timestamp_unit}]", f"datetime64[{unit}]"))[0]
            if common_unit != timestamp_unit:
                stamps[:position] = stamps[:position].view(f"datetime64[{timestamp_unit}]").astype(f"datetime64[{common_unit}]").view(np.int64)
                timestamp_unit = common_unit
        required = position + len(monthly)
        if required > capacity:
            capacity = max(required, capacity + max(len(monthly), capacity // 4))
            values.resize((capacity, 5), refcheck=False)
            stamps.resize(capacity, refcheck=False)
        values[position:required] = monthly.to_numpy(copy=False)
        stamps[position:required] = monthly.index.as_unit(timestamp_unit).asi8
        position = required
        del monthly
        if progress is not None:
            progress((month_index + 1) / len(months))
    values.resize((position, 5), refcheck=False)
    stamps.resize(position, refcheck=False)
    index = pd.DatetimeIndex(stamps.view(f"datetime64[{timestamp_unit}]"), tz="UTC")
    frame = pd.DataFrame(values, index=index, columns=_COLUMNS, copy=False)
    if require_evaluation and (frame.empty or not (frame.index >= start).any()):
        raise ValueError("STATIC_HISTORY_RANGE_UNAVAILABLE")
    # Each month is already sorted and checked for duplicate timestamps and
    # month ownership, so chronological disjoint months need no global sort
    # or duplicate-index hash table.
    return LoadedHistory(frame, digest.hexdigest(), revision, str(directory) if directory else f"{_RAW_ROOT}{revision}/Frontend/replay-data", months, raw_bytes)


HistoryData = LoadedHistory


def load_history(symbol: str, start, end, warmup_days: int = 7, progress=None) -> HistoryData:
    """Worker interface: frame/version plus metadata; progress receives 0..1.

    Source configuration is read exclusively from the server environment.
    Progress emits once per loaded month, never once per candle.
    """
    return load_fast_history(symbol, start, end, warmup_days, progress=progress)
