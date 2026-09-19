from __future__ import annotations

import os

import pytest

import backend.app.application.system_health as system_health
from backend.app.application.system_health import SystemHealthService


def test_resources_check_exposes_read_only_memory_and_normalized_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_health, "_memory_metrics", lambda: (16 * 1024, 6 * 1024))
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 0.5, 0.2))

    service = object.__new__(SystemHealthService)
    report = service._resources_check()

    assert report.status == "ok"
    assert report.metrics["memory_total_bytes"] == 16 * 1024
    assert report.metrics["memory_available_bytes"] == 6 * 1024
    assert report.metrics["cpu_load_percent"] == 25.0


def test_resources_check_does_not_fake_unavailable_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_health, "_memory_metrics", lambda: None)
    monkeypatch.setattr(os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))
    service = object.__new__(SystemHealthService)
    report = service._resources_check()
    assert report.status == "warning"
    assert report.metrics["memory_total_bytes"] is None
    assert report.metrics["cpu_load_percent"] is None
