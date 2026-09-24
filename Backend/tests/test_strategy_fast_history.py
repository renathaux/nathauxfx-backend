import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from services import strategy_fast_history as history
from services.strategy_simulator_static_data import _canonical_frame


def candle(timestamp, **kwargs):
    return {"timestamp": timestamp, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 4, **kwargs}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATOR_HISTORY_CACHE_DIR", str(tmp_path / "raw-cache"))
    history.clear_history_file_cache()
    yield
    history.clear_history_file_cache()


@pytest.fixture
def dataset(tmp_path):
    (tmp_path / "XAUUSD").mkdir()
    manifest = {"version": 1, "base_timeframe": "5m", "symbols": {"XAUUSD": {
        "months": ["2024-01", "2024-02"],
        "2024-01": {"first_timestamp": "2024-01-01T00:00:00Z"},
        "2024-02": {"first_timestamp": "2024-02-01T00:00:00Z"},
    }}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for month, rows in [
        ("2024-01", [candle("2024-01-31T23:55:00Z", volume="bad")]),
        ("2024-02", [candle("2024-02-01T00:00:00Z", volume=float("nan")), candle("2024-02-01T00:05:00Z"), candle("2024-02-01T00:10:00Z")]),
    ]:
        (tmp_path / "XAUUSD" / f"{month}.json").write_text(json.dumps({"symbol": "XAUUSD", "timeframe": "5m", "candles": rows}))
    return tmp_path


def load(dataset, **kwargs):
    return history.load_fast_history("XAUUSD", "2024-02-01", "2024-02-01T00:10:00Z", history_dir=dataset, **kwargs)


def test_numeric_frame_matches_existing_canonical_volume_and_bounds(dataset):
    result = load(dataset)
    rows = []
    for month in result.months:
        rows.extend(json.loads((dataset / "XAUUSD" / f"{month}.json").read_text())["candles"])
    expected = _canonical_frame(rows, "2024-02-01", "2024-02-01T00:10:00Z")
    pd.testing.assert_frame_equal(result.frame_5m, expected)
    assert len(result.frame_5m) == 3
    assert len(result.history_hash) == 64
    assert all(str(dtype) == "float64" for dtype in result.frame_5m.dtypes)


def test_local_content_invalidation_without_manifest_change(dataset):
    first = load(dataset)
    filename = dataset / "XAUUSD/2024-02.json"
    value = json.loads(filename.read_text())
    value["candles"][0]["close"] = 10.5
    filename.write_text(json.dumps(value))
    second = load(dataset)
    assert first.history_hash != second.history_hash
    assert second.frame_5m.iloc[1].Close == 10.5


def test_revision_and_window_invalidate_hash(dataset):
    assert load(dataset).history_hash != load(dataset, revision="a" * 40).history_hash
    assert load(dataset).history_hash != load(dataset, warmup_days=0).history_hash


def test_missing_month_is_failure(dataset):
    with pytest.raises(ValueError, match="MONTH_UNAVAILABLE"):
        history.load_fast_history("XAUUSD", "2024-02-01", "2024-03-02", history_dir=dataset)
    (dataset / "XAUUSD/2024-02.json").unlink()
    with pytest.raises(FileNotFoundError):
        load(dataset)


@pytest.mark.parametrize("change", ["duplicate", "nonfinite", "outside_month", "bad_timestamp"])
def test_invalid_candles_rejected(dataset, change):
    filename = dataset / "XAUUSD/2024-02.json"
    value = json.loads(filename.read_text())
    if change == "duplicate":
        value["candles"].append(value["candles"][0])
    elif change == "nonfinite":
        value["candles"][0]["open"] = float("inf")
    elif change == "outside_month":
        value["candles"][0]["timestamp"] = "2024-01-31T23:55:00Z"
    else:
        value["candles"][0]["timestamp"] = None
    filename.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="INVALID_CANDLES"):
        load(dataset)


@pytest.mark.parametrize("revision", ["main", "https://evil.example", "../manifest", "abc123"])
def test_invalid_revision_rejected_before_io(dataset, revision):
    with pytest.raises(ValueError, match="40-character"):
        load(dataset, revision=revision)


@pytest.mark.parametrize("url", ["http://127.0.0.1/manifest.json", "https://www.nathauxfx.com.evil/replay-data/manifest.json", "https://raw.githubusercontent.com/evil/repo/main/manifest.json"])
def test_arbitrary_network_source_rejected(url):
    with pytest.raises(ValueError, match="SOURCE_NOT_ALLOWED"):
        history._download(url)


