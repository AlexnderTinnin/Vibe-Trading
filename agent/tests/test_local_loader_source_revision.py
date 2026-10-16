"""Persisted local bars must follow observed filesystem source revisions."""

import os
from pathlib import Path

import pandas as pd
import pytest

from backtest.loaders import base, local_loader


def _write(path: Path, kind: str, close: int) -> None:
    frame = pd.DataFrame(
        {
            "date": ["2025-01-02"],
            "open": [10],
            "high": [100],
            "low": [1],
            "close": [close],
            "volume": [100],
        }
    )
    if kind == "csv":
        frame.to_csv(path, index=False)
    elif kind == "parquet":
        frame.to_parquet(path, index=False)
    else:
        import duckdb

        with duckdb.connect(str(path)) as connection:
            connection.register("source", frame)
            connection.execute("CREATE OR REPLACE TABLE bars AS SELECT * FROM source")


def _loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str):
    if kind == "parquet":
        pytest.importorskip("pyarrow", reason="local Parquet reads require an optional pandas engine")
    path = tmp_path / ("bars." + kind)
    entry = {"symbol": "AAA.US", "type": kind}
    if kind == "duckdb":
        entry.update(db_path=str(path), query="SELECT * FROM bars")
    else:
        # Reader and filesystem identity must agree on whitespace handling.
        entry["path"] = "  " + str(path) + "  "
    monkeypatch.setattr(base, "loader_cache_enabled", lambda: True)
    monkeypatch.setattr(base, "loader_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(local_loader, "_load_config", lambda: {"sources": [entry]})

    def fetch():
        return local_loader.DataLoader().fetch(["AAA.US"], "2025-01-02", "2025-01-03")

    return path, entry, fetch


@pytest.mark.parametrize("kind", ["csv", "parquet", "duckdb"])
def test_rewritten_local_source_invalidates_persisted_bars(tmp_path, monkeypatch, kind):
    path, _entry, fetch = _loader(tmp_path, monkeypatch, kind)
    _write(path, kind, 10)
    assert fetch()["AAA.US"]["close"].tolist() == [10]
    before = path.stat()
    _write(path, kind, 20)
    if kind == "csv":
        assert path.stat().st_size == before.st_size
    os.utime(
        path,
        ns=(
            before.st_atime_ns,
            max(path.stat().st_mtime_ns, before.st_mtime_ns + 1_000_000),
        ),
    )
    assert fetch()["AAA.US"]["close"].tolist() == [20]
    assert list((tmp_path / "cache").rglob("*.parquet"))

    def unexpected_read(*args):
        raise AssertionError("unchanged source should retain its persisted cache hit")

    if kind == "duckdb":
        monkeypatch.setattr(local_loader, "_read_duckdb", unexpected_read)
    else:
        monkeypatch.setitem(local_loader._READERS, kind, unexpected_read)
    assert fetch()["AAA.US"]["close"].tolist() == [20]


@pytest.mark.parametrize("kind", ["csv", "parquet", "duckdb"])
def test_deleted_local_source_does_not_replay_previous_bars(
    tmp_path, monkeypatch, kind
):
    path, _entry, fetch = _loader(tmp_path, monkeypatch, kind)
    _write(path, kind, 10)
    assert "AAA.US" in fetch()
    path.unlink()
    assert fetch() == {}


def test_duckdb_wal_revision_participates_in_source_identity(tmp_path, monkeypatch):
    import duckdb

    path, entry, _fetch = _loader(tmp_path, monkeypatch, "duckdb")
    _write(path, "duckdb", 10)
    with duckdb.connect(str(path)) as connection:
        revision = getattr(
            local_loader, "_source_file_revision", local_loader._source_cache_identity
        )
        before = revision(entry)
        connection.execute("UPDATE bars SET close=20")
        assert Path(str(path) + ".wal").exists()
        assert revision(entry) != before


@pytest.mark.parametrize("kind", ["csv", "parquet", "duckdb"])
def test_many_source_revisions_replace_one_cache_entry(tmp_path, monkeypatch, kind):
    path, _entry, fetch = _loader(tmp_path, monkeypatch, kind)
    for value in range(10, 35):
        _write(path, kind, value)
        assert fetch()["AAA.US"]["close"].tolist() == [value]
        assert len(list((tmp_path / "cache").rglob("*.parquet"))) == 1
        assert len(list((tmp_path / "cache").rglob("*.json"))) == 1
    assert not list((tmp_path / "cache").rglob("*.tmp"))


def test_legacy_local_metadata_refetches_once_then_retains_cache(tmp_path, monkeypatch):
    import json

    path, _entry, fetch = _loader(tmp_path, monkeypatch, "csv")
    _write(path, "csv", 10)
    assert "AAA.US" in fetch()
    metadata = next((tmp_path / "cache").rglob("*.json"))
    document = json.loads(metadata.read_text())
    document.pop("source_revision")
    metadata.write_text(json.dumps(document))
    original = local_loader._READERS["csv"]
    calls = []

    def read(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setitem(local_loader._READERS, "csv", read)
    assert "AAA.US" in fetch()
    assert "AAA.US" in fetch()
    assert len(calls) == 1
    assert json.loads(metadata.read_text())["source_revision"]


def test_nonlocal_default_cache_contract_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "loader_cache_enabled", lambda: True)
    monkeypatch.setattr(base, "loader_cache_root", lambda: tmp_path / "cache")
    frame = pd.DataFrame({"close": [10]}, index=pd.DatetimeIndex(["2025-01-02"]))
    calls = []

    def read():
        calls.append(True)
        return frame

    options = dict(
        source="owned-provider-fixture",
        symbol="AAA.US",
        timeframe="1D",
        start_date="2025-01-02",
        end_date="2025-01-03",
        fields=None,
        fetch=read,
    )
    pd.testing.assert_frame_equal(base.cached_loader_fetch(**options), frame)
    pd.testing.assert_frame_equal(base.cached_loader_fetch(**options), frame)
    assert len(calls) == 1


def test_mismatched_revision_payload_is_refetched(tmp_path, monkeypatch):
    import shutil

    path, _entry, fetch = _loader(tmp_path, monkeypatch, "csv")
    _write(path, "csv", 10)
    assert fetch()["AAA.US"]["close"].tolist() == [10]
    cache_file = next((tmp_path / "cache").rglob("*.parquet"))
    old_payload = tmp_path / "old-payload.parquet"
    shutil.copy2(cache_file, old_payload)
    _write(path, "csv", 20)
    assert fetch()["AAA.US"]["close"].tolist() == [20]
    # A concurrent old writer can replace data after the new metadata wins.
    # Same schema and row count cannot detect this mixed data/metadata pair.
    os.replace(old_payload, cache_file)
    original = local_loader._READERS["csv"]
    reads = []

    def read(*args):
        reads.append(True)
        return original(*args)

    monkeypatch.setitem(local_loader._READERS, "csv", read)
    assert fetch()["AAA.US"]["close"].tolist() == [20]
    assert len(reads) == 1
    assert len(list((tmp_path / "cache").rglob("*.parquet"))) == 1


def test_revision_payload_replaced_during_read_is_a_miss(tmp_path, monkeypatch):
    import duckdb
    import shutil

    path, entry, fetch = _loader(tmp_path, monkeypatch, "csv")
    _write(path, "csv", 10)
    assert "AAA.US" in fetch()
    cache_file = next((tmp_path / "cache").rglob("*.parquet"))
    replacement = tmp_path / "replacement.parquet"
    shutil.copy2(cache_file, replacement)
    original_connect = duckdb.connect

    class ReadConnection:
        def __init__(self):
            self.connection = original_connect(database=":memory:")

        def execute(self, query):
            self.connection.execute(query)
            return self

        def fetchdf(self):
            frame = self.connection.fetchdf()
            os.replace(replacement, cache_file)
            return frame

        def close(self):
            self.connection.close()

    monkeypatch.setattr(duckdb, "connect", lambda **kwargs: ReadConnection())
    assert (
        base._read_loader_cache_frame(
            cache_file, local_loader._source_file_revision(entry)
        )
        is None
    )
