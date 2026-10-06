from __future__ import annotations

import os
from pathlib import Path

import pytest

import backend.app.application.system_health as system_health
from backend.app.application.system_health import SystemHealthService, SystemResourceSampler


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


def test_memory_metrics_supports_synology_cgroup_v1(tmp_path: Path) -> None:
    proc = tmp_path / "meminfo"
    proc.write_text(
        "MemTotal:       16384 kB\nMemAvailable:    8192 kB\n",
        encoding="ascii",
    )
    cgroup_v1 = tmp_path / "cgroup-v1"
    cgroup_v1.mkdir()
    (cgroup_v1 / "memory.limit_in_bytes").write_text(str(8 * 1024 * 1024), encoding="ascii")
    (cgroup_v1 / "memory.usage_in_bytes").write_text(str(3 * 1024 * 1024), encoding="ascii")

    assert system_health._memory_metrics(
        proc_meminfo_path=proc,
        cgroup_v2_root=tmp_path / "missing-v2",
        cgroup_v1_memory_root=cgroup_v1,
    ) == (8 * 1024 * 1024, 5 * 1024 * 1024)


def test_memory_metrics_falls_back_to_proc_when_cgroup_files_are_absent(tmp_path: Path) -> None:
    proc = tmp_path / "meminfo"
    proc.write_text(
        "MemTotal:       16384 kB\nMemAvailable:    6144 kB\n",
        encoding="ascii",
    )

    assert system_health._memory_metrics(
        proc_meminfo_path=proc,
        cgroup_v2_root=tmp_path / "missing-v2",
        cgroup_v1_memory_root=tmp_path / "missing-v1",
    ) == (16 * 1024 * 1024, 6 * 1024 * 1024)


def test_resource_sampler_keeps_bounded_history_without_dashboard_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = iter((10.0, 20.0, 30.0))
    monkeypatch.setattr(system_health, "_cpu_load_percent", lambda: next(samples))
    sampler = SystemResourceSampler(history_size=2)

    assert sampler.sample_now() == 10.0
    assert sampler.sample_now() == 20.0
    assert sampler.sample_now() == 30.0
    assert sampler.snapshot() == (30.0, (20.0, 30.0))