def test_redirect_rejected():
    with pytest.raises(ValueError, match="REDIRECT_REJECTED"):
        history._NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1")


def test_remote_cache_is_revision_keyed_and_byte_bounded(monkeypatch):
    history.clear_history_file_cache()
    calls = []
    def download(url):
        calls.append(url)
        return b"123456"
    monkeypatch.setattr(history, "_download", download)
    monkeypatch.setattr(history, "MAX_CACHE_BYTES", 10)
    history._read_file("manifest.json", None, "a" * 40)
    history._read_file("manifest.json", None, "a" * 40)
    assert len(calls) == 1
    history._read_file("manifest.json", None, "b" * 40)
    assert len(calls) == 2
    assert history.history_file_cache_info()["bytes"] == 6
    history._read_file("manifest.json", None, "a" * 40)
    assert len(calls) == 2  # Recovered from persistent disk after RAM eviction.
    history.clear_history_file_cache()


def test_environment_mount_and_revision(dataset, monkeypatch):
    monkeypatch.setenv("SIMULATOR_HISTORY_DIR", str(dataset))
    monkeypatch.setenv("SIMULATOR_HISTORY_REVISION", "b" * 40)
    result = history.load_fast_history("XAUUSD", "2024-02-01", "2024-02-02")
    assert result.revision == "b" * 40
    assert result.source == str(dataset)


def test_worker_interface_and_monthly_progress(dataset, monkeypatch):
    monkeypatch.setenv("SIMULATOR_HISTORY_DIR", str(dataset))
    progress = []
    result = history.load_history("XAUUSD", "2024-02-01", "2024-02-02", progress=progress.append)
    assert isinstance(result, history.HistoryData)
    assert result.frame is result.frame_5m
    assert result.version == result.history_hash
    assert progress == [0.5, 1.0]


def test_empty_month_is_failure(dataset):
    filename = dataset / "XAUUSD/2024-01.json"
    value = json.loads(filename.read_text())
    value["candles"] = []
    filename.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="EMPTY_MONTH"):
        load(dataset)


def test_remote_disk_survives_fresh_python_process(monkeypatch):
    monkeypatch.setattr(history, "_download", lambda url: b"immutable-history")
    assert history._read_file("manifest.json", None, "a" * 40) == b"immutable-history"
    script = """
from services import strategy_fast_history as h
h._download = lambda url: (_ for _ in ()).throw(AssertionError('network used'))
assert h._read_file('manifest.json', None, 'a' * 40) == b'immutable-history'
"""
    subprocess.run([sys.executable, "-c", script], check=True, env={**os.environ, "PYTHONPATH": str(Path(history.__file__).parents[1])})


def test_disk_cache_revision_invalidation_and_private_permissions(monkeypatch):
    calls = []
    monkeypatch.setattr(history, "_download", lambda url: calls.append(url) or url.encode())
    first = history._read_file("manifest.json", None, "a" * 40)
    history.clear_history_file_cache()
    assert first == history._read_file("manifest.json", None, "a" * 40)
    assert first != history._read_file("manifest.json", None, "b" * 40)
    assert len(calls) == 2
    directory = Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"])
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())


