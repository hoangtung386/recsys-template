from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from recsys.logging_config import configure_logging
from recsys.serving.sinks import JsonlSink, MemorySink, NullSink
from recsys.settings import Settings


def test_settings_parse_env_and_hide_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECSYS_APPS", "a, b")
    monkeypatch.setenv("REC_MAX_INFLIGHT", "3")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
    monkeypatch.setenv("RECSYS_REGISTRY_DSN", "postgresql://user:hunter2@db/x")
    settings = Settings()
    assert settings.apps == ["a", "b"]
    assert settings.max_inflight == 3
    assert settings.mlflow_tracking_uri == "http://mlflow:5000"
    assert "hunter2" not in repr(settings) and "hunter2" not in str(settings)


def test_settings_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REC_MAX_INFLIGHT", "0")
    with pytest.raises(ValueError):
        Settings()


def test_json_logging_emits_valid_json_with_extras(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", json_output=True)
    logging.getLogger("recsys.test").info("hello", extra={"model_version": "v1"})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "hello" and record["model_version"] == "v1"
    assert record["level"] == "info" and "timestamp" in record
    configure_logging("INFO", json_output=False)
    logging.getLogger("recsys.test").info("plain")
    assert "plain" in capsys.readouterr().err


def test_memory_and_null_sinks() -> None:
    memory = MemorySink()
    assert memory.send({"a": 1}) and memory.messages == [{"a": 1}]
    assert NullSink().send({"a": 1})


def test_jsonl_sink_writes_everything_on_close(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "served.jsonl"
    sink = JsonlSink(path, max_queue=1000)
    for i in range(200):
        assert sink.send({"i": i})
    sink.close()
    assert [json.loads(line)["i"] for line in path.read_text().splitlines()] == list(
        range(200)
    )


def test_jsonl_sink_drops_instead_of_blocking_when_full(tmp_path: Path) -> None:
    dropped: list[int] = []
    sink = JsonlSink(
        tmp_path / "s.jsonl", max_queue=1, on_drop=lambda: dropped.append(1)
    )
    started = time.perf_counter()
    results = [sink.send({"i": i}) for i in range(5000)]
    assert time.perf_counter() - started < 2.0
    sink.close()
    assert not all(results) and len(dropped) == results.count(False)
