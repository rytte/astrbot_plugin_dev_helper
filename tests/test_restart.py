"""Verify restart confirmation and delivery without restarting the test process."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def restart_env(env, monkeypatch):
    starts = []

    def thread(*, target, name, daemon):
        assert target is env.main.restart_process
        assert name == "restart" and daemon is True
        return SimpleNamespace(start=lambda: starts.append(True))

    monkeypatch.setattr(env.main, "is_desktop_managed_backend", lambda: False)
    monkeypatch.setattr(env.main.threading, "Thread", thread)
    return env, starts


async def test_restart_requires_confirmation_and_sends_reply_first(
    restart_env, monkeypatch
):
    env, starts = restart_env
    first = env.event("/restart")
    await env.scheduler.execute(first)
    assert "60 秒内发送 /restart confirm" in "".join(first.sent)
    assert not starts

    confirmed = env.event("/restart confirm")
    original_start = env.main.threading.Thread

    def checked_thread(*, target, name, daemon):
        original_start(target=target, name=name, daemon=daemon)
        return SimpleNamespace(
            start=lambda: starts.append(
                "已确认，AstrBot 即将重启。" in "".join(confirmed.sent)
            )
        )

    monkeypatch.setattr(env.main.threading, "Thread", checked_thread)
    await env.scheduler.execute(confirmed)
    assert starts == [True]
    assert confirmed.is_stopped()
    assert not env.model_calls


async def test_restart_rejects_unconfirmed_other_session_and_expired(restart_env):
    env, starts = restart_env

    direct = env.event()
    await env.invoke("restart", direct, "confirm")
    assert "已过期" in "".join(direct.sent)

    await env.invoke("restart", env.event(platform="qq_one"), "")
    other_session = env.event(platform="qq_two")
    await env.invoke("restart", other_session, "confirm")
    assert "已过期" in "".join(other_session.sent)

    key = (env.event(platform="qq_one").unified_msg_origin, "admin")
    env.plugin._restart_confirmations[key] = env.main.time.monotonic() - 1
    expired = env.event(platform="qq_one")
    await env.invoke("restart", expired, "confirm")
    assert "已过期" in "".join(expired.sent)
    assert not starts


async def test_restart_rejects_group_and_api_role(restart_env):
    env, starts = restart_env
    group = env.event(group="group")
    await env.invoke("restart", group, "")
    assert "私聊" in "".join(group.sent)

    api_role = env.event()
    api_role.set_extra("_api_key_allow_admin_role", False)
    await env.invoke("restart", api_role, "")
    assert "仅限" in "".join(api_role.sent)

    assert not starts


async def test_desktop_restart_runs_only_after_confirmation(restart_env, monkeypatch):
    env, starts = restart_env
    monkeypatch.setattr(env.main, "is_desktop_managed_backend", lambda: True)
    desktop_starts = []
    monkeypatch.setattr(
        env.main, "schedule_desktop_restart", lambda: desktop_starts.append(True)
    )

    first = env.event("/restart")
    await env.scheduler.execute(first)
    assert "整个 AstrBot Desktop 应用" in "".join(first.sent)
    assert not desktop_starts

    confirmed = env.event("/restart confirm")
    await env.scheduler.execute(confirmed)
    assert desktop_starts == [True]
    assert not starts