def test_disk_lru_is_byte_bounded_across_process_cache_resets(monkeypatch):
    calls = []
    monkeypatch.setattr(history, "MAX_DISK_CACHE_BYTES", 12)
    monkeypatch.setattr(history, "_download", lambda url: calls.append(url) or b"123456")
    for revision in ['a', 'b', 'a', 'c', 'a']:
        history.clear_history_file_cache()
        history._read_file("manifest.json", None, revision * 40)
    assert len(calls) == 3  # Touching A kept it; C evicted B.
    files = list(Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"]).glob("*.json"))
    assert len(files) == 2 and sum(path.stat().st_size for path in files) == 12
    history.clear_history_file_cache()
    history._read_file("manifest.json", None, "b" * 40)
    assert len(calls) == 4
    assert not list(Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"]).glob("*.tmp"))


@pytest.mark.parametrize("relative", ['../secret', '/etc/passwd', 'XAUUSD/../../secret', 'XAUUSD/2024-13.json', 'http://127.0.0.1', 'BTCUSD/2024-01.json'])
def test_cache_rejects_noncanonical_paths_before_any_io(relative, monkeypatch):
    monkeypatch.setattr(history, "_download", lambda url: pytest.fail("network must not be used"))
    with pytest.raises(ValueError, match="SOURCE_NOT_ALLOWED"):
        history._read_file(relative, None, "a" * 40)
    with pytest.raises(ValueError, match="SOURCE_NOT_ALLOWED"):
        history._read_file(relative, Path('/tmp'), "a" * 40)
    assert not Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"]).exists()


def test_ram_hits_refresh_disk_lru(monkeypatch):
    calls = []
    monkeypatch.setattr(history, "MAX_DISK_CACHE_BYTES", 12)
    monkeypatch.setattr(history, "_download", lambda url: calls.append(url) or b"123456")
    for revision in ['a', 'b', 'a', 'c']:
        history._read_file("manifest.json", None, revision * 40)
    history.clear_history_file_cache()
    history._read_file("manifest.json", None, "a" * 40)
    assert len(calls) == 3
    history._read_file("manifest.json", None, "b" * 40)
    assert len(calls) == 4


def test_interrupted_disk_write_never_serves_partial_bytes(monkeypatch):
    calls = []
    monkeypatch.setattr(history, "_download", lambda url: calls.append(url) or b"complete")
    replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk failure")))
    assert history._read_file("manifest.json", None, "a" * 40) == b"complete"
    directory = Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"])
    assert not list(directory.glob("*.json")) and not list(directory.glob("*.tmp"))
    monkeypatch.setattr(os, "replace", replace)
    history.clear_history_file_cache()
    assert history._read_file("manifest.json", None, "a" * 40) == b"complete"
    assert len(calls) == 2


def test_symlink_cache_entry_not_read_or_overwritten_outside_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_download", lambda url: b"canonical")
    directory = Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"])
    directory.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    (directory / history._disk_key("a" * 40, "manifest.json")).symlink_to(secret)
    assert history._read_file("manifest.json", None, "a" * 40) == b"canonical"
    assert secret.read_bytes() == b"private"


def test_symlink_cache_root_is_bypassed(tmp_path, monkeypatch):
    directory = Path(os.environ["SIMULATOR_HISTORY_CACHE_DIR"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    directory.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(history, "_download", lambda url: b"canonical")
    assert history._read_file("manifest.json", None, "a" * 40) == b"canonical"
    assert not list(elsewhere.iterdir())


def test_fast_remote_load_retains_no_raw_ram_bytes(dataset, monkeypatch):
    calls = []
    def download(url):
        relative = url.split('/Frontend/replay-data/', 1)[1]
        calls.append(relative)
        return (dataset / relative).read_bytes()
    monkeypatch.delenv("SIMULATOR_HISTORY_DIR", raising=False)
    monkeypatch.setattr(history, "_download", download)
    first = history.load_fast_history("XAUUSD", "2024-02-01", "2024-02-01T00:10Z")
    second = history.load_fast_history("XAUUSD", "2024-02-01", "2024-02-01T00:10Z")
    assert history.history_file_cache_info()["bytes"] == 0
    assert len(calls) == 3  # Disk cache survives both loads.
    assert first.history_hash == second.history_hash
    pd.testing.assert_frame_equal(first.frame, second.frame, check_exact=True)


@pytest.mark.parametrize("hint", [None, 1, -10, 100000000000, "bad"])
def test_manifest_counts_only_hint_allocation(dataset, hint):
    path = dataset / "manifest.json"
    payload = json.loads(path.read_text())
    for month in payload["symbols"]["XAUUSD"]["months"]:
        payload["symbols"]["XAUUSD"][month]["count"] = hint
    path.write_text(json.dumps(payload))
    result = load(dataset)
    assert len(result.frame) == 3
    assert result.frame.index.is_monotonic_increasing
    assert all(str(dtype) == "float64" for dtype in result.frame.dtypes)


def test_mixed_timestamp_precision_preserved_across_months(dataset):
    path = dataset / "XAUUSD/2024-02.json"
    payload = json.loads(path.read_text())
    payload['candles'][0]['timestamp'] = '2024-02-01T00:00:00.000000001Z'
    # Use consistent ISO formatting within each month for pandas parsing.
    payload['candles'][1]['timestamp'] = '2024-02-01T00:05:00.000000000Z'
    payload['candles'][2]['timestamp'] = '2024-02-01T00:10:00.000000000Z'
    path.write_text(json.dumps(payload))
    result = load(dataset)
    frames = [history._month_frame(json.loads((dataset / 'XAUUSD' / f'{month}.json').read_text()), 'XAUUSD', month, pd.Timestamp('2024-01-25', tz='UTC'), pd.Timestamp('2024-02-01T00:10Z')) for month in result.months]
    pd.testing.assert_frame_equal(result.frame, pd.concat(frames).sort_index(), check_exact=True)
