import os

import pytest

import backend.app.container_healthcheck as container_healthcheck


def test_root_healthcheck_drops_to_puid_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUID", "1026")
    monkeypatch.setenv("PGID", "100")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(os, "setgroups", lambda value: calls.append(("groups", value)))
    monkeypatch.setattr(os, "setgid", lambda value: calls.append(("gid", value)))
    monkeypatch.setattr(os, "setuid", lambda value: calls.append(("uid", value)))

    container_healthcheck._drop_healthcheck_privileges()

    assert calls == [("groups", []), ("gid", 100), ("uid", 1026)]


def test_non_root_healthcheck_keeps_current_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1026)
    calls: list[str] = []
    monkeypatch.setattr(os, "setgroups", lambda _value: calls.append("groups"))
    monkeypatch.setattr(os, "setgid", lambda _value: calls.append("gid"))
    monkeypatch.setattr(os, "setuid", lambda _value: calls.append("uid"))

    container_healthcheck._drop_healthcheck_privileges()

    assert calls == []
